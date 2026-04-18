"""Structured JSON logger using structlog + stdlib logging.

Configures structlog as a formatter on stdlib logging handlers so all
existing `logging.getLogger(__name__)` calls across the codebase output
structured JSON (or colored console) with zero code changes.

Includes a secret-redaction processor that catches any accidental token
leaks in log events or exception tracebacks.
"""

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog


def _build_redaction_patterns() -> list[tuple[re.Pattern, str]]:
    """Build compiled regex patterns to redact known secrets from log output.

    Reads token values from env vars at setup time and compiles exact-match
    patterns. If a token appears anywhere in a log event string, it gets
    replaced with [REDACTED].
    """
    patterns = []
    secret_env_vars = [
        "DISCORD_BOT_TOKEN",
        "DISCORD_OAUTH_CLIENT_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "OAUTH_STATE_SECRET",
        "WHOP_REFRESH_TOKEN_ENCRYPTION_KEY",
        # Added 2026-04-18 after security audit: these were leaking into
        # logs during error traces (Resend 401 responses, Whop API errors,
        # webhook auth failures respectively).
        "RESEND_API_KEY",
        "WHOP_API_KEY",
        "WHOP_WEBHOOK_SECRET",
        "ADMIN_WEBHOOK_SECRET",
    ]
    for var in secret_env_vars:
        val = os.getenv(var, "")
        if val and len(val) > 8:
            patterns.append(
                (re.compile(re.escape(val)), f"[REDACTED:{var}]")
            )
    return patterns


_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = []


def _redact_secrets(logger, method_name, event_dict):
    """structlog processor that strips known secret values from all fields."""
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            for pattern, replacement in _REDACT_PATTERNS:
                value = pattern.sub(replacement, value)
            event_dict[key] = value
    return event_dict


def setup_logging(logging_config) -> None:
    """Configure structured logging from a LoggingConfig instance.

    Args:
        logging_config: LoggingConfig with level, file, and format fields.
    """
    global _REDACT_PATTERNS
    _REDACT_PATTERNS = _build_redaction_patterns()

    log_level = getattr(logging, logging_config.level.upper(), logging.INFO)

    # Shared structlog processors for both renderers
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        _redact_secrets,
    ]

    # Choose renderer based on config
    if logging_config.format == "console":
        console_renderer = structlog.dev.ConsoleRenderer()
    else:
        console_renderer = structlog.processors.JSONRenderer()

    # File handler always uses JSON
    file_renderer = structlog.processors.JSONRenderer()

    # Console handler
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                console_renderer,
            ],
            foreign_pre_chain=shared_processors,
        )
    )

    # File handler with rotation (10 MB per file, keep 5 backups)
    log_path = Path(logging_config.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5,
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                file_renderer,
            ],
            foreign_pre_chain=shared_processors,
        )
    )

    # Configure stdlib root logger
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    root.setLevel(log_level)

    # Configure structlog to use stdlib
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
