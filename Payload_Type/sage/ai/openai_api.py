"""OpenAI API selection, Daybreak access program and safety identifier: one validator, one request projection.

Both model factories (the ordinary ``Model._get_base_chat_model`` and the profile-based
``init_chat_model_from_profile``) build their ``ChatOpenAI`` keyword arguments here, so a selected route cannot
mean one request shape in a chat and another elsewhere.

Semantics:
- API unset keeps the adapter's own route (Chat Completions for ordinary model names), with no new request
  fields, so existing OpenAI and OpenAI-compatible proxy configurations send exactly what they sent before.
- ``chat_completions`` pins Chat Completions; ``responses`` selects the Responses API, stateless (``store=False``)
  with encrypted reasoning returned so it can be replayed with the history Sage already keeps.
- On Responses, Sage always names the access program. Unset means **Off** (``standard``): Sage never inherits a
  project's default Daybreak program. ``access_programs`` is documented for Responses only, so it is never sent
  on Chat Completions.
- The safety identifier is operator-supplied and sent only when configured. Sage never derives one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

API_ENV = "SAGE_OPENAI_API"
ACCESS_PROGRAM_ENV = "SAGE_OPENAI_ACCESS_PROGRAM"
SAFETY_IDENTIFIER_ENV = "SAGE_OPENAI_SAFETY_IDENTIFIER"

CHAT_COMPLETIONS = "chat_completions"
RESPONSES = "responses"
API_VALUES = (CHAT_COMPLETIONS, RESPONSES)
API_LABELS = {"": "adapter default", CHAT_COMPLETIONS: "Chat Completions", RESPONSES: "Responses"}

STANDARD = "standard"
ACCESS_PROGRAM_VALUES = (STANDARD, "daybreak_blue", "daybreak_red")
ACCESS_PROGRAM_LABELS = {STANDARD: "Off", "daybreak_blue": "Blue", "daybreak_red": "Red"}

# Models whose documented Responses configuration needs a pseudonymous per-user identifier.
SAFETY_IDENTIFIER_REQUIRED_MODELS = frozenset({"gpt-6-cyber"})

REASONING_INCLUDE = "reasoning.encrypted_content"


@dataclass(frozen=True)
class OpenAIRoute:
    api: str = ""
    access_program: str = ""
    safety_identifier: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api or self.access_program or self.safety_identifier)

    @property
    def effective_access_program(self) -> str:
        """The program a request names: the selection, or Off on Responses; none on Chat Completions."""
        if self.api != RESPONSES:
            return ""
        return self.access_program or STANDARD


def validate_route(
    *,
    provider: str,
    model: str,
    api: Any = "",
    access_program: Any = "",
    safety_identifier: Any = "",
) -> OpenAIRoute:
    """Return the normalized route, or raise ``ValueError`` naming the setting and the action to take."""
    api_value = str(api or "").strip()
    program = str(access_program or "").strip()
    identifier = str(safety_identifier or "").strip()
    if api_value and api_value not in API_VALUES:
        raise ValueError(
            f"{API_ENV} must be chat_completions or responses (OpenAI API: Chat Completions or Responses); "
            "leave it blank to keep the adapter's default route."
        )
    if program and program not in ACCESS_PROGRAM_VALUES:
        raise ValueError(
            f"{ACCESS_PROGRAM_ENV} must be standard, daybreak_blue or daybreak_red (Daybreak: Off, Blue or Red)."
        )
    route = OpenAIRoute(api=api_value, access_program=program, safety_identifier=identifier)
    if route.configured and str(provider or "").strip().casefold() != "openai":
        raise ValueError(
            f"{API_ENV}, {ACCESS_PROGRAM_ENV} and {SAFETY_IDENTIFIER_ENV} require the openai model provider."
        )
    if program and api_value != RESPONSES:
        raise ValueError(
            f"Daybreak ({ACCESS_PROGRAM_ENV}={program}) requires OpenAI API = Responses. Set OpenAI API to "
            "Responses or clear Daybreak; Sage does not change the API for you."
        )
    if api_value == RESPONSES and str(model or "").strip() in SAFETY_IDENTIFIER_REQUIRED_MODELS and not identifier:
        raise ValueError(
            f"{str(model).strip()} on the Responses API requires an OpenAI Safety Identifier "
            f"({SAFETY_IDENTIFIER_ENV}): set a stable pseudonymous identifier for the person using this chat."
        )
    return route


def adapter_options(route: OpenAIRoute) -> dict[str, Any]:
    """``ChatOpenAI`` keyword arguments for a validated route; empty for an unconfigured route."""
    options: dict[str, Any] = {}
    extra_body: dict[str, Any] = {}
    if route.api == CHAT_COMPLETIONS:
        options["use_responses_api"] = False
    elif route.api == RESPONSES:
        options.update(
            use_responses_api=True,
            output_version="responses/v1",
            store=False,
            include=[REASONING_INCLUDE],
        )
        extra_body["access_programs"] = {"cyber": route.effective_access_program}
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
    )
