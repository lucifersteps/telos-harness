"""Logging setup."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from telos.core.clock import USER_TIMEZONE


DEFAULT_LOG_TIME_FORMAT = "%H:%M:%S"


class ConsoleLogFormatter(logging.Formatter):
    RESET = "\x1b[0m"
    LEVEL_COLORS = {
        "DEBUG": "\x1b[2m",
        "INFO": "\x1b[36m",
        "WARNING": "\x1b[33m",
        "ERROR": "\x1b[31m",
        "CRITICAL": "\x1b[1;31m",
    }

    def __init__(self, *, use_color: bool = False):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=USER_TIMEZONE).strftime(DEFAULT_LOG_TIME_FORMAT)
        level = f"{record.levelname:<7}"
        if self.use_color:
            color = self.LEVEL_COLORS.get(record.levelname)
            if color:
                level = f"{color}{level}{self.RESET}"
        logger_name = f"{_short_logger_name(record.name):<22}"
        prefix = f"{timestamp} | {level} | {logger_name} | "
        message = _indent_multiline(record.getMessage(), prefix)
        message = f"{prefix}{message}"
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return message


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=USER_TIMEZONE).replace(tzinfo=None).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _STANDARD_LOG_RECORD_KEYS:
                continue
            data[key] = value
        return json.dumps(data, ensure_ascii=False, sort_keys=True)


def configure_logging(
    *,
    level: str | int | None = None,
    json_logs: bool | None = None,
    force: bool = False,
) -> None:
    root = logging.getLogger()
    log_file = os.environ.get("TELOS_LOG_FILE", "").strip()
    if root.handlers and not force:
        root.setLevel(_coerce_level(level))
        if log_file:
            ensure_log_file_handler(log_file)
        return

    handler = logging.StreamHandler(sys.stderr)
    if _use_json_logs(json_logs):
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(ConsoleLogFormatter(use_color=_use_color_logs()))

    if force:
        root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(_coerce_level(level))
    if log_file:
        ensure_log_file_handler(log_file)


def ensure_log_file_handler(path: str | Path) -> None:
    """Attach a rotating JSON-lines file handler to the root logger, once.

    Idempotent: repeated calls (or calls from both the web process and the
    timeline collector subprocess, which inherit ``TELOS_LOG_FILE``) do not add
    duplicate handlers for the same file. The web console tails this file to
    surface runtime activity (截图 / 送审 / recap) in the browser.
    """
    target = Path(path).expanduser()
    abspath = os.path.abspath(str(target))
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing, logging.FileHandler) and getattr(existing, "baseFilename", "") == abspath:
            return
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        abspath,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(JsonLogFormatter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def _coerce_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    raw = str(level or os.environ.get("TELOS_LOG_LEVEL") or "WARNING").upper()
    value = getattr(logging, raw, None)
    return value if isinstance(value, int) else logging.WARNING


def _use_json_logs(json_logs: bool | None) -> bool:
    if json_logs is not None:
        return bool(json_logs)
    return os.environ.get("TELOS_LOG_JSON", "").strip().lower() in {"1", "true", "yes"}


def _use_color_logs() -> bool:
    raw = os.environ.get("TELOS_LOG_COLOR", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"} or os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stderr, "isatty", lambda: False)())


def _short_logger_name(name: str) -> str:
    text = str(name or "root")
    if text.startswith("telos."):
        text = text.removeprefix("telos.")
    if len(text) <= 22:
        return text
    parts = text.split(".")
    if len(parts) > 2:
        text = ".".join(parts[-2:])
    if len(text) <= 22:
        return text
    return "..." + text[-19:]


def _indent_multiline(message: str, prefix: str) -> str:
    if "\n" not in message:
        return message
    if message.startswith("\n"):
        return message
    lines = message.splitlines()
    if not lines:
        return ""
    indent = " " * len(prefix)
    return "\n".join([lines[0], *(f"{indent}{line}" for line in lines[1:])])


_STANDARD_LOG_RECORD_KEYS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}
