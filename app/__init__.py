import logging
from colorlog import ColoredFormatter

log_format = (
    "%(log_color)s%(levelname)s%(reset)s:     %(asctime)s - %(name)s - %(message)s"
)

# Configure color logging
formatter = ColoredFormatter(
    log_format,
    datefmt="%Y-%m-%d %H:%M:%S",  # Concise timestamp format
    log_colors={
        "DEBUG": "cyan",
        "INFO": "green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "bold_red",
    },
    reset=True,  # Reset colors after each log message
    style="%",  # Use % formatting style
)

# Create a stream handler to output logs to the console
handler = logging.StreamHandler()
handler.setFormatter(formatter)

# Get the root logger and set its level
logger = logging.getLogger()
logger.setLevel(logging.INFO)  # Adjust as needed (DEBUG, INFO, WARNING, etc.)

# Clear existing handlers and set the new handler
if logger.hasHandlers():
    logger.handlers.clear()
logger.addHandler(handler)
