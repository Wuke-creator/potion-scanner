"""Structured JSON logger using structlog + stdlib logging.

Configures structlog as a formatter on stdlib logging handlers so all
existing `logging.getLogger(__name__)` calls across the codebase output
structured JSON (or colored console) with zero code changes.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog


def setup_logging(logging_config) -> None:
    """Configure structured logging from a LoggingConfig instance.

    Args:
        logging_config: LoggingConfig with level, file, and format fields.
    """
    log_level = getattr(logging, logging_config.level.upper(), logging.INFO)

    # Shared structlog processors for both renderers
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
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
