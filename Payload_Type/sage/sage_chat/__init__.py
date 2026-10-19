"""Native Mythic v4.0.0 chat container for Sage.

Importing this package registers ``SageChat`` via ``Chat.__subclasses__()`` (the SDK enumerates
subclasses at ``start_services``). ``main.py`` imports this package as the sole Sage service surface;
the former PayloadType container is not co-registered.
"""

from .service import SageChat
from .cancel_adapter import install_paused_cancel_handler

install_paused_cancel_handler()

__all__ = ["SageChat"]
