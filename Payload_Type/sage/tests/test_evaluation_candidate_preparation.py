from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai.langgraph import policy
from Payload_Type.sage.tests.test_replay_capture import CATALOG, _inputs


ROOT = Path(__file__).resolve().parents[3]
PREPARE = ROOT / "skills/sage-eval-gauge/scripts/prepare_evaluation_candidates.py"
MEASURE = ROOT / "skills/sage-eval-gauge/scripts/run_development_measurement.py"


def _load_prepare():
    namespace = {"__name__": "m3_candidate_test", "__file__": str(PREPARE)}
    exec(compile(PREPARE.read_text(), str(PREPARE), "exec"), namespace)
    return namespace


def _helpers(source: str):
    tree = ast.parse(source)
    body = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_normalized_state_payload", "_prior_decisions_payload"}
    ]
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    namespace = {"Any": Any}
    exec(compile(module, "policy-helpers", "exec"), namespace)
    return namespace


def _candidate_sources():
    prepare = _load_prepare()
    source = (ROOT / prepare["POLICY_PATH"]).read_text()
    variants = prepare["build_candidates"](source)
    by_id = {row["id"]: row for row in variants}
    changed = {
        name: source.replace(before, after, 1)
        for name, before, after, _hypothesis in prepare["VARIANTS"]
    }
    changed["whitespace-sham"] = source.replace(
        "def _prior_decisions_payload(", "\n\ndef _prior_decisions_payload(", 1
    )
    changed["label-sham"] = source
    return source, variants, by_id, changed


def _run(script: Path, *arguments: str):
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


def test_build_candidates_are_five_unapplied_hypotheses_and_two_syntax_valid_shams():
    source, variants, by_id, changed = _candidate_sources()
    assert len(variants) == 7
    assert {row["kind"] for row in variants[:5]} == {"working_hypothesis"}
    assert {row["kind"] for row in variants[5:]} == {"sham"}
    assert all(row["behaviorally_validated"] is False for row in variants)
    assert all(row["patch"] and row["source_sha256"] != hashlib.sha256(source.encode()).hexdigest()
               for row in variants[:5])
    for candidate in changed.values():
        ast.parse(candidate)
    assert ast.dump(ast.parse(changed["whitespace-sham"]), include_attributes=False) == ast.dump(
        ast.parse(source), include_attributes=False
    )
    assert by_id["label-sham"]["patch"] == ""
    assert by_id["label-sham"]["source_sha256"] == hashlib.sha256(source.encode()).hexdigest()


def test_candidate_attribution_rejects_two_field_or_changed_plus_inserted_edits():
    prepare = _load_prepare()
    source = (ROOT / prepare["POLICY_PATH"]).read_text()
    before = '"status": str(getattr(item, "status", "") or ""),'
    two = before + '\n                "satisfied_effects": [],\n                "preconditions": [],'
    planted = source.replace(before, two, 1)
    assert prepare["candidate_change_count"](source, planted, "_normalized_state_payload") == 2
    with pytest.raises(ValueError, match="multi-edit attribution"):
        prepare["variant_source"](source, "planted-two", before, two)
    changed_plus_insert = source.replace(before,
        '"status": str(getattr(item, "effect", "") or ""),\n                "preconditions": [],', 1)
    assert prepare["candidate_change_count"](
        source, changed_plus_insert, "_normalized_state_payload") == 2


def test_working_hypotheses_change_only_the_supplied_policy_payload_values():
    source, variants, by_id, changed = _candidate_sources()
    baseline = _helpers(source)

    history = [
        SimpleNamespace(
            decision_id=str(index), disposition="select", selected_capability=f"cap-{index}",
            selected_target=f"target-{index}", expected_evidence=f"expect-{index}",
        )
        for index in range(12)
    ]
    assert [row["decision_id"] for row in _helpers(changed["history-12"])["_prior_decisions_payload"](history)] == [
        str(index) for index in range(12)
    ]
    assert len(baseline["_prior_decisions_payload"](history)) == 8
    short_history = history[:4]
    assert _helpers(changed["history-12"])["_prior_decisions_payload"](short_history) == baseline[
        "_prior_decisions_payload"
    ](short_history)
    assert by_id["history-12"]["kind"] == "working_hypothesis"
    assert by_id["history-12"]["behaviorally_validated"] is False

    hops = [
        SimpleNamespace(
            technique=f"cap-{index}", target=f"target-{index}", effect=f"effect-{index}",
            status="observed", satisfied_effects=[f"known-effect-{index}"],
            preconditions=[f"known-precondition-{index}"],
        )
        for index in range(24)
    ]
    state = SimpleNamespace(hops=hops, footholds=[], graph_facts=[])
    state.achieved_effects = lambda: []
    assert len(_helpers(changed["outcomes-24"])["_normalized_state_payload"](state)["recent_outcomes"]) == 24
    assert len(baseline["_normalized_state_payload"](state)["recent_outcomes"]) == 12

    effect_payload = _helpers(changed["outcome-effect-links"])["_normalized_state_payload"](state)
    assert effect_payload["recent_outcomes"][-1]["satisfied_effects"] == ["known-effect-23"]
    assert set(effect_payload["recent_outcomes"][-1]) == {
        "capability", "target", "effect", "status", "satisfied_effects"
    }
    precondition_payload = _helpers(changed["outcome-preconditions"])["_normalized_state_payload"](state)
    assert precondition_payload["recent_outcomes"][-1]["preconditions"] == ["known-precondition-23"]
    assert set(precondition_payload["recent_outcomes"][-1]) == {
        "capability", "target", "effect", "status", "preconditions"
    }

    expected = _helpers(changed["history-expectations"])["_prior_decisions_payload"](history)
    assert expected[-1]["expected_evidence"] == "expect-11"
    assert set(expected[-1]) == {"decision_id", "disposition", "capability", "target", "expected_evidence"}


def test_prepare_cli_is_unapplied_hash_bound_and_fail_closed(tmp_path):
    policy_path = ROOT / "Payload_Type/sage/ai/langgraph/policy.py"
    original_bytes = policy_path.read_bytes()
    corpus_digest = hashlib.sha256(b"frozen-development-corpus").hexdigest()
    output = tmp_path / "candidates.json"

    prepared = _run(
        PREPARE, "--source-root", str(ROOT), "--output", str(output),
        "--development-corpus-sha256", corpus_digest,
    )
    assert prepared.returncode == 0
    document = json.loads(output.read_text())
    assert document["development_corpus_sha256"] == corpus_digest
    assert document["applied_to_runtime"] is False
    assert document["live_authorized"] is False
    assert policy_path.read_bytes() == original_bytes

    overwrite = _run(
        PREPARE, "--source-root", str(ROOT), "--output", str(output),
        "--development-corpus-sha256", corpus_digest,
    )
    assert overwrite.returncode == 2
    assert json.loads(overwrite.stdout)["status"] == "refused"
    assert policy_path.read_bytes() == original_bytes

    changed_root = tmp_path / "changed"
    changed_policy = changed_root / "Payload_Type/sage/ai/langgraph/policy.py"
    changed_policy.parent.mkdir(parents=True)
    changed_policy.write_text(original_bytes.decode().replace("for item in history[-8:]", "for item in history[-7:]", 1))
    changed_run = _run(
        PREPARE, "--source-root", str(changed_root), "--output", str(tmp_path / "changed.json"),
        "--development-corpus-sha256", corpus_digest,
    )
    assert changed_run.returncode == 2
    assert "source seam changed" in json.loads(changed_run.stdout)["reason"]


def test_measurement_cli_plan_has_no_authority_and_precision_rejects_saturated_ties(tmp_path):
    plan_input = tmp_path / "plan.json"
    plan_input.write_text(json.dumps({
        "practical_delta": 0.1, "tolerance": 0.05, "family_alpha": 0.05, "comparisons": 10,
    }))
    plan_run = _run(MEASURE, "plan", "--input", str(plan_input))
    assert plan_run.returncode == 0
    plan = json.loads(plan_run.stdout)
    assert plan["live_authorized"] is False
    assert plan["protected_access_authorized"] is False
    assert plan["composite_execution_authorized"] is False

    precision_input = tmp_path / "precision.json"
    precision_input.write_text(json.dumps({
        "left": [False] * 5, "right": [False] * 5, "practical_delta": 0.1,
    }))
    precision_run = _run(MEASURE, "precision", "--input", str(precision_input))
    assert precision_run.returncode == 0
    precision = json.loads(precision_run.stdout)
    assert precision["paired_n"] == 5
    assert precision["informative"] is False
    assert "saturated_binary_arms" in precision["reason_codes"]


def _replay_packet(monkeypatch, history_count=20):
    state, candidates, history = _inputs()
    history = history[:history_count]
    monkeypatch.setenv(policy._CAPTURE_DECISION_PACKETS_ENV, "true")
    monkeypatch.setenv(policy._CAPTURE_REPLAY_CONTEXT_ENV, "true")
    packet, _ = policy._captured_decision_packet(
        objective="measure input behavior", state=state, candidates=candidates,
        history=history, budgets={"task_budget": 1}, selection_contract=policy.SELECTION_CONTRACT_LLM,
        catalog=CATALOG,
    )
    return state, candidates, history, packet


def test_replay_requests_match_actual_baseline_and_exercise_all_working_candidates(monkeypatch):
    prepare = _load_prepare()
    source = (ROOT / prepare["POLICY_PATH"]).read_text()
    state, candidates, history, packet = _replay_packet(monkeypatch)
    catalog = CATALOG
    actual = policy.LLMPolicy(None, catalog=catalog).request_payload(
        packet["objective"], state, candidates, history, packet["budgets"]
    )
    replayed = prepare["replay_requests"](source, packet, catalog)
    assert replayed["baseline_request"] == actual
    assert replayed["provider_requests"] == 0
    by_id = {row["id"]: row for row in replayed["variants"]}
    assert all(by_id[name]["input_changed"] is True for name, *_ in prepare["VARIANTS"])
    assert by_id["whitespace-sham"]["request"] == actual
    assert by_id["label-sham"]["request"] == actual
    assert all("replay_context" not in row["request"] for row in replayed["variants"])
    assert "replay_context" not in replayed["baseline_request"]


def test_replay_requests_refuse_missing_or_mismatched_context_and_retain_short_noop(monkeypatch):
    prepare = _load_prepare()
    source = (ROOT / prepare["POLICY_PATH"]).read_text()
    catalog = CATALOG
    _state, _candidates, _history, packet = _replay_packet(monkeypatch)
    missing_catalog = json.loads(json.dumps(packet))
    missing_catalog["replay_context"].pop("capability_catalog")
    with pytest.raises(ValueError, match="catalog"):
        prepare["replay_requests"](source, missing_catalog, catalog)
    with pytest.raises(ValueError, match="catalog"):
        prepare["replay_requests"](source, packet, [{"name": "other", "description": "different"}])
    legacy = {key: value for key, value in packet.items() if key != "replay_context"}
    with pytest.raises(ValueError, match="legacy packet"):
        prepare["replay_requests"](source, legacy, catalog)

    wrong_history = json.loads(json.dumps(packet))
    wrong_history["prior_decisions"][-1]["disposition"] = "stop"
    with pytest.raises(ValueError, match="does not reproduce"):
        prepare["replay_requests"](source, wrong_history, catalog)
    wrong_state = json.loads(json.dumps(packet))
    wrong_state["replay_context"]["hops"][-1]["status"] = "changed-status"
    with pytest.raises(ValueError, match="does not reproduce"):
        prepare["replay_requests"](source, wrong_state, catalog)

    _state, _candidates, _history, short_packet = _replay_packet(monkeypatch, history_count=4)
    short = prepare["replay_requests"](source, short_packet, catalog)
    history_row = next(row for row in short["variants"] if row["id"] == "history-12")
    assert history_row["input_changed"] is False
    assert len(short["variants"]) == 7


def test_prepare_cli_replays_passive_packet_without_provider_or_source_mutation(tmp_path, monkeypatch):
    prepare = _load_prepare()
    policy_path = ROOT / prepare["POLICY_PATH"]
    original_bytes = policy_path.read_bytes()
    _state, _candidates, _history, packet = _replay_packet(monkeypatch)
    catalog = CATALOG
    packet_path, catalog_path, output = tmp_path / "packet.json", tmp_path / "catalog.json", tmp_path / "output.json"
    packet_path.write_text(json.dumps(packet))
    catalog_path.write_text(json.dumps(catalog))
    result = _run(
        PREPARE, "--source-root", str(ROOT), "--output", str(output),
        "--development-corpus-sha256", hashlib.sha256(b"frozen-development-corpus").hexdigest(),
        "--replay-packet", str(packet_path), "--catalog", str(catalog_path),
    )
    assert result.returncode == 0
    replay = json.loads(output.read_text())["request_replay"]
    assert replay["provider_requests"] == 0
    assert policy_path.read_bytes() == original_bytes
