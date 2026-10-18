"""Structured Logging for PolyArb.
Provides dual console and JSON-line file logging with log rotation and trace_id correlation.
"""
from __future__ import annotations
import contextvars
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def set_trace_id(trace_id: str):
    """Set the correlation trace_id for current execution context."""
    _current_trace_id.set(trace_id)


def get_trace_id() -> str:
    """Retrieve current correlation trace_id."""
    return _current_trace_id.get()


class JsonFormatter(logging.Formatter):
    """Formats log records as structured JSON."""
    def format(self, record: logging.LogRecord) -> str:
        record_dict: dict[str, Any] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        trace_id = get_trace_id()
        if trace_id:
            record_dict["trace_id"] = trace_id
        if hasattr(record, "extra_data") and isinstance(record.extra_data, dict):
            record_dict.update(record.extra_data)
        if record.exc_info:
            record_dict["exception"] = self.formatException(record.exc_info)
        return json.dumps(record_dict)


class ConsoleFormatter(logging.Formatter):
    """Clean human-readable console formatter."""
    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%H:%M:%S")
        trace_id = get_trace_id()
        prefix = f"[{ts}] [{record.levelname:5s}]"
        if trace_id:
            prefix += f" [{trace_id}]"
        prefix += f" [{record.name}]:"
        return f"{prefix} {record.getMessage()}"


def setup_logging(log_dir: str = "logs", level: int = logging.INFO) -> logging.Logger:
    """Sets up root 'polyarb' logger with both rotating JSON file and console handlers."""
    logger = logging.getLogger("polyarb")
    logger.setLevel(level)

    # Avoid duplicate handlers if setup called multiple times
    if logger.handlers:
        return logger

    # 1. Console Handler
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(ConsoleFormatter())
    logger.addHandler(ch)

    # 2. Rotating JSON File Handler
    try:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_path / "polyarb.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(JsonFormatter())
        logger.addHandler(fh)
    except Exception as e:
        logger.warning(f"Failed to initialize rotating file logger: {e}")

    return logger


def get_logger(name: str = "polyarb") -> logging.Logger:
    """Retrieve logger namespaced under polyarb."""
    if not name.startswith("polyarb"):
        name = f"polyarb.{name}"
    return logging.getLogger(name)
