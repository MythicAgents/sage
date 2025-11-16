"""
Logging fix for mythic_container.logging handler issues.

This module ensures the logger always has proper handlers and provides a fallback.
"""

import logging
import sys
from mythic_container.logging import logger as mythic_logger


def emergency_log(message: str):
    """Emergency logging that bypasses the normal logger."""
    print(f"[EMERGENCY LOG] {message}", file=sys.stderr, flush=True)


def ensure_logger_initialized():
    """
    Ensure mythic logger has handlers and is properly configured.

    This fixes the issue where mythic_container.logging stops working
    after initial logs because handlers get removed or aren't added.
    """
    # Check if logger has handlers
    if not mythic_logger.handlers:

        # Add a stdout handler
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)

        # Use the same format as mythic_container
        formatter = logging.Formatter(
            "%(levelname) -4s %(asctime)s %(funcName) "
            "-3s %(lineno) -5d: %(message)s"
        )
        handler.setFormatter(formatter)

        mythic_logger.addHandler(handler)
        mythic_logger.setLevel(logging.DEBUG)

        # Force flush
        sys.stdout.flush()
        sys.stderr.flush()

        mythic_logger.info("✅ Logger reinitialized with stdout handler")
        force_flush_all_handlers()

    # Check if logger level is set
    if mythic_logger.level == 0 or mythic_logger.level > logging.DEBUG:
        mythic_logger.setLevel(logging.DEBUG)
        mythic_logger.info(f"✅ Logger level set to DEBUG")
        force_flush_all_handlers()


def get_safe_logger():
    """
    Get a logger that is guaranteed to work.

    Returns the mythic logger if it has handlers, otherwise returns a fallback.
    """
    ensure_logger_initialized()
    return mythic_logger


def force_flush_all_handlers():
    """Force flush all logger handlers."""
    for handler in mythic_logger.handlers:
        if hasattr(handler, 'flush'):
            handler.flush()
    sys.stdout.flush()
    sys.stderr.flush()
