"""Native Mythic v4.0.0 chat container for Sage.

Importing this package registers ``SageChat`` via ``Chat.__subclasses__()`` (the SDK enumerates
subclasses at ``start_services``). ``main.py`` imports this package as the sole Sage service surface;
the former PayloadType container is not co-registered.
"""

from .service import SageChat

__all__ = ["SageChat"]
