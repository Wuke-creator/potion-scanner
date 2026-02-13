"""Tests for structlog integration."""

import json
import logging
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.utils.logger import setup_logging


@dataclass
class FakeLoggingConfig:
    level: str = "INFO"
    file: str = ""
    format: str = "json"


class TestStructuredLogging:
    """Verify structlog produces correct JSON output via stdlib loggers."""

    def test_json_output_has_required_fields(self, tmp_path):
        log_file = tmp_path / "test.log"
        config = FakeLoggingConfig(file=str(log_file), format="json")
        setup_logging(config)

        logger = logging.getLogger("test_json_fields")
        logger.info("hello world")

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) >= 1
        record = json.loads(lines[-1])
        assert record["event"] == "hello world"
        assert record["level"] == "info"
        assert "timestamp" in record
        assert record["logger"] == "test_json_fields"

    def test_console_format_does_not_crash(self, tmp_path):
        log_file = tmp_path / "test.log"
        config = FakeLoggingConfig(file=str(log_file), format="console")
        setup_logging(config)

        logger = logging.getLogger("test_console")
        logger.info("console message")

        # File handler still writes JSON even in console mode
        lines = log_file.read_text().strip().splitlines()
        assert len(lines) >= 1
        record = json.loads(lines[-1])
        assert record["event"] == "console message"

    def test_log_level_filtering(self, tmp_path):
        log_file = tmp_path / "test.log"
        config = FakeLoggingConfig(level="WARNING", file=str(log_file), format="json")
        setup_logging(config)

        logger = logging.getLogger("test_level")
        logger.info("should be filtered")
        logger.warning("should appear")

        lines = log_file.read_text().strip().splitlines()
        assert len(lines) >= 1
        # Only the warning should be present
        events = [json.loads(line)["event"] for line in lines]
        assert "should appear" in events
        assert "should be filtered" not in events

    def test_exception_info_included(self, tmp_path):
        log_file = tmp_path / "test.log"
        config = FakeLoggingConfig(file=str(log_file), format="json")
        setup_logging(config)

        logger = logging.getLogger("test_exc")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("caught error")

        content = log_file.read_text()
        assert "ValueError" in content
        assert "boom" in content

    def test_file_handler_is_rotating(self, tmp_path):
        log_file = tmp_path / "test.log"
        config = FakeLoggingConfig(file=str(log_file), format="json")
        setup_logging(config)

        root = logging.getLogger()
        file_handlers = [
            h for h in root.handlers if isinstance(h, RotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        handler = file_handlers[0]
        assert handler.maxBytes == 10 * 1024 * 1024  # 10 MB
        assert handler.backupCount == 5
