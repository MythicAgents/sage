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

import re

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


# --------------------------------------------------------------------------------------------------
# Provider configuration rejections (OpenAI): recoverable by changing the chat's configuration.
# --------------------------------------------------------------------------------------------------

# Codes OpenAI documents for Daybreak access programs (Daybreak guide, 2026-11-20).
_ACCESS_PROGRAM_CODES = frozenset({
    "invalid_access_program", "unsupported_access_program", "access_program_not_enabled",
})
_CONFIG_PARAMS = frozenset({"model", "safety_identifier"})

# Reasoning effort: OpenAI names the field `reasoning_effort` (Chat Completions) or `reasoning.effort` (Responses).
_EFFORT_PARAMS = frozenset({"reasoning_effort", "reasoning.effort"})
_EFFORT_NAMED = re.compile(r"reasoning[ _.]effort", re.IGNORECASE)
_EFFORT_LABELS = {"none": "None", "minimal": "Minimal", "low": "Low", "medium": "Medium", "high": "High",
                  "xhigh": "Extra high", "max": "Maximum"}
_SUPPORTED_VALUES = re.compile(r"supported values (?:are|include)\s*:?\s*(?P<values>[^.\n]+)", re.IGNORECASE)
TOOLS_WITH_EFFORT = "tools_with_effort"
EFFORT_VALUE = "effort_value"


def _effort_rejection(status: int, code: str, param: str, message: str) -> str:
    """Which reasoning-effort rejection this is: tools with effort on this endpoint, an effort value, or ``""``.

    Identified by the structured field when the provider names one, else by the message naming reasoning effort.
    A request-content field (``input``, ``messages``, a tool schema) is a history or schema error, never an
    effort setting, even when its text mentions reasoning.
    """
    if status != 400:
        return ""
    if param and param not in _EFFORT_PARAMS and param != "tools":
        return ""
    if param not in _EFFORT_PARAMS and not _EFFORT_NAMED.search(message):
        return ""
    text = " ".join(message.casefold().split())
    if "function tools" in text or ("tools" in text and ("/chat/completions" in text or "/responses" in text)):
        return TOOLS_WITH_EFFORT
    if param in _EFFORT_PARAMS or code in {"unsupported_value", "unsupported_parameter"} or any(
        phrase in text for phrase in ("unsupported value", "does not support", "supported values", "not supported")
    ):
        return EFFORT_VALUE
    return ""


def _supported_efforts(message: str) -> list[str]:
    """Enabled effort labels the provider lists as supported, in its order; empty when it lists none."""
    found = _SUPPORTED_VALUES.search(message)
    if not found:
        return []
    words = re.findall(r"[a-z]+", found.group("values").casefold())
    return [_EFFORT_LABELS[word] for word in dict.fromkeys(words) if word in _EFFORT_LABELS and word != "none"]


def _status_error(exc: BaseException):
    try:
        import openai
    except ImportError:  # pragma: no cover - openai ships with the runtime
        return None
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, openai.APIStatusError):
            return current
        current = current.__cause__ or current.__context__
    return None


def _error_fields(error) -> tuple[int, str, str, str]:
    body = getattr(error, "body", None)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = body["error"]
    body = body if isinstance(body, dict) else {}
    code = str(getattr(error, "code", None) or body.get("code") or "")
    param = str(getattr(error, "param", None) or body.get("param") or "")
    message = str(body.get("message") or getattr(error, "message", "") or "")
    return int(getattr(error, "status_code", 0) or 0), code, param, message


def provider_configuration_rejection(exc: BaseException):
    """The provider rejection in ``exc``'s cause chain that a configuration change can fix, else ``None``.

    Authentication and permission failures, a model or endpoint the provider will not serve, the documented
    Daybreak access-program errors and a reasoning effort the model or endpoint refuses qualify. Rate limits,
    server errors and request-content errors (for example a malformed history) do not: changing the chat's
    route would not fix them.
    """
    error = _status_error(exc)
    if error is None:
        return None
    status, code, param, message = _error_fields(error)
    if status in {401, 403, 404}:
        return error
    if status == 400 and (
        code in _ACCESS_PROGRAM_CODES or param in _CONFIG_PARAMS or param.startswith("access_programs")
        or _effort_rejection(status, code, param, message)
    ):
        return error
    return None


def _endpoint_origin(endpoint: str) -> str:
    from urllib.parse import urlsplit

    text = str(endpoint or "").strip()
    if not text:
        return "provider default"
    parts = urlsplit(text)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}" if parts.scheme and host else "configured"


_KEEPS = (
    "This chat is still usable and keeps its conversation: changing the model (**Model** or `/model <name>`), "
    "OpenAI API, OpenAI Daybreak, OpenAI Safety Identifier or Reasoning Effort continues it."
)
_NEW_CHAT = (
    "Changing the API key, provider or API endpoint starts a new chat; this chat's conversation does not "
    "carry over. Changing the model (**Model** or `/model <name>`), OpenAI API, OpenAI Daybreak, OpenAI "
    "Safety Identifier or Reasoning Effort keeps it."
)
_RESPONSES_FOR_TOOLS = (
    "set **OpenAI API** to **Responses** in this chat's configuration (or the SAGE_OPENAI_API user secret or "
    "environment value), then send your request again; Responses keeps reasoning and tools. Setting "
    "**Reasoning Effort** to **None** also avoids this error, but only on models that accept None, and it turns "
    "reasoning off"
)


def _effort_action(message: str) -> str:
    supported = _supported_efforts(message)
    if len(supported) > 1:
        values = f"one of the values the provider lists: {', '.join(supported[:-1])} or {supported[-1]}"
    elif supported:
        values = f"the value the provider lists: {supported[0]}"
    else:
        values = "a value this model supports"
    return (
        f"set **Reasoning Effort** to a supported value in this chat's configuration ({values}), then send your "
        "request again. Leaving the field blank inherits the SAGE_REASONING_EFFORT user secret, then the "
        "environment, then the provider's default for the model; it is not the same as **None**, which "
        "Sage sends as an explicit value"
    )
_MODEL_ACTION = (
    "change **Model** to a model this API key can use (or run `/model <name>`), then send your request again; if "
    "access to this model is granted at OpenAI instead, send your request again here"
)


def _configuration_action(status: int, code: str, param: str, message: str, api: str) -> tuple[str, bool]:
    """``(action, keeps_conversation)``: what to change, and whether that change continues this chat.

    The model and the OpenAI route settings (API, Daybreak, safety identifier) continue an idle chat in place. A
    new key, provider or endpoint starts a new session, and a fix made in the OpenAI project without touching
    Sage's settings is retried here as it is.
    """
    if code == "invalid_access_program":
        return ("set **OpenAI Daybreak** to the value named in the error (Off, Blue or Red), or the "
                "SAGE_OPENAI_ACCESS_PROGRAM user secret or environment value, then send your request again", True)
    if code == "unsupported_access_program":
        return ("set **OpenAI Daybreak** to **Off** to keep this model, or change **Model** (or run `/model <name>`) "
                "to one that supports this Daybreak program, then send your request again", True)
    effort = _effort_rejection(status, code, param, message)
    if effort == TOOLS_WITH_EFFORT and api != "responses":
        return (_RESPONSES_FOR_TOOLS, True)
    if effort:
        return (_effort_action(message), True)
    if code == "access_program_not_enabled":
        return ("enable this Daybreak program for the API key's OpenAI project (for GPT-6 Cyber, also allowlist the "
                "sending IP address), then send your request again here; using another project's API key "
                "starts a new chat", False)
    if status == 401:
        return ("replace the API key (the API_KEY field, user secret or environment value); if the key itself is "
                "fixed at OpenAI, send your request again here", False)
    if status == 403:
        return ("give the API key's OpenAI project access to this model and Daybreak program, then send your "
                "request again here; a different API key starts a new chat", False)
    if status == 404 and param == "model" and "responses" in message.casefold() and api != "responses":
        return ("set **OpenAI API** to **Responses** in this chat's configuration (or the SAGE_OPENAI_API "
                "user secret or environment value), then send your request again", True)
    if param == "safety_identifier":
        return ("set **OpenAI Safety Identifier** to a stable pseudonym for the person using this chat, then send "
                "your request again", True)
    if code == "model_not_found" or param == "model":
        return (_MODEL_ACTION, True)
    return ("check that the API key's project has access to this model and that the model name and API Endpoint "
            "are right; change **Model** (or run `/model <name>`) to keep this chat; if access is granted at "
            "OpenAI, send your request again here", False)


def provider_configuration_notice(exc: BaseException, profile) -> str:
    """Operator-facing text for a configuration rejection; ``""`` for any other error.

    Names the provider's status, code, parameter and message, the route Sage used with each setting's source,
    and the configuration action. Never includes the API key or the safety identifier's value.
    """
    error = provider_configuration_rejection(exc)
    if error is None:
        return ""
    status, code, param, message = _error_fields(error)

    def value(name: str, default: str = "") -> str:
        return str(getattr(profile, name, default) or default) if profile is not None else default

    def source(name: str) -> str:
        getter = getattr(profile, "source_for", None)
        return str(getter(name)) if callable(getter) else "unknown"

    from ai import openai_api

    api = value("openai_api")
    program = value("openai_access_program")
    if api == openai_api.RESPONSES:
        daybreak = (openai_api.ACCESS_PROGRAM_LABELS.get(program, program) + f" ({source('openai_access_program')})"
                    if program else "Off (Sage default)")
    else:
        daybreak = "not sent (Chat Completions or adapter route)"
    details = ", ".join(part for part in (f"HTTP {status}", f"code `{code}`" if code else "",
                                           f"param `{param}`" if param else "") if part)
    api_label = openai_api.API_LABELS.get(api, api) + (f" ({source('openai_api')})" if api else "")
    effort_value = value("reasoning_effort")
    effort = (f"{_EFFORT_LABELS.get(effort_value, effort_value)} ({source('reasoning_effort')})"
              if effort_value else "not sent (provider default)")
    route = (
        f"provider {value('provider', 'openai')} · model `{value('model')}` · OpenAI API: {api_label} · "
        f"OpenAI Daybreak: {daybreak} · OpenAI Safety Identifier: "
        f"{'configured' if value('openai_safety_identifier') else 'not configured'} · "
        f"Reasoning Effort: {effort} · endpoint {_endpoint_origin(value('api_endpoint'))}"
    )
    action, keeps = _configuration_action(status, code, param, message, api)
    return (
        f"The model provider rejected this request ({details}): {message}\n\n"
        f"Route used: {route}.\n\n"
        f"To fix it, {action}. {_KEEPS if keeps else _NEW_CHAT} "
        "Sage did not change the model, provider or endpoint."
    )


def _provider_message(exc: BaseException) -> tuple[str, str]:
    """``(status, message)`` from the first provider error in ``exc``'s cause chain that carries them."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        body = getattr(current, "body", None)
        if isinstance(body, dict):
            inner = body.get("error") if isinstance(body.get("error"), dict) else body
            message = inner.get("message") if isinstance(inner, dict) else None
            if isinstance(message, str) and message.strip():
                status = getattr(current, "status_code", None)
                return (f"HTTP {status}" if status else ""), message.strip()
        current = current.__cause__ or current.__context__
    status = getattr(exc, "status_code", None)
    return (f"HTTP {status}" if status else ""), operator_error_text(exc)


def unproven_model_notice(exc: BaseException, profile) -> str:
    """Operator-facing text for a failure before this chat's Model ever answered or ran anything, any provider.

    Names the provider, the model value and its source, the provider's message and the fix. Never includes the
    API key or other credentials.
    """
    status, message = _provider_message(exc)

    def value(name: str, default: str = "") -> str:
        return str(getattr(profile, name, default) or default) if profile is not None else default

    def source(name: str) -> str:
        getter = getattr(profile, "source_for", None)
        return str(getter(name)) if callable(getter) else "unknown"

    details = f" ({status})" if status else ""
    return (
        f"The model provider rejected this chat's request before the model answered{details}: {message}\n\n"
        f"Route used: provider {value('provider', 'unknown')} · model `{value('model')}` ({source('model')}) · "
        f"endpoint {_endpoint_origin(value('api_endpoint'))}.\n\n"
        "To fix it, change **Model** to a model this provider and key can use, or run `/model <name>`, then send "
        "your request again. This chat is still usable and keeps its conversation; nothing was run."
    )
