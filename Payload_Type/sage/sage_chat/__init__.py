"""Native Mythic v4.0.0 chat container for Sage.

Importing this package registers ``SageChat`` via ``Chat.__subclasses__()`` (the SDK enumerates
subclasses at ``start_services``). ``main.py`` imports ``sage_chat`` alongside ``container`` during
the Phase 1-3 transition (co-registration is supported); the PayloadType import is removed in Phase 4.
"""

from .service import SageChat

__all__ = ["SageChat"]
