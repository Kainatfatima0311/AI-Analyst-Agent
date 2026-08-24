"""Structured logging.

One run must be one filterable stream. Every line carries ``run_id``, and where applicable
``step_id``, ``node``, ``tool`` and ``query_id``, so a reviewer who was not present can
reconstruct why a run succeeded or failed (standard 4 in the design document).

Context is bound with structlog's contextvars, so a node binds ``run_id`` once and every log
line emitted anywhere beneath it — including inside the tools — carries it without threading a
logger through every call.

A redaction processor scrubs known secret values from every event before it is rendered
(control C9). It is applied to the message and to every field value, because the leak that
matters in practice is an exception string that happens to contain a DSN.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

REDACTED = "***REDACTED***"

_secrets: tuple[str, ...] = ()


def set_redaction_secrets(values: tuple[str, ...]) -> None:
    """Register the strings that must never appear in log output.

    Called once at startup with ``get_settings().secret_values``. Kept as module state rather
    than a processor argument so that ``configure_logging`` can be called before settings are
    loaded (for example in a test that has not built a Settings object yet).
    """
    global _secrets
    _secrets = tuple(sorted(values, key=len, reverse=True))


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        for secret in _secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v) for v in value)
    return value


def redact_secrets(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """structlog processor: remove registered secrets from every value in the event."""
    if not _secrets:
        return event_dict
    return {key: _scrub(value) for key, value in event_dict.items()}


def truncate_sql(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Keep log lines readable: SQL belongs in sql_audit, not repeated in full in the log.

    The full statement is always persisted, so nothing is lost by shortening it here.
    """
    for key in ("sql", "sql_text", "rewritten_sql"):
        value = event_dict.get(key)
        if isinstance(value, str) and len(value) > 400:
            event_dict[key] = value[:400] + f" ... [{len(value)} chars, see sql_audit]"
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog and the stdlib root logger to share one pipeline.

    structlog is routed *through* stdlib logging rather than writing directly, so that
    everything — our own events, uvicorn's access log, psycopg warnings — passes through the
    same processors and comes out in one consistent format instead of two interleaved ones.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        truncate_sql,
        redact_secrets,
    ]

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric_level)
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


@contextmanager
def bound(**values: Any) -> Iterator[None]:
    """Bind context for the duration of a block, then restore what was there before.

    Used by the graph to bind ``run_id`` for a whole run and ``step_id`` / ``node`` for each
    node, without leaking either into the next run handled by the same worker.
    """
    tokens = structlog.contextvars.bind_contextvars(**values)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def bind_run(run_id: str, **extra: Any) -> None:
    """Bind run-scoped context for the current task. Use ``clear_context`` when the run ends."""
    structlog.contextvars.bind_contextvars(run_id=run_id, **extra)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
