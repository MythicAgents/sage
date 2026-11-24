"""A blank OpenAI API means Chat Completions, through the real model factory, the OpenAI SDK and the adapter.

langchain-openai picks the Responses API on its own for some model names (``gpt-5.6-sol``; ``gpt-6*`` with tools
bound), and such a request would carry none of Sage's Responses settings (``store: false``, encrypted reasoning,
the Daybreak program). Sage pins the API on every route, so a blank one sends Chat Completions. A model that needs
Responses is then rejected, and Sage's notice names the setting. The rejection body is the recorded Chat Completions
400 from the provider battery (row ``r03``); the replay prompt and the bound tool are synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.tools import tool

from ai.langgraph.operator_error import provider_configuration_notice
from tests import provider_wire_replay as replay

FIXTURE = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/provider_wire_round_trips.json")
                     .read_text(encoding="utf-8"))
REJECTION = next(row for row in FIXTURE["rows"] if row["id"] == "r03_chat_completions_400_a_reasoning_effort")
RESPONSES_FIELDS = ("store", "include", "access_programs", "input")


@tool
def list_callbacks() -> str:
    """List the operation's callbacks."""
    return "[]"


# Model names the installed adapter would move to Responses on its own (its Responses-only prefix list, and
# gpt-6 with tools), plus an ordinary name as the control.
@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-6-cyber", "gpt-5.5"])
@pytest.mark.parametrize("path", replay.PATHS)
def test_a_blank_openai_api_sends_chat_completions_without_responses_fields(model, path):
    row = {**REJECTION, "id": f"blank_{model}", "model": model, "route": {"api": "", "reasoning_effort": "low"}}
    sent: list[dict] = []
    llm, _client, requests = replay.build_model(row, path, sent=sent)

    result = replay.outcome(llm.bind_tools([list_callbacks]))

    assert requests == ["/v1/chat/completions"]
    assert sent[0]["model"] == model and sent[0]["tools"]
    assert not [field for field in RESPONSES_FIELDS if field in sent[0]]
    notice = provider_configuration_notice(result, replay.notice_profile(row))
    assert "set **OpenAI API** to **Responses**" in notice
    assert "OpenAI API: Chat Completions (default)" in notice


def test_a_selected_responses_route_still_carries_sages_responses_settings():
    row = {**REJECTION, "id": "selected_responses", "model": "gpt-5.6-sol",
           "route": {"api": "responses", "reasoning_effort": "low"}}
    sent: list[dict] = []
    llm, _client, requests = replay.build_model(row, "default", sent=sent)

    replay.outcome(llm.bind_tools([list_callbacks]))

    assert requests == ["/v1/responses"]
    assert sent[0]["store"] is False
    assert sent[0]["include"] == ["reasoning.encrypted_content"]
    assert sent[0]["access_programs"] == {"cyber": "standard"}
