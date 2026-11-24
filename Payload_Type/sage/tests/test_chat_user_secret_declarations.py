"""Every chat setting that names a user-secret fallback declares that secret on its chat model.

Mythic 4.0.8 sends a chat container every user secret its operator holds, declared or not
(``rabbitmq.GetSecrets``), so an undeclared fallback still resolves. The declaration is what a Mythic user is
shown as the model's optional secrets, so a setting whose description promises a user-secret fallback must
appear there too. The checks read the built model definitions and the real resolver; nothing is mocked.
"""

from __future__ import annotations

import re

import pytest

from sage_chat import models as chat_models
from sage_chat.config import resolve_llm_profile
from sage_chat.headless import build_chat_request

_NAMED_SECRET = re.compile(r"\bthe ([A-Z][A-Z0-9_]+) user secret\b")


def _named_secrets(option) -> set[str]:
    text = f"{option.Description or ''} {getattr(option, 'HelpText', '') or ''}"
    return set(_NAMED_SECRET.findall(text))


@pytest.mark.parametrize("definition", chat_models.SAGE_MODELS, ids=lambda definition: definition.Name)
def test_every_named_user_secret_fallback_is_declared(definition):
    declared = set(definition.Metadata.OptionalUserSecrets) | set(definition.Metadata.RequiredUserSecrets)
    missing = {
        option.Name: sorted(_named_secrets(option) - declared)
        for option in definition.Metadata.ConfigurationOptions
        if _named_secrets(option) - declared
    }
    assert missing == {}


def test_the_reasoning_effort_setting_names_its_own_user_secret():
    # Guards the scan above against a wording change that would silently stop it matching.
    sage = next(definition for definition in chat_models.SAGE_MODELS if definition.Name == "Sage")
    effort = next(option for option in sage.Metadata.ConfigurationOptions if option.Name == "SAGE_REASONING_EFFORT")
    assert _named_secrets(effort) == {"SAGE_REASONING_EFFORT"}


def test_a_reasoning_effort_user_secret_fills_a_blank_chat_field(monkeypatch):
    monkeypatch.delenv("SAGE_REASONING_EFFORT", raising=False)
    request = build_chat_request(
        "hello",
        config={"provider": "openai", "model": "gpt-test", "SAGE_REASONING_EFFORT": ""},
        secrets={"SAGE_REASONING_EFFORT": "low"},
    )
    profile = resolve_llm_profile(request)
    assert profile.reasoning_effort == "low"
    assert profile.source_for("reasoning_effort") == "user-secret"

    chosen = build_chat_request(
        "hello",
        config={"provider": "openai", "model": "gpt-test", "SAGE_REASONING_EFFORT": "none"},
        secrets={"SAGE_REASONING_EFFORT": "low"},
    )
    assert resolve_llm_profile(chosen).reasoning_effort == "none"
