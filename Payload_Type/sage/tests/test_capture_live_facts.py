from __future__ import annotations

from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-eval-gauge/scripts/capture_live_facts.py"
SPEC = importlib.util.spec_from_file_location("m3_capture_live_facts", SCRIPT)
live = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = live
assert SPEC.loader is not None; SPEC.loader.exec_module(live)


def _row(table, **values):
    return {name: values.get(name) for name in live.native_inventory.TABLE_FIELDS[table]}


def _snapshot(*, task=True, corrupt=False):
    operation = 9
    callbacks = [_row("callback", id=10, operation_id=operation, display_id=11,
        init_callback="x", last_checkin="x", timestamp="x", active=True, dead=False,
        user="u", host="h", domain="d", integrity_level=2, pid=1,
        process_name="p", os="o", architecture="a", sleep_info="s")]
    tasks = ([_row("task", id=20, operation_id=operation, display_id=200, callback_id=10,
        command_name="ls", original_params="", display_params="", completed=True,
        status="completed", status_timestamp_preprocessing="2026-01-01T00:00:00Z",
        status_timestamp_submitted="2026-01-01T00:00:01Z",
        status_timestamp_processing="2026-01-01T00:00:02Z",
        status_timestamp_processed="2026-01-01T00:00:03Z", timestamp="2026-01-01T00:00:00Z",
        parent_task_id=None, opsec_pre_blocked=False, opsec_pre_bypassed=False,
        opsec_post_blocked=False, opsec_post_bypassed=False, tasking_location="command_line",
        response_count=0)] if task else [])
    messages = ([_row("chat_message", id=30, operation_id=operation, channel_id="channel",
        chat_request_id=None, chat_response_key="", author_type="user", sender_display_name="",
        message=None, tool_output=None, edited=False, deleted=False, status="published",
        metadata=None, created_at="x", updated_at="x"),
        _row("chat_message", id=31, operation_id=operation, channel_id="channel",
        chat_request_id=40, chat_response_key="response", author_type="agent",
        sender_display_name="sage", message='{"safe":"finding"}', tool_output=None,
        edited=False, deleted=False, status="published", metadata={}, created_at="x", updated_at="x")]
        if task else [])
    requests = ([_row("chat_request", id=40, operation_id=operation, channel_id="channel",
        request_message_id=30, chat_container_id="sage", model="model", status="complete", error=None,
        context_snapshot=None, retry_of_id=None, created_at="x", updated_at="x",
        completed_at="x", cancelled_at=None)] if task else [])
    rows = {"callback": callbacks, "task": tasks, "response": [], "credential": [],
            "chat_request": requests, "chat_message": messages}
    result = {"schema": "sage-m3-native-inventory-v1", "operation_id": operation,
        "complete": True, "failures": [], "snapshot_consistency": "single_operation_root_query",
        "query_hashes": {"operation_snapshot": "a" * 64}, "rows": rows}
    if corrupt: result["rows"]["task"][0].pop("status")
    return result


def _objective(outcome):
    observation = live.effect_registry.OracleObservation(
        registry_version=live.effect_registry.REGISTRY_VERSION,
        oracle_id="independent.objective-control.v1", origin="mythic_task_output",
        proof_kind="objective_control", outcome=outcome, observation_id="objective-observation",
        target="target", subject="subject", envelope_id="different-independent-envelope")
    return live.effect_registry.admit_observation(
        live.Milestone.OBJECTIVE, observation, expected_target="target").to_record()


def _inputs(*, positive=True):
    identity = {"protocol_id": "p", "pair_id": "pair", "logical_seed": 1,
        "policy_arm": "shipping_sage", "model_provider": "provider", "model_id": "model",
        "model_version": "model-v1", "backend_route": "provider:model",
        "generation_config": {"temperature": 0}, "wrapper_id": "shipping_native_sage",
        "wrapper_revision": "tree:v1", "authority_mode": "auto",
        "execution_path": "policy_selected_controller", "source_version": "tree:v1"}
    contract = {"expected_target": "target", "row_identity": {"row_id": "row",
        "surface_id": "surface", "family_id": "family"}, "configured_policy_mode": "llm",
        "configured_provider": "provider", "configured_model": "model", "arm": "shipping_sage",
        "pair_id": "pair", "observer_version": "observer-v", "token_ceiling": 100,
        "model_context_limit": 1000, "max_output_tokens": 100, "current_engagement_id": "engagement",
        "expected_operation_id": 9, "selected_channel": "channel", "selected_request": 40}
    decision = {"decision_id": "decision", "policy_mode": "llm", "decision_owner": "model_branch",
        "branch_opportunity_count": 1, "effective_backend": "provider:model",
        "backend_provenance_source": "provider_response", "token_usage_source": "provider_response",
        "token_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
    proof = live.proof_boundary.ProofEnvelope(
        origin="mythic_task", engagement_id="engagement", callback_id="11",
        transaction_id="transaction", task_id="200", terminal_status="completed", command="ls",
        verifier_id="verifier", verifier_hash=live.proof_boundary.stable_verifier_hash("verifier"),
        verifier_input_sha256="b" * 64, verifier_result_sha256="c" * 64,
        captured_at="2026-01-01T00:00:03Z", persistence_state="admitted")
    transaction = {"transaction_id": "transaction", "decision_id": "decision", "policy_mode": "llm",
        "callback_id": "11", "child_tasks": [{"task_id": "200", "terminal_status": "completed"}],
        "verifier_ids": ["verifier"], "proof_envelope_ids": [proof.hash], "proof_lineage": [{
            "proof_envelope_id": proof.hash, "transaction_id": "transaction", "task_id": "200",
            "verifier_id": "verifier", "verifier_hash": proof.verifier_hash,
            "admissible_for_runtime_achievement": True}]}
    telemetry = {"policy_mode": "llm", "configured_policy_mode": "llm", "policy_switches": [],
        "policy_identity_valid": True, "model_provider": "provider", "model_id": "model",
        "semantic_transaction_count": 1, "authorized_transaction_count": 1,
        "semantic_policy_coverage": 1.0, "model_calls": 1,
        "effective_backend_requests": [decision], "backend_provenance_complete": True,
        "model_response_records": [decision], "token_usage_complete": True, "total_tokens": 15,
        "token_budget": 100, "model_context_limit": 1000, "max_output_tokens": 100,
        "transactions": [transaction], "decisions": [decision]}
    record = _objective(positive)
    raw_oracle = {"success": positive, "proof_record": record}
    events = [{"event_id": "pair:shipping_sage:coordinator:1", "source": "coordinator",
        "kind": "arm_started", "sequence": 1, "monotonic_ns": 1},
        {"event_id": "pair:shipping_sage:mythic:2", "source": "mythic", "kind": "task_observed",
         "sequence": 2, "monotonic_ns": 2, "task_id": 200},
        {"event_id": "pair:shipping_sage:mythic:3", "source": "mythic", "kind": "task_completed",
         "sequence": 3, "monotonic_ns": 3, "task_id": 200}]
    if positive: events.append({"event_id": "pair:shipping_sage:oracle:4", "source": "oracle",
        "kind": "objective_positive", "sequence": 4, "monotonic_ns": 4})
    terminal_sequence = len(events) + 1
    events.append({"event_id": f"pair:shipping_sage:wrapper:{terminal_sequence}", "source": "wrapper",
        "kind": "terminal", "sequence": terminal_sequence, "monotonic_ns": terminal_sequence,
        "status": "success"})
    observation = {"observer_version": "observer-v", "oracle": raw_oracle,
        "task_window_observed": True, "wrapper_objective_recognized": positive,
        "tasks": [{"operation_id": 9, "display_id": 200, "completed": True,
            "callback": {"display_id": 11, "host": "h", "domain": "d", "user": "u"}}],
        "events": events}
    episode = {"event": "measurement_capture_episode", "schema": "sage-measurement-capture-episode-v1",
        "episode_id": "episode", "requested_identity": {"measurement_kind": "capture",
            "resource_episode_id": "episode", "source_version": "tree:v1", "side": "harness"},
        "oracle": raw_oracle, "task_state_observation": observation,
        "response_records": [{"response_id": "decision", "effective_backend": "provider:model",
            "backend_provenance_source": "provider_response", "input_tokens": 10,
            "output_tokens": 5, "total_tokens": 15, "usage_complete": True}],
        "proof_records": {"objective_proof": record},
        "scorecard": {"objective_proven": positive, "clean_stop": True, "model_calls": 1,
            "total_tokens": 15, "policy_mode": "llm", "configured_policy_mode": "llm",
            "policy_identity_valid": True, "model_provider": "provider", "model_id": "model",
            "semantic_transaction_count": 1, "authorized_transaction_count": 1,
            "measurement_identity": identity},
        "native_solver_result": {"runtime_telemetry": telemetry,
            "producer_settlement": {"native_producer_terminal": True}}, "raw_result": None,
        "complete_inventory": True, "missing_facts": [], "telemetry_validation_error": None}
    event_decision = {**decision, "decision_packet": {"schema": "m3-policy-input-v1"},
                      "decision_packet_hash": "sha256:" + "d" * 64}
    projected = [{"event_id": "policy-event", "time_ns": 1, "kind": "tool_event",
                  "payload": {"event": "policy_decision", "decision": event_decision}}]
    engagement_first = {"engagement_id": "engagement", "hops": [], "graph_facts": []}
    engagement_final = {"engagement_id": "engagement",
        "hops": ([{"proof_envelope": proof.to_dict(), "private_evidence": "RAW_PRIVATE_HOP"}]
                 if positive else []), "graph_facts": []}
    return {"episode": episode, "contract": contract, "native_first": _snapshot(task=False),
        "native_final": _snapshot(), "projected_events": projected,
        "containment_receipt": {"complete": True, "owned_processes_dead": True,
            "cleanup_steps": [{"name": "namespace", "result": "complete"}]},
        "engagement_first": engagement_first, "engagement_final": engagement_final}


def test_positive_and_negative_actual_producers_derive_safe_live_facts():
    positive = live.derive_live_outer_facts(**_inputs())
    assert positive["source_kind"] == "live" and len(positive["proof_envelopes"]) == 1
    assert positive["semantic_transactions"] == ["transaction"]
    assert positive["policy_event_inventory"] == {"complete": True, "model_owned_branch_observed": True}
    assert "RAW_PRIVATE_HOP" not in repr(positive)
    negative = live.derive_live_outer_facts(**_inputs(positive=False))
    assert negative["proof_envelopes"] == [] and negative["negative_oracle_complete"] is True


def test_negative_objective_preserves_valid_partial_progress_through_live_bridge(tmp_path):
    from tests.test_capture_composite_bridge import _inputs as bridge_inputs, _load as bridge_load

    values = _inputs(positive=False)
    values["engagement_final"] = _inputs()["engagement_final"]
    facts = live.derive_live_outer_facts(**values)
    compiler, _ = bridge_inputs()
    compiler["redacted_episode_packet"] = values["episode"]
    compiler["episode_packet_sha256"] = live._hash(values["episode"])
    bridge = bridge_load()
    derived, proofs = bridge._derive_outcome(values["episode"], compiler, facts)
    assert len(proofs) == 1 and derived["proof_provenance_complete"] is True
    result = bridge.compose_live_capture(compiler, **values, output_dir=tmp_path)
    assert result["fixture_only"] is False
    assert result["admission"]["status"] == "admitted"
    assert result["admission"]["composite_row"]["safe_success"] is False


def test_negative_objective_does_not_hide_invalid_partial_progress():
    values = _inputs(positive=False)
    values["engagement_final"] = _inputs()["engagement_final"]
    values["native_final"]["rows"]["task"][0]["display_id"] = 201
    with pytest.raises(live.LiveCaptureFactsIncomplete):
        live.derive_live_outer_facts(**values)


def test_actual_model_event_projection_joins_packet_free_runtime(monkeypatch):
    from ai.langgraph import model

    values = _inputs(); full = deepcopy(values["projected_events"][0]["payload"]["decision"])
    emitted = []
    monkeypatch.setenv(model.observation_channel.ENV_NAME, str(ROOT / "private-observation-test"))
    monkeypatch.setenv("SAGE_EVAL_CAPTURE_REPLAY_CONTEXT", "true")
    monkeypatch.setattr(model.observation_channel, "emit",
                        lambda kind, payload: emitted.append((kind, payload)) or True)
    monkeypatch.setattr(model.Model, "_controller_refresh_runtime_policy_telemetry", lambda _self: {})
    instance = object.__new__(model.Model); instance._controller_observed_decisions = []
    instance._controller_runtime_telemetry = {"configured_policy_mode": "llm"}
    recorded = instance._controller_record_policy_decision(full)
    assert set(full) == set(recorded) | {"decision_packet", "decision_packet_hash"}
    assert emitted == [("tool_event", {"event": "policy_decision", "decision": full})]

    redaction = live._load("capture_redaction").SecretIndex()
    projector = live._load("capture_event_projection").CaptureEventProjection(redaction)
    projected = projector({"schema": "sage-observation-event-v2", "event_id": "a" * 32,
        "process_uuid": "b" * 32, "pid": os.getpid(), "seq": 1, "time_ns": 1,
        "kind": emitted[0][0], "payload": emitted[0][1]})
    telemetry = values["episode"]["native_solver_result"]["runtime_telemetry"]
    telemetry["decisions"] = telemetry["effective_backend_requests"] = telemetry["model_response_records"] = [recorded]
    values["projected_events"] = [projected]
    assert live.derive_live_outer_facts(**values)["source_receipts"]["policy_decision_packets"] == [{
        "decision_id": "decision", "decision_packet_hash": "sha256:" + "d" * 64}]


@pytest.mark.parametrize(("positive", "safe_success"), [(False, False), (True, "ABSTAIN")])
def test_plain_markdown_reaches_existing_composite_as_unknown_opsec(tmp_path, positive, safe_success):
    from tests.test_capture_composite_bridge import _inputs as bridge_inputs, _load as bridge_load

    values = _inputs(positive=positive)
    values["native_final"]["rows"]["chat_message"][-1]["message"] = "Ordinary Markdown finding"
    outer = live.derive_live_outer_facts(**values)
    assert outer["terminal_snapshot"]["metadata_taxonomy_complete"] is False
    compiler, _old_outer = bridge_inputs(); compiler["redacted_episode_packet"] = values["episode"]
    outer["source_kind"] = "test_fixture"
    result = bridge_load().compose_capture(compiler, outer, output_dir=tmp_path)
    assert result["admission"]["status"] == "admitted"
    assert result["admission"]["composite_row"]["safe_success"] == safe_success
    compiler["risk"]["counters"]["secret_exposure_count"] = {"value": 0, "complete": False}
    risk_dir = tmp_path / "risk-unknown"; risk_dir.mkdir()
    rejected = bridge_load().compose_capture(compiler, outer, output_dir=risk_dir)
    assert rejected["admission"]["status"] == "not_reusable"


@pytest.mark.parametrize("case", ["stale", "task", "route", "event", "containment", "snapshot",
                                  "callback", "terminal", "observer-task"])
def test_required_live_join_failures_remain_explicit(case):
    values = _inputs()
    if case == "stale": values["engagement_first"] = deepcopy(values["engagement_final"])
    elif case == "task": values["native_final"]["rows"]["task"][0]["display_id"] = 201
    elif case == "route": values["episode"]["native_solver_result"]["runtime_telemetry"]["model_id"] = "other"
    elif case == "event": values["projected_events"] = []
    elif case == "containment": values["containment_receipt"]["owned_processes_dead"] = False
    elif case == "snapshot": values["native_final"] = _snapshot(corrupt=True)
    elif case == "callback":
        values["native_first"]["rows"]["callback"][0]["display_id"] = 12
        values["native_final"]["rows"]["callback"][0]["display_id"] = 12
    elif case == "terminal": values["native_final"]["rows"]["task"][0]["status"] = "error"
    else: values["episode"]["task_state_observation"]["tasks"] = []
    with pytest.raises(live.LiveCaptureFactsIncomplete): live.derive_live_outer_facts(**values)
