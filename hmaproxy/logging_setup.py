"""Logging: levels, rotating file, timestamp + proxy id, and secret redaction.

Every record passes through ``RedactFilter`` which replaces registered secrets
(the VPN password, the proxy password) with ``***`` so they never reach a log
file or the console, whatever code path produced the message.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path
from urllib.parse import quote

LOGGER_NAME = "hmaproxy"
FORMAT = "%(asctime)s %(levelname)-7s [%(proxy_id)s] %(message)s"

_secrets: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    """Mark a value as secret so it is masked in all log output."""
    if not value or len(value) < 3:
        # Masking 1-2 char strings would mangle normal text; such passwords
        # are rejected elsewhere anyway.
        return
    with _lock:
        _secrets.add(value)
        _secrets.add(quote(value, safe=""))


def redact(text: str) -> str:
    if not text:
        return text
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, "***")
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "proxy_id"):
            record.proxy_id = "-"
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed format args
            message = str(record.msg)
        clean = redact(message)
        if clean != message or record.args:
            record.msg = clean
            record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def get_logger(proxy_id: str | None = None) -> logging.LoggerAdapter:
    return logging.LoggerAdapter(logging.getLogger(LOGGER_NAME), {"proxy_id": proxy_id or "-"})


def setup_logging(log_file: Path | None, level: str = "INFO", console_level: str = "WARNING",
                  max_bytes: int = 5 * 1024 * 1024, backup_count: int = 5) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()

    fmt = logging.Formatter(FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    redact_filter = RedactFilter()

    if sys.stderr is not None:  # None under pythonw.exe
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(_level(console_level))
        console.setFormatter(fmt)
        console.addFilter(redact_filter)
        logger.addHandler(console)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            os.chmod(log_file, 0o600)
        except OSError as exc:
            logger.warning("cannot open log file %s: %s", log_file, exc)
        else:
            fh.setLevel(_level(level))
            fh.setFormatter(fmt)
            fh.addFilter(redact_filter)
            logger.addHandler(fh)


def _level(name: str) -> int:
    name = name.upper()
    if name == "WARN":
        name = "WARNING"
    return getattr(logging, name, logging.INFO)
