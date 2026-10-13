"""Structured logging with secret / PII redaction.

Use ``get_logger(__name__).info("event_name", extra={"fields": {...}})`` or the
``log_event`` helper. Values under sensitive keys are replaced with
``[REDACTED]`` before they are emitted, and bearer tokens / emails embedded in
free-text messages are masked.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

SENSITIVE_KEYS = {
    "password", "passwd", "secret", "token", "access_token", "refresh_token", "authorization",
    "api_key", "apikey", "client_secret", "cookie", "email", "phone", "ip", "message_body",
    "first_name", "last_name", "name",
}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_QS_SECRET_RE = re.compile(r"(?i)((?:access_token|client_secret|token|key)=)[^&\s]+")


def scrub_text(text: str) -> str:
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _QS_SECRET_RE.sub(r"\1[REDACTED]", text)
    return _EMAIL_RE.sub("[EMAIL]", text)


def redact(value: Any, _key: str | None = None) -> Any:
    if _key is not None and _key.lower() in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": scrub_text(record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload.update(redact(fields))
        if record.exc_info:
            payload["exc"] = scrub_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:7} {record.name}: {scrub_text(record.getMessage())}"
        fields = getattr(record, "fields", None)
        if fields:
            base += " " + json.dumps(redact(fields), default=str)
        if record.exc_info:
            base += "\n" + scrub_text(self.formatException(record.exc_info))
        return base


_configured = False


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    global _configured
    root = logging.getLogger()
    if _configured:
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.handlers = [handler]
    root.setLevel(level)
    # Never let HTTP client debug logs leak headers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": fields})


def init_error_monitoring(dsn: str | None, environment: str) -> bool:
    """Initialise Sentry if a DSN is configured and sentry-sdk is installed."""
    if not dsn:
        return False
    try:
        import sentry_sdk  # type: ignore
    except ImportError:
        logging.getLogger(__name__).warning("SENTRY_DSN set but sentry-sdk is not installed")
        return False
    sentry_sdk.init(dsn=dsn, environment=environment, send_default_pii=False, traces_sample_rate=0.0)
    return True
