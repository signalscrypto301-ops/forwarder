import logging
import sys
from logging.handlers import RotatingFileHandler

def log_unhandled_exception(exc_type, exc_value, exc_traceback):
    logger.error("Unhandled exception occurred", exc_info=(exc_type, exc_value, exc_traceback))

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

log_handler = RotatingFileHandler('admin.log', maxBytes=1024*1024, backupCount=5)
log_handler.setLevel(logging.DEBUG)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_handler.setFormatter(formatter)

logger.addHandler(log_handler)

sys.excepthook = log_unhandled_exception

