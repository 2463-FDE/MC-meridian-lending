"""Logging setup.

Logs the full request body on every POST — including PII. No redaction.
Halcyon said "we need the body to debug." (D5)

Output goes to logs/kyc-service.log.
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
        fh = logging.FileHandler(os.path.join(LOG_DIR, "kyc-service.log"))
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger
