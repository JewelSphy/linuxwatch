import logging
from logging.handlers import RotatingFileHandler
from config import LOG_PATH


def setup_logging():
   log = logging.getLogger("linuxwatch")
   if log.handlers:
      return log
   log.setLevel(logging.INFO)

   fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

   file_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
   file_handler.setFormatter(fmt)

   screen_handler = logging.StreamHandler()
   screen_handler.setFormatter(fmt)

   log.addHandler(file_handler)
   log.addHandler(screen_handler)
   return log
