import json
import logging
import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple
from together import AsyncTogether
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageToolCall

from app.database.models import Message, MessageRole, User
from app.config import llm_settings
from app.database.db import get_user_message_history
from app.services.whatsapp_service import (
    whatsapp_client,
)  # TODO: send updates to user during tool calls
from assets.prompts import get_system_prompt
from app.tools.registry import tools_functions, tools_metadata, ToolName


class MessageProcessor:
    """Handles processing and batching of messages for a single user."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.lock = asyncio.Lock()
        self.messages: List[str] = []

    def add_message(self, message: str) -> None:
        self.messages.append(message)

    def get_pending_messages(self) -> List[str]:
        return self.messages.copy()

    def clear_messages(self) -> None:
        self.messages.clear()

    @property
    def has_messages(self) -> bool:
        return bool(self.messages)

    @property
    def is_locked(self) -> bool:
        return self.lock.locked()


class LLMClient:
    def __init__(self):
        self.client = AsyncOpenAI(
            base_url="https://api.together.xyz/v1",
            api_key=llm_settings.together_api_key.get_secret_value(),
        )
        self.logger = logging.getLogger(__name__)
        self._processors: dict[int, MessageProcessor] = {}
        # self._user_locks = {}  # {user_id: Lock()}
        # self._message_buffers = {}  # {user_id: ["message1", "message2"]}

    def _get_processor(self, user_id: int) -> MessageProcessor:
        """Get or create a message processor for a user."""
        if user_id not in self._processors:
            self._processors[user_id] = MessageProcessor(user_id)
        return self._processors[user_id]

    def _cleanup_processor(self, user_id: int) -> None:
        """Remove processor if it's empty and unlocked."""
        processor = self._processors.get(user_id)
        if processor and not processor.has_messages and not processor.is_locked:
            del self._processors[user_id]

    async def _check_new_messages(
        self, processor: MessageProcessor, original_count: int
    ) -> bool:
        """Check if new messages arrived during processing."""
        return len(processor.messages) > original_count

    # TODO: this might best be done in the llm_utils file
    async def _generate_completion(
        self, messages: List[dict], include_tools: bool = True
    ) -> ChatCompletionMessage:
        """Generate a completion from the API with optional tool support."""
        try:
            response = await self.client.chat.completions.create(
                model=llm_settings.llm_model_name,
                messages=messages,
                tools=tools_metadata if include_tools else None,
                tool_choice="auto" if include_tools else None,
            )
            return response.choices[0].message
        except Exception as e:
            self.logger.error(f"Error generating completion: {str(e)}")
            return None

    async def _process_tool_calls(
        self, messages: List[dict], tool_calls: List[ChatCompletionMessageToolCall]
    ) -> List[dict]:
        """Process tool calls and append results to messages."""
        updated_messages = messages.copy()
        # Convert each tool_call to a dictionary
        tool_calls_serializable = [
            {
                "function_name": tool_call.function.name,
                "arguments": tool_call.function.arguments,
                "tool_call_id": tool_call.id,
            }
            for tool_call in tool_calls
        ]
        updated_messages.append(
            {
                "role": MessageRole.assistant,
                "tool_calls": json.dumps(tool_calls_serializable),
            }
        )

        for tool_call in tool_calls:
            try:
                function_name = tool_call.function.name
                function_args = json.loads(tool_call.function.arguments)

                if function_name in tools_functions:
                    result = tools_functions[function_name](**function_args)

                    updated_messages.append(
                        {
                            "role": MessageRole.tool,
                            "content": json.dumps(result),
                            "tool_call_id": tool_call.id,
                        },
                    )

            except Exception as e:
                self.logger.error(f"Error processing tool call: {str(e)}")

        return updated_messages

    async def generate_response(
        self,
        user: User,
        message: str,
    ) -> Optional[List[dict]]:
        """Generate a response, handling message batching and tool calls."""
        processor = self._get_processor(user.id)
        processor.add_message(message)

        self.logger.info(
            f"Message buffer for user: {user.wa_id}, buffer: {processor.get_pending_messages()}"
        )

        if processor.is_locked:
            self.logger.info(f"Lock held for user {user.id}, message buffered")
            return None

        async with processor.lock:
            while True:
                try:
                    messages_to_process = processor.get_pending_messages()

                    if not messages_to_process:  # Shouldn't happen, but just in case
                        self.logger.debug(f"No messages to process for user {user.id}")
                        processor.clear_messages()
                        self._cleanup_processor(user.id)
                        return None

                    # Fetch message history from the database and format
                    message_history = await get_user_message_history(user.id)
                    formatted_messages = self._format_conversation_history(
                        message_history
                    )

                    # Prepare messages for API and removes duplicate messages from database and messages_to_process
                    api_messages = [
                        *formatted_messages[: -(len(messages_to_process))],
                        {"role": "user", "content": "\n".join(messages_to_process)},
                    ]

                    # Initial generation with tools enabled
                    initial_response = await self._generate_completion(
                        api_messages, include_tools=True
                    )
                    # TODO: add some logs for this
                    if not initial_response:
                        return None

                    # Check for new messages
                    if await self._check_new_messages(
                        processor, len(messages_to_process)
                    ):
                        self.logger.info(
                            "New messages arrived during processing, restarting"
                        )
                        continue

                    # Process tool calls if present
                    if initial_response.tool_calls:
                        self.logger.info("Processing tool calls")
                        updated_messages = await self._process_tool_calls(
                            api_messages, initial_response.tool_calls
                        )

                        self.logger.debug(updated_messages)

                        final_response = await self._generate_completion(
                            messages=updated_messages,
                            include_tools=False,
                        )

                        # TODO: add some logs for this
                        if not final_response:
                            return None

                        # Check for new messages again
                        if await self._check_new_messages(
                            processor, len(messages_to_process)
                        ):
                            self.logger.info(
                                "New messages arrived during processing of tools, restarting"
                            )
                            continue

                        # This is bad code, should fix later
                        response_messages = [
                            *[
                                {
                                    "role": msg["role"],
                                    "content": (
                                        msg.get("tool_calls")
                                        if i == 0
                                        else msg.get("content")
                                    ),
                                }
                                for i, msg in enumerate(
                                    updated_messages[
                                        -(len(initial_response.tool_calls) + 1) :
                                    ]
                                )
                            ],
                            {
                                "role": MessageRole.assistant,
                                "content": final_response.content,
                            },
                        ]
                    else:
                        response_messages = [
                            {
                                "role": MessageRole.assistant,
                                "content": initial_response.content,
                            }
                        ]

                    # Success - clear buffer and return response
                    self.logger.info("Message processing complete, clearing buffer")
                    processor.clear_messages()
                    self._cleanup_processor(user.id)

                    return response_messages

                except Exception as e:
                    self.logger.error(f"Error processing messages: {e}")
                    processor.clear_messages()
                    self._cleanup_processor(user.id)
                    return None

    def _format_conversation_history(
        self, messages: Optional[List[Message]]
    ) -> List[dict]:
        # TODO: Handle message history using eg. sliding window, truncation, vector database, summarization
        formatted_messages = []

        # Add system message at the start
        system_prompt = get_system_prompt("default_system")

        # Add system message (this is not stored in the database to allow for updates)
        formatted_messages.append({"role": "system", "content": system_prompt})

        # Format each message from the history
        if messages:
            for msg in messages:
                formatted_messages.append({"role": msg.role, "content": msg.content})

        return formatted_messages


llm_client = LLMClient()
