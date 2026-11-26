"""OpenAI-family providers, the API each chat calls, Daybreak and the safety identifier: one owner.

Both model factories (the ordinary ``Model._get_base_chat_model`` and the profile-based
``init_chat_model_from_profile``), configuration validation, the provider-rejection notice and the debug line read
the family, the endpoint rule and the effective API from here, so a chat's route cannot mean one request shape in
one place and another elsewhere.

Providers: ``openai`` (OpenAI), ``litellm`` (LiteLLM Gateway) and ``openai_compatible`` (Custom, any
OpenAI-compatible server) all build ``ChatOpenAI``; LangChain only ever sees ``openai``. A gateway or custom server
needs an API Endpoint before any request, so its key is never sent to OpenAI.

Semantics:
- An explicit OpenAI API (``chat_completions`` or ``responses``) always wins, on every family provider.
- With OpenAI API blank at every layer, ``openai`` on OpenAI's own endpoint uses Responses. "OpenAI's own" is
  ``https`` with host ``api.openai.com`` or a subdomain of it (data residency, Private Link), and the endpoint is
  the one the adapter would call: Sage's API Endpoint, else ``OPENAI_API_BASE``, else an enabled
  ``LANGSMITH_GATEWAY``, else ``OPENAI_BASE_URL``, else ``https://api.openai.com/v1``. Everything else (a gateway,
  a custom server, ``openai`` pointed at any other endpoint) uses Chat Completions, which is what such a
  configuration sent before. The adapter is never left to choose: langchain-openai picks Responses on its own for
  some model names, and such a request would carry none of Sage's Responses settings below.
- Responses is stateless (``store=False``) with encrypted reasoning returned so it can be replayed with the history
  Sage already keeps, and always names the access program. Unset means **Off** (``standard``): Sage never inherits
  a project's default Daybreak program. ``access_programs`` is documented for Responses only, so it is never sent
  on Chat Completions.
- The safety identifier is optional (OpenAI recommends it; no model requires it), operator-supplied and sent
  only when configured. Sage never derives one.
- Sage never asks for more than one completion, so a Chat Completions response with several assistant choices is
  one reply a gateway split (one choice per output item). ``install_choice_fold`` folds it into one choice before
  LangChain keeps the first: every text and tool call in choice order, as a fixed gateway sends it. Any other shape
  reaches LangChain unchanged; an unrecognized multi-choice shape is logged without its content.
"""

from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

API_ENV = "SAGE_OPENAI_API"
ACCESS_PROGRAM_ENV = "SAGE_OPENAI_ACCESS_PROGRAM"
SAFETY_IDENTIFIER_ENV = "SAGE_OPENAI_SAFETY_IDENTIFIER"

OPENAI = "openai"
LITELLM = "litellm"
OPENAI_COMPATIBLE = "openai_compatible"
FAMILY = (OPENAI, LITELLM, OPENAI_COMPATIBLE)
PROVIDER_LABELS = {OPENAI: "OpenAI", LITELLM: "LiteLLM Gateway", OPENAI_COMPATIBLE: "Custom (OpenAI-compatible)"}
# The adapter LangChain builds for every family provider.
ADAPTER_PROVIDER = OPENAI

CHAT_COMPLETIONS = "chat_completions"
RESPONSES = "responses"
API_VALUES = (CHAT_COMPLETIONS, RESPONSES)
API_LABELS = {CHAT_COMPLETIONS: "Chat Completions", RESPONSES: "Responses"}

STANDARD = "standard"
ACCESS_PROGRAM_VALUES = (STANDARD, "daybreak_blue", "daybreak_red")
ACCESS_PROGRAM_LABELS = {STANDARD: "Off", "daybreak_blue": "Blue", "daybreak_red": "Red"}

REASONING_INCLUDE = "reasoning.encrypted_content"

OPENAI_DEFAULT_ENDPOINT = "https://api.openai.com/v1"
_OPENAI_HOST = "api.openai.com"
_LANGSMITH_GATEWAY_ENV = "LANGSMITH_GATEWAY"
_LANGSMITH_GATEWAY_DEFAULT = "https://gateway.smith.langchain.com"
_FALSE_VALUES = ("false", "0", "no")
_TRUE_VALUES = ("true", "1", "yes")


def normalize_provider(provider: Any) -> str:
    return str(provider or "").strip().casefold()


def is_family(provider: Any) -> bool:
    """Whether ``provider`` builds ``ChatOpenAI`` and takes the OpenAI route settings."""
    return normalize_provider(provider) in FAMILY


def adapter_provider(provider: Any) -> str:
    """The provider name handed to LangChain: ``openai`` for the whole family, else the provider unchanged."""
    return ADAPTER_PROVIDER if is_family(provider) else str(provider or "")


@dataclass(frozen=True)
class Endpoint:
    url: str
    source: str  # "API Endpoint", "OPENAI_API_BASE", "LANGSMITH_GATEWAY", "OPENAI_BASE_URL" or "default"


def effective_endpoint(api_endpoint: Any = "", environ: Mapping[str, str] | None = None) -> Endpoint:
    """The base URL the adapter will call, in the adapter's own precedence."""
    env = os.environ if environ is None else environ
    configured = str(api_endpoint or "").strip()
    if configured:
        return Endpoint(configured, "API Endpoint")
    adapter_base = env.get("OPENAI_API_BASE") or ""
    if adapter_base:
        return Endpoint(adapter_base, "OPENAI_API_BASE")
    gateway = str(env.get(_LANGSMITH_GATEWAY_ENV) or "")
    if gateway and gateway.lower() not in _FALSE_VALUES:
        base = _LANGSMITH_GATEWAY_DEFAULT if gateway.lower() in _TRUE_VALUES else gateway.rstrip("/")
        return Endpoint(f"{base}/openai/v1", _LANGSMITH_GATEWAY_ENV)
    sdk_base = env.get("OPENAI_BASE_URL") or ""
    if sdk_base:
        return Endpoint(sdk_base, "OPENAI_BASE_URL")
    return Endpoint(OPENAI_DEFAULT_ENDPOINT, "default")


def is_openai_endpoint(url: Any) -> bool:
    """``https`` with host ``api.openai.com`` or one of its subdomains; anything else is not OpenAI's own."""
    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return parts.scheme.casefold() == "https" and (host == _OPENAI_HOST or host.endswith("." + _OPENAI_HOST))


def default_api(provider: Any, api_endpoint: Any = "", environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """``(api, reason)`` a blank OpenAI API means for this provider and endpoint; ``("", "")`` off the family."""
    name = normalize_provider(provider)
    if name == OPENAI:
        if is_openai_endpoint(effective_endpoint(api_endpoint, environ).url):
            return RESPONSES, "for OpenAI's API"
        return CHAT_COMPLETIONS, "for an endpoint other than OpenAI's"
    if name in FAMILY:
        return CHAT_COMPLETIONS, f"for {PROVIDER_LABELS[name]}"
    return "", ""


@dataclass(frozen=True)
class OpenAIRoute:
    api: str = ""  # the explicit selection, if any
    access_program: str = ""
    safety_identifier: str = ""
    default: str = ""  # what a blank ``api`` means on this provider and endpoint
    default_reason: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api or self.access_program or self.safety_identifier)

    @property
    def effective_api(self) -> str:
        return self.api or self.default

    @property
    def effective_access_program(self) -> str:
        """The program a request names: the selection, or Off on Responses; none on Chat Completions."""
        if self.effective_api != RESPONSES:
            return ""
        return self.access_program or STANDARD


def api_label(route: OpenAIRoute, source: str = "") -> str:
    """The effective API and where it came from, e.g. ``Responses (default) for OpenAI's API``."""
    if route.api:
        label = API_LABELS.get(route.api, route.api)
        return f"{label} ({source})" if source else label
    if not route.default:
        return "not applicable"
    return f"{API_LABELS[route.default]} (default) {route.default_reason}"


def validate_route(
    *,
    provider: str,
    model: str,
    api: Any = "",
    access_program: Any = "",
    safety_identifier: Any = "",
    endpoint: Any = "",
    environ: Mapping[str, str] | None = None,
) -> OpenAIRoute:
    """Return the normalized route, or raise ``ValueError`` naming the setting and the action to take.

    ``endpoint`` is Sage's resolved API Endpoint (blank when unset at every layer).
    """
    api_value = str(api or "").strip()
    program = str(access_program or "").strip()
    identifier = str(safety_identifier or "").strip()
    name = normalize_provider(provider)
    if api_value and api_value not in API_VALUES:
        raise ValueError(
            f"{API_ENV} must be chat_completions or responses (OpenAI API: Chat Completions or Responses); "
            "leave it blank to keep the provider's default API."
        )
    if program and program not in ACCESS_PROGRAM_VALUES:
        raise ValueError(
            f"{ACCESS_PROGRAM_ENV} must be standard, daybreak_blue or daybreak_red (OpenAI Daybreak: Off, Blue or Red)."
        )
    if (api_value or program or identifier) and name not in FAMILY:
        raise ValueError(
            f"{API_ENV}, {ACCESS_PROGRAM_ENV} and {SAFETY_IDENTIFIER_ENV} require the OpenAI, LiteLLM Gateway or "
            "Custom (OpenAI-compatible) model provider."
        )
    if name in (LITELLM, OPENAI_COMPATIBLE) and not str(endpoint or "").strip():
        raise ValueError(
            f"{PROVIDER_LABELS[name]} needs an API Endpoint: set API Endpoint to the "
            f"{'gateway' if name == LITELLM else 'server'}'s base URL (for example https://host:port or "
            "https://host/v1), or the API_ENDPOINT user secret or environment value. Sage sends no request without "
            "it, so this provider's key never goes to OpenAI."
        )
    default, reason = default_api(name, endpoint, environ)
    route = OpenAIRoute(api=api_value, access_program=program, safety_identifier=identifier, default=default,
                        default_reason=reason)
    if program and route.effective_api != RESPONSES:
        raise ValueError(
            f"OpenAI Daybreak ({ACCESS_PROGRAM_ENV}={program}) requires OpenAI API = Responses. Set OpenAI API to "
            "Responses or clear OpenAI Daybreak; Sage does not change the API for you."
        )
    return route


def adapter_options(route: OpenAIRoute) -> dict[str, Any]:
    """``ChatOpenAI`` keyword arguments for a validated family route. Every route pins the API."""
    # Bound only the connect phase (langchain-openai otherwise sends no timeout); the node watchdog owns "no answer".
    options: dict[str, Any] = {"timeout": httpx.Timeout(None, connect=10.0)}
    extra_body: dict[str, Any] = {}
    if route.effective_api == RESPONSES:
        options.update(
            use_responses_api=True,
            output_version="responses/v1",
            store=False,
            include=[REASONING_INCLUDE],
        )
        extra_body["access_programs"] = {"cyber": route.effective_access_program}
    else:
        options["use_responses_api"] = False
    if route.safety_identifier:
        extra_body["safety_identifier"] = route.safety_identifier
    if extra_body:
        options["extra_body"] = extra_body
    return options


def route_from_configurable(provider: str, model: str, configurable: dict[str, Any]) -> OpenAIRoute:
    """Validate the projection a ``Model`` receives in ``config['configurable']``."""
    return validate_route(
        provider=provider,
        model=model,
        api=configurable.get("openai_api"),
        access_program=configurable.get("openai_access_program"),
        safety_identifier=configurable.get("openai_safety_identifier"),
        endpoint=configurable.get("base_url"),
    )


# One reply split across Chat Completions choices.

_FOLD_MARK = "_sage_split_choice_fold"
_FOLDABLE_FINISH = (None, "stop", "tool_calls")


def _unfoldable(choices: list, n: int | None) -> str:
    """Why these choices are not one split reply, or "" when they are. Never names their content."""
    if n is not None and n > 1:
        return "more than one completion was requested"
    seen: set = set()
    calls = 0
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            return "a choice has no message"
        if message.get("role") != "assistant":
            return "a choice is not from the assistant"
        if message.get("parsed") is not None:
            return "a choice carries parsed output"
        if message.get("refusal") is not None:
            return "a choice carries a refusal"
        if choice.get("finish_reason") not in _FOLDABLE_FINISH:
            return "a choice did not finish normally"
        if message.get("function_call") is not None or message.get("audio") is not None:
            return "a choice carries a legacy function call or audio"
        if message.get("content") is not None and not isinstance(message.get("content"), str):
            return "a choice's content is not text"
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            return "a choice's tool calls are not a list"
        for call in tool_calls:
            call_id = call.get("id") if isinstance(call, dict) else None
            if call_id in seen:
                return "a tool-call id repeats"
            seen.add(call_id)
        calls += len(tool_calls)
    return "" if calls else "no choice carries a tool call"


def _joined(values: list) -> str | None:
    texts = [value for value in values if isinstance(value, str) and value.strip()]
    return "\n\n".join(texts) if texts else None


def _fold(data: dict) -> dict:
    positions = list(enumerate(data["choices"]))
    ordered = [choice for _position, choice in sorted(positions, key=lambda item: (
        item[1].get("index") if isinstance(item[1].get("index"), int) and not isinstance(item[1].get("index"), bool)
        else item[0], item[0]))]
    messages = [choice["message"] for choice in ordered]
    message = {**messages[0], "role": "assistant", "refusal": None,
               "content": _joined([m.get("content") for m in messages]),
               "tool_calls": [call for m in messages for call in m.get("tool_calls") or []]}
    reasoning = _joined([m.get("reasoning_content") for m in messages])
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    annotations = [m["annotations"] for m in messages if isinstance(m.get("annotations"), list)]
    if annotations:
        message["annotations"] = [item for group in annotations for item in group]
    return {**data, "choices": [{**ordered[0], "index": 0, "finish_reason": "tool_calls", "message": message}]}


def fold_split_choices(response: Any, *, n: int | None) -> Any:
    """``response`` with one reply split across choices folded into ``choices[0]``; otherwise ``response`` itself.

    The fold follows LiteLLM's own fix for its Responses-to-Chat bridge: texts joined in choice order, every tool
    call in choice order, reasoning and annotations kept, ``finish_reason`` ``tool_calls``. A response with fewer
    than two choices, or with an ``error``, is returned untouched for LangChain to read (or reject) as before.
    """
    choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
    if not isinstance(choices, list) or len(choices) < 2:
        return response
    if isinstance(response, dict):
        if response.get("error"):
            return response
        data = response
    elif any(getattr(getattr(choice, "message", None), "parsed", None) is not None for choice in choices):
        logger.warning("Sage kept the first of %d choices in a provider reply unfolded: %s.", len(choices),
                       "a choice carries parsed output")
        return response
    else:
        try:
            data = response.model_dump(warnings=False)
        except Exception:  # noqa: BLE001 - an unreadable SDK object is left for LangChain to read as before
            logger.warning("Sage kept the first of %d choices in a provider reply unfolded: %s.", len(choices),
                           "the response could not be read")
            return response
    reason = _unfoldable(data.get("choices") or [], n)
    if reason:
        logger.warning("Sage kept the first of %d choices in a provider reply unfolded: %s.", len(choices), reason)
        return response
    folded = _fold(data)
    if isinstance(response, dict):
        return folded
    try:
        # The same SDK type, so LangChain copies choices[0]'s refusal as it does for a single-choice reply.
        return type(response).model_validate(folded)
    except Exception:  # noqa: BLE001 - LangChain reads the folded dict as well
        return folded


def install_choice_fold() -> None:
    """Fold a split reply before ``ChatOpenAI`` makes one generation per choice. Idempotent; once per process.

    LangChain's ``invoke`` keeps only the first generation, so without this a tool call the gateway put in a later
    choice never reaches Sage. Wraps ``BaseChatOpenAI._create_chat_result`` (its Chat Completions parser, which
    ``AzureChatOpenAI`` also reaches through ``super()``); the Responses API never reaches it.
    """
    from langchain_openai.chat_models.base import BaseChatOpenAI

    parse = BaseChatOpenAI._create_chat_result
    if getattr(parse, _FOLD_MARK, False):
        return

    @functools.wraps(parse)
    def _create_chat_result(self, response, generation_info=None):
        return parse(self, fold_split_choices(response, n=getattr(self, "n", None)), generation_info)

    setattr(_create_chat_result, _FOLD_MARK, True)
    BaseChatOpenAI._create_chat_result = _create_chat_result
