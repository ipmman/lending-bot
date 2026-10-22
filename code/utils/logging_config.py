import logging
import os
from logging.handlers import RotatingFileHandler

def setup_logging(log_level=logging.INFO, log_file="../logs/main.log"):
    # Make sure the log directory exists
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger()  # root logger
    logger.setLevel(log_level)
    # At DEBUG, websockets logs every frame (including the apiKey in the auth message), so never go below INFO
    logging.getLogger("websockets").setLevel(max(log_level, logging.INFO))

    # Clear existing handlers to avoid duplicates
    logger.handlers.clear()
    
    # RotatingFileHandler: 20MB per file, keep last 5 files
    file_handler = RotatingFileHandler(
        log_file, 
        mode='a', 
        maxBytes=20*1024*1024, 
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

    # StreamHandler
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(log_level)
    stream_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

    # Add handlers
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger
