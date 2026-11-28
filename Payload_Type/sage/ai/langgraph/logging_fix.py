"""Logging fix for mythic_container.logging handler issues.

This module ensures the logger always has proper handlers and provides a fallback.
"""

import functools
import logging
import sys
from mythic_container.logging import logger as mythic_logger


def emergency_log(message: str):
    """Emergency logging that bypasses the normal logger."""
    print(f"[EMERGENCY LOG] {message}", file=sys.stderr, flush=True)


def ensure_logger_initialized():
    """Ensure mythic logger has handlers and is properly configured.

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


def install_mythic_client_log_guard():
    """Keep each Mythic scripting client from adding its own handler to the shared ``mythic`` logger.

    ``mythic.mythic_classes.Mythic.__init__`` adds a new stdout handler to the process-wide ``mythic`` logger,
    which ``mythic_container`` also logs through, on every ``mythic.login()``. Sage logs in at container start
    and for each new chat, so without this every container-log line repeats once per login. After the original
    constructor runs, the guard removes that client's own handler object and nothing else: the logger level the
    client sets, ``mythic_container``'s handlers and this module's fallback handler are left as they are.
    Idempotent; call once at service start, before the first login.
    """
    from mythic import mythic_classes

    original = mythic_classes.Mythic.__init__
    if getattr(original, "_sage_log_guard", False):
        return

    @functools.wraps(original)
    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        handler = getattr(self, "log_handler", None)
        if handler is not None:
            logging.getLogger("mythic").removeHandler(handler)

    __init__._sage_log_guard = True
    mythic_classes.Mythic.__init__ = __init__


def get_safe_logger():
    """Get a logger that is guaranteed to work.

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
