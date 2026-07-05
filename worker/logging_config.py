import json
import logging
import os
import sys
from datetime import datetime, timezone

_RESERVED_KEYS = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "task_id"}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "task_id": getattr(record, "task_id", None),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_KEYS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)[:2000]
        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.task_id = getattr(record, "task_id", None)
        base = super().format(record)
        extras = {key: value for key, value in record.__dict__.items() if key not in _RESERVED_KEYS}
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


_TEXT_FMT = "%(asctime)s %(levelname)-8s %(name)s task_id=%(task_id)s %(message)s"

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = os.environ.get("LOG_FORMAT", "json").lower()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if fmt == "json" else _TextFormatter(_TEXT_FMT))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
