"""Operator-safe rendering of exception text.

`BaseException.__str__` returns `str(args[0])` when the exception carries exactly one
argument, so an exception whose payload is an *object* renders that object's repr.
LangGraph's control-flow exceptions are built exactly that way: `ParentCommand(command)`
stringifies to the entire ``Command(update={'messages': [AIMessage(content="...")]})``
repr. When such an exception escapes the graph, any surface that shows `str(exc)` to the
operator publishes that repr — including the full text of every message it carried.

The rule this module enforces is a class rule, not a patch for one exception type: no
operator-facing text is ever produced by stringifying a non-string exception payload.
"""

from __future__ import annotations

SUPPRESSED_SUFFIX = "internal error (details suppressed from chat; see Sage logs)"


def operator_error_text(exc: BaseException) -> str:
    """Return text for `exc` that is safe to render into an operator-facing channel.

    String payloads pass through verbatim — including a string that merely *looks* like an
    object repr, since that is genuine error information an operator needs. A non-string
    payload is suppressed and only the exception type is named; the full detail belongs in
    the logs, not the chat channel.
    """

    args = getattr(exc, "args", ())
    try:
        payload_is_text = all(isinstance(arg, str) for arg in args)
    except TypeError:  # pragma: no cover - args is always iterable on real exceptions
        payload_is_text = False

    if payload_is_text:
        return str(exc)
    return f"{type(exc).__name__}: {SUPPRESSED_SUFFIX}"
