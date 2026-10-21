"""UTF-8 application logging with secret redaction and bounded file size."""

import json
import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import PROJECT_ROOT

logging.getLogger("youtube_collector").addHandler(logging.NullHandler())

DEFAULT_LOG_PATH = PROJECT_ROOT / "logs" / "collector.log"


class SecretFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.secrets = tuple(value for value in secrets if value)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[REDACTED]")
        message = re.sub(r"AIza[A-Za-z0-9_-]{20,}", "[REDACTED]", message)
        message = re.sub(r"https?://[^\s\"<>]+", "[REDACTED_URL]", message)
        message = re.sub(r"(?i)((?:api[_-]?key|x-goog-api-key|authorization)\s*[:=]\s*)[^\s,\"}]+",
                         r"\1[REDACTED]", message)
        record.msg, record.args = message, ()
        # Application logs use error categories, never raw tracebacks or request objects.
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def configure_logging(
    log_path: str | Path = DEFAULT_LOG_PATH, *, secrets: tuple[str, ...] = (),
) -> None:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("youtube_collector")
    for handler in logger.handlers[:]:
        if getattr(handler, "collector_file", False):
            logger.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.collector_file = True
    handler.addFilter(SecretFilter(secrets))
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s %(message)s",
                                  datefmt="%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


_EVENT_CONTEXT = ContextVar("collector_event_context", default=("unknown", None))


@contextmanager
def event_context(origin: str, run_id: str | None = None):
    """Explicit provenance; importing a test runner is not evidence of origin."""
    if origin not in {"real", "test", "unknown"}:
        raise ValueError("Invalid event origin")
    token = _EVENT_CONTEXT.set((origin, run_id or (uuid4().hex if origin != "unknown" else None)))
    try:
        yield
    finally:
        _EVENT_CONTEXT.reset(token)


@contextmanager
def run_context():
    origin, _ = _EVENT_CONTEXT.get()
    with event_context("real" if origin == "unknown" else origin):
        yield


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields) -> None:
    origin, run_id = _EVENT_CONTEXT.get()
    fields.update(origin=origin, run_id=run_id)
    logger.log(level, json.dumps({"event": event, **fields}, ensure_ascii=False))
