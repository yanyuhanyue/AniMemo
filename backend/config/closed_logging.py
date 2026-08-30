from __future__ import annotations

import logging
import re
import secrets
import threading
import weakref

from .api_errors import correlation_id_for

_EXCEPTION_CLASS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_CORRELATION_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_STANDARD_RECORD_FIELDS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)
_CLOSED_FIELDS = frozenset(
    {
        "event",
        "stage",
        "status_code",
        "correlation_id",
        "exception_class",
    }
)
_SANITIZED_RECORDS: weakref.WeakKeyDictionary[logging.LogRecord, tuple[str, int, str, str]] = (
    weakref.WeakKeyDictionary()
)
_SANITIZED_RECORDS_LOCK = threading.Lock()


def _exception_class(exc_info: object) -> str:
    try:
        candidate: object | None = None
        if isinstance(exc_info, tuple) and exc_info:
            candidate = exc_info[0]
        elif isinstance(exc_info, BaseException):
            candidate = type(exc_info)
        name = getattr(candidate, "__name__", "")
    except Exception:
        return "RequestBoundaryError"
    if isinstance(name, str) and _EXCEPTION_CLASS_PATTERN.fullmatch(name):
        return name
    return "RequestBoundaryError"


def _status_code(value: object) -> int:
    try:
        normalized = int(value)
    except Exception:
        return 500
    return normalized if 100 <= normalized <= 599 else 500


def _event_for(logger_name: object) -> str:
    if isinstance(logger_name, str):
        if logger_name.startswith("django.security"):
            return "django_security_boundary"
        if logger_name.startswith("django.server"):
            return "django_server_boundary"
    return "django_request_boundary"


def _server_correlation_id(request: object | None) -> str:
    try:
        candidate = correlation_id_for(request)
    except Exception:
        candidate = ""
    if isinstance(candidate, str) and _CORRELATION_PATTERN.fullmatch(candidate):
        return candidate
    try:
        return secrets.token_hex(16)
    except Exception:
        return "0" * 32


def _closed_values(
    record: logging.LogRecord,
    authoritative: tuple[str, int, str, str] | None,
) -> tuple[str, int, str, str]:
    values = record.__dict__
    if authoritative is None:
        event = _event_for(record.name)
        status_code = _status_code(values.get("status_code"))
        correlation_id = _server_correlation_id(values.get("request"))
        exception_class = _exception_class(values.get("exc_info"))
    else:
        event, status_code, correlation_id, exception_class = authoritative

    for key in tuple(values):
        if key not in _STANDARD_RECORD_FIELDS and key not in _CLOSED_FIELDS:
            values.pop(key, None)

    record.msg = "django_boundary_event"
    record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    record.pathname = ""
    record.filename = ""
    record.module = ""
    record.funcName = ""
    record.lineno = 0
    record.thread = 0
    record.threadName = ""
    record.process = 0
    record.processName = ""
    record.taskName = ""
    record.event = event
    record.stage = "http_request_boundary"
    record.status_code = status_code
    record.correlation_id = correlation_id
    record.exception_class = exception_class
    return event, status_code, correlation_id, exception_class


def _take_closed_values(record: logging.LogRecord) -> tuple[str, int, str, str] | None:
    with _SANITIZED_RECORDS_LOCK:
        return _SANITIZED_RECORDS.pop(record, None)


def _remember_closed_values(record: logging.LogRecord, values: tuple[str, int, str, str]) -> None:
    with _SANITIZED_RECORDS_LOCK:
        _SANITIZED_RECORDS[record] = values


class ClosedDjangoBoundaryFilter(logging.Filter):
    """Replace Django boundary logs with a fixed, non-replayable schema."""

    def __init__(self, *, finalize: bool = False) -> None:
        super().__init__()
        self._finalize = bool(finalize)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Logging may be configured repeatedly in one long-lived process,
            # leaving more than one prepare filter on the same logger. Every
            # pass consumes the out-of-band authoritative tuple; prepare passes
            # put it back for the next filter and the final handler consumes it.
            authoritative = _take_closed_values(record)
            closed = _closed_values(record, authoritative)
            if not self._finalize:
                _remember_closed_values(record, closed)
        except Exception:
            try:
                closed = _closed_values(
                    record,
                    ("django_logging_boundary_failure", 500, _server_correlation_id(None), "LoggingBoundaryFailure"),
                )
                if not self._finalize:
                    _remember_closed_values(record, closed)
            except Exception:
                # A LogRecord created by the standard logging package always
                # supports these assignments. This last branch deliberately
                # emits no untrusted value even for a hostile synthetic record.
                record.msg = "django_boundary_event"
                record.args = ()
                record.exc_info = None
                record.exc_text = None
                record.stack_info = None
        return True
