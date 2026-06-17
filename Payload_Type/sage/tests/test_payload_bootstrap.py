import asyncio
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import mythic_tools  # noqa: E402
import prompt_loader  # noqa: E402
from mythic_tools import MythicTools  # noqa: E402


def _make_tools() -> MythicTools:
    mt = MythicTools(agent_task_id="payload-bootstrap-test")
    mt.client = object()
    return mt


def test_payload_names_include_sage_for_fresh_reset_bootstrap(monkeypatch):
    seen = {}

    async def fake_query(client, query, variables=None):
        seen["query"] = query
        return {"payloadtype": [{"name": "sage"}, {"name": "apollo"}]}

    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", fake_query)

    names = asyncio.run(_make_tools().get_payload_names())

    assert names == ["sage", "apollo"]
    assert '_neq: "sage"' not in seen["query"]


def test_payload_info_query_does_not_filter_sage(monkeypatch):
    seen = {}

    async def fake_query(client, query, variables=None):
        seen["query"] = query
        return {
            "payloadtype": [
                {"name": "sage", "supported_os": ["linux"], "buildparameters": []},
                {"name": "apollo", "supported_os": ["windows"], "buildparameters": []},
            ]
        }

    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", fake_query)

    raw = asyncio.run(_make_tools().get_all_payload_info())
    payloads = json.loads(raw)["payloadtype"]

    assert [p["name"] for p in payloads] == ["sage", "apollo"]
    assert '_neq: "sage"' not in seen["query"]


def test_payload_prompt_forbids_reuse_after_mythic_reset():
    rendered = prompt_loader.load_prompt(
        "mythic_payload",
        installed_payloads_text="        - sage\n        - apollo",
        installed_c2_profiles_text="        - http: HTTP C2",
    )

    assert "if the operator says this is after a Mythic reset" in rendered
    assert "do NOT reuse any old payload" in rendered
    assert "Build new Sage and/or Apollo payloads" in rendered
