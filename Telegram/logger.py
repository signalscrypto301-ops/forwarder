import os
import sys
import logging
from logging.handlers import RotatingFileHandler

LOG_PATH = os.path.join(os.path.dirname(__file__), "admin.log")

formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

# File handler with rotation
file_handler = RotatingFileHandler(
    LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)

# Ensure stdout supports UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Console handler for Docker / terminal visibility
console_handler = logging.StreamHandler(sys.stdout)

console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

# Root logger configuration to capture Telethon, aiogram, and urllib3
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logger = logging.getLogger("forwarder")
logger.setLevel(logging.DEBUG)


def log_unhandled_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.error("Unhandled exception occurred", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = log_unhandled_exception


