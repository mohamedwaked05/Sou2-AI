"""Environment-aware privacy-safe logging and defense-in-depth redaction."""

import json
import logging
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?i)(password|authorization|cookie|access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|secret|database[_-]?url|connection[_-]?string)"
    r"(\s*[:=]\s*)([^\s,;}]+)"
)
BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*")
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
DATABASE_URL_PATTERN = re.compile(r"(?i)postgres(?:ql)?(?:\+\w+)?://[^\s]+")
request_id_context: ContextVar[str | None] = ContextVar(
    "sou2ai_request_id", default=None
)


def redact_log_value(value: object) -> str:
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: [REDACTED_INTERNAL_EXCEPTION]"
    redacted = str(value)
    redacted = BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)
    redacted = SENSITIVE_FIELD_PATTERN.sub(r"\1\2[REDACTED]", redacted)
    redacted = JWT_PATTERN.sub("[REDACTED_TOKEN]", redacted)
    return DATABASE_URL_PATTERN.sub("[REDACTED_DATABASE_URL]", redacted)


def _safe_log_argument(value: object) -> object:
    # Preserve numeric arguments for %d/%.3f diagnostic formats.
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return redact_log_value(value)


class SensitiveDataFilter(logging.Filter):
    """Redact common secret shapes even when a caller logs them accidentally."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(("sqlalchemy.engine", "sqlalchemy.pool")):
            return False
        if not hasattr(record, "request_id"):
            record.request_id = request_id_context.get()
        record.msg = redact_log_value(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_safe_log_argument(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: _safe_log_argument(value) for key, value in record.args.items()
            }
        # Cached traceback text can come from a different handler's formatter.
        record.exc_text = None
        record.stack_info = None
        return True


class PrivacySafeConsoleFormatter(logging.Formatter):
    """Keep development logs readable without rendering exception payloads."""

    def format(self, record: logging.LogRecord) -> str:
        record.exc_text = None
        record.stack_info = None
        return super().format(record)

    def formatException(  # noqa: N802
        self, ei: tuple[type[BaseException], BaseException, object]
    ) -> str:
        return f"{ei[0].__name__}: [REDACTED_INTERNAL_EXCEPTION]"


class ProductionJSONFormatter(logging.Formatter):
    """Emit one flat JSON object per production log event."""

    SAFE_EXTRA_FIELDS = (
        "event",
        "request_id",
        "http_method",
        "route_template",
        "status_code",
        "duration_ms",
        "client_ip",
        "user_id",
        "business_id",
        "exception_type",
        "reason",
        "reservation_id",
        "outcome",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_log_value(getattr(record, "event", record.getMessage())),
        }
        for field in self.SAFE_EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = _safe_log_argument(value)
        if record.exc_info:
            payload["exception_type"] = record.exc_info[0].__name__
            payload["exception"] = "redacted_internal_exception"
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_logging(log_level: str, environment: str = "development") -> None:
    """Configure one privacy-safe console handler without duplicates."""
    root_logger = logging.getLogger()
    effective_level = "WARNING" if environment.lower() == "testing" else log_level
    root_logger.setLevel(effective_level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # SQL text and bind parameters can contain arbitrary customer records.
    for name in ("sqlalchemy.engine", "sqlalchemy.pool"):
        logging.getLogger(name).disabled = True
        logging.getLogger(name).setLevel(logging.CRITICAL)

    handler = next(
        (
            existing
            for existing in root_logger.handlers
            if getattr(existing, "_sou2ai_handler", False)
        ),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler()
        handler._sou2ai_handler = True
        root_logger.addHandler(handler)
    handler.filters.clear()
    handler.addFilter(SensitiveDataFilter())
    handler.setLevel(effective_level.upper())
    if environment.lower() == "production":
        handler.setFormatter(ProductionJSONFormatter())
    else:
        handler.setFormatter(
            PrivacySafeConsoleFormatter(
                "%(asctime)s | %(levelname)s | %(name)s | "
                "request_id=%(request_id)s | %(message)s"
            )
        )
    for existing in root_logger.handlers:
        if isinstance(existing, logging.StreamHandler):
            if not any(
                isinstance(item, SensitiveDataFilter) for item in existing.filters
            ):
                existing.addFilter(SensitiveDataFilter())
            existing.setFormatter(handler.formatter)
    # Uvicorn and RQ may install non-propagating handlers of their own.
    for named_logger in logging.root.manager.loggerDict.values():
        if isinstance(named_logger, logging.Logger):
            for existing in named_logger.handlers:
                if isinstance(existing, logging.StreamHandler):
                    if not any(
                        isinstance(item, SensitiveDataFilter)
                        for item in existing.filters
                    ):
                        existing.addFilter(SensitiveDataFilter())
                    existing.setFormatter(handler.formatter)
    # RequestSecurityMiddleware supplies route templates without raw query strings.
    logging.getLogger("uvicorn.access").disabled = True
