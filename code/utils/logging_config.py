import logging
import os
from logging.handlers import RotatingFileHandler

def setup_logging(log_level=logging.INFO, log_file="../logs/main.log"):
    # 確保 log 目錄存在
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger()  # root logger
    logger.setLevel(log_level)

    # 清除現有的 handler，避免重複
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

    # 添加 handler
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger
