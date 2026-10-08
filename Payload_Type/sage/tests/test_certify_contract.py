"""The Certify references expose the parser contract bound to the inspected v2 artifact."""

import asyncio
import json
import shlex
from pathlib import Path
from unittest.mock import AsyncMock

from ai.langgraph import ttp_library
from ai.langgraph.mythic_tools import MythicTools


SAGE_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = SAGE_ROOT / "tool_contracts" / "certify.json"


def _contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _assert_enrolled_example(contract, args):
    tokens = shlex.split(args)
    verb = contract["verbs"][tokens[0]]
    supplied = {token[2:] for token in tokens[1:] if token.startswith("--")}
    assert supplied <= set(verb["options"])
    assert set(verb.get("required", [])) <= supplied


def test_binary_identity_and_enrollment_are_explicit():
    contract = _contract()
    enrolled = {entry["name"] for entry in contract["registered_verbs"] if entry["enrolled"]}
    assert enrolled == {"enum-cas", "enum-templates", "request", "request-download"}
    assert len(contract["registered_verbs"]) == 11
    assert contract["disabled_registered_verbs"] == []
    assert contract["legacy_unregistered_verbs"] == ["find", "cas", "download"]
    assert contract["identity_rule"].startswith("verified only when sha256 matches")
    assert contract["evidence"]["help_execution"].startswith("observed_off_target_four_enrolled_verbs_")
    assert contract["evidence"]["command_execution"].startswith("unverified_")
    assert contract["comparison_source"]["binding"].startswith("comparison only")


def test_guidance_examples_follow_enrolled_v2_parser_shapes():
    contract = _contract()
    frontmatter, _ = ttp_library.load_ttp("certify")
    assert frontmatter["binary_sha256"] == contract["binary"]["sha256"]
    assert frontmatter["binary_contract"] == "tool_contracts/certify.json"
    for example in frontmatter["usage_examples"]:
        _assert_enrolled_example(contract, example["args"])


def test_real_progressive_disclosure_returns_current_and_legacy_scoped_guidance(monkeypatch):
    sink = MythicTools(agent_task_id="certify-contract-test")
    monkeypatch.setattr(sink, "_resolve_payload_type", AsyncMock(return_value="apollo"))
    guidance_tool = next(tool for tool in sink.get_tools(["get_ttp_guidance"]) if tool.name == "get_ttp_guidance")
    full_tool = next(tool for tool in sink.get_tools(["get_ttp_full_reference"]) if tool.name == "get_ttp_full_reference")

    short = json.loads(asyncio.run(guidance_tool.ainvoke({
        "goal": "use Certify for ADCS enumeration and certificate requests",
        "callback_display_id": 1,
    })))
    full = json.loads(asyncio.run(full_tool.ainvoke({"slug": "certify"})))

    assert short["slug"] == "certify"
    assert "enum-templates --filter-vulnerable" in json.dumps(short)
    assert "current argument form" in short["guidance"].lower()
    assert "legacy scope" in full["full_reference"].lower()
    assert "Older Certify builds" in full["full_reference"]
    current = short["frontmatter"]["usage_examples"]
    assert all("/" not in example["args"] for example in current)


def test_v2_migration_reference_contains_no_executable_legacy_example():
    contract = _contract()
    frontmatter, body = ttp_library.load_ttp("certify-v2")
    for example in frontmatter["usage_examples"]:
        _assert_enrolled_example(contract, example["args"])
    reference = ttp_library.full_reference(body)
    assert "enum-cas --filter-vulnerable" in reference
    assert "are not registered by this build" in reference


def test_related_ttp_examples_are_single_current_invocations():
    contract = _contract()
    for slug in ("adcs-esc4", "adcs-esc6", "adcsscan-bof"):
        frontmatter, _ = ttp_library.load_ttp(slug)
        examples = [
            example["args"] for example in frontmatter["usage_examples"]
            if example["args"].startswith("Certify.exe ")
        ]
        assert examples
        for example in examples:
            assert ";" not in example
            invocation = example.removeprefix("Certify.exe ")
            tokens = invocation.split()
            _assert_enrolled_example(contract, invocation)
            if "--ca" in tokens:
                ca = tokens[tokens.index("--ca") + 1]
                assert ca.count("\\") == 1
