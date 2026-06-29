"""Logging setup.

Mirrors the LOS logging config (stream + file handler, no redaction). Logs are written
to logs/disclosure-service.log.
"""
import logging
import os

LOG_DIR = os.getenv("LOG_DIR", "logs")


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    fmt = logging.Formatter("%(levelname)s %(asctime)s %(name)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.FileHandler(os.path.join(LOG_DIR, "disclosure-service.log"))
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger
