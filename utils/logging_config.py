"""
Structured logging configuration for LEGO Train Controller.

Provides JSON and text formatters with contextual information.
Replaces print() statements throughout the codebase.
"""

import logging
import os
import sys
import json
from datetime import datetime
from typing import Any, Dict, Optional
from config import get_settings


class JSONFormatter(logging.Formatter):
    """
    JSON formatter for structured logging.

    Outputs log records as JSON objects with consistent fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_data: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Add extra fields if present
        if hasattr(record, "extra"):
            log_data.update(record.extra)

        return json.dumps(log_data)


class TextFormatter(logging.Formatter):
    """
    Human-readable text formatter for development.

    Outputs log records in a readable format with colors (if terminal supports it).
    """

    # ANSI color codes
    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
        "RESET": "\033[0m",  # Reset
    }

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as colored text."""
        # Check if output supports colors
        use_colors = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()

        level_name = record.levelname
        if use_colors:
            color = self.COLORS.get(level_name, self.COLORS["RESET"])
            level_str = f"{color}{level_name:8}{self.COLORS['RESET']}"
        else:
            level_str = f"{level_name:8}"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        location = f"{record.module}.{record.funcName}:{record.lineno}"

        message = record.getMessage()

        # Add exception info if present
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)

        return f"{timestamp} | {level_str} | {location:40} | {message}"


def setup_logging(
    level: Optional[str] = None,
    log_format: Optional[str] = None,
    log_file: Optional[str] = None,
) -> logging.Logger:
    """
    Configure structured logging for the application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_format: Log format (json or text)
        log_file: Optional file path for logging

    Returns:
        logging.Logger: Configured root logger
    """
    settings = get_settings()

    # Use provided values or fall back to settings
    level = level or settings.log_level
    log_format = log_format or settings.log_format
    log_file = log_file or settings.log_file

    # Convert string level to logging constant
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove existing handlers
    root_logger.handlers.clear()

    # Choose formatter
    if log_format.lower() == "json":
        formatter = JSONFormatter()
    else:
        formatter = TextFormatter()

    # Console handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler (if specified)
    if log_file:
        try:
            file_handler = logging.FileHandler(os.path.expanduser(log_file))
            file_handler.setLevel(numeric_level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
            root_logger.info(f"Logging to file: {log_file}")
        except Exception as e:
            root_logger.error(f"Failed to create log file handler: {e}")

    root_logger.info(
        f"Logging configured: level={level}, format={log_format}, file={log_file or 'none'}"
    )

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a specific module.

    Args:
        name: Logger name (typically __name__)

    Returns:
        logging.Logger: Logger instance
    """
    return logging.getLogger(name)


# Context manager for adding extra context to logs
class LogContext:
    """
    Context manager for adding extra fields to log records.

    Usage:
        with LogContext(hub_id=12, channel=21):
            logger.info("Command sent")
            # Output includes hub_id and channel fields
    """

    def __init__(self, **kwargs):
        """Initialize with extra context fields."""
        self.extra = kwargs
        self.old_factory = None

    def __enter__(self):
        """Add extra fields to log records."""
        self.old_factory = logging.getLogRecordFactory()

        def record_factory(*args, **kwargs):
            record = self.old_factory(*args, **kwargs)
            record.extra = self.extra
            return record

        logging.setLogRecordFactory(record_factory)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Restore original log record factory."""
        logging.setLogRecordFactory(self.old_factory)
