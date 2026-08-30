"""Offline tests for the live driver's probe assembly: ground truth is scoped to the scenario's
milestones and read via the OUT-OF-BAND reader (never the agent callback). The live solve/baseline are
validated on the range; this pins the wiring that prevents the pollution + wrong-domain bugs.
"""
import json
import importlib.util
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import run_gauge_live as rgl  # noqa: E402
from range_state import Milestone, PROBEABLE_MILESTONES  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
ORCHESTRATE_SCRIPT = ROOT / "skills" / "sage-eval-gauge" / "scripts" / "orchestrate.py"
ORCHESTRATE_SPEC = importlib.util.spec_from_file_location(
    "sage_eval_orchestrate_for_live_child",
    ORCHESTRATE_SCRIPT,
)
orchestrate = importlib.util.module_from_spec(ORCHESTRATE_SPEC)
assert ORCHESTRATE_SPEC and ORCHESTRATE_SPEC.loader
ORCHESTRATE_SPEC.loader.exec_module(orchestrate)


def test_gate1p_exact_child_command_parses_help_without_pythonpath():
    payload_root = ROOT / "Payload_Type" / "sage"
    result = subprocess.run(
        [sys.executable, "ai/hillclimb/run_gauge_live.py", "--help"],
        cwd=payload_root,
        env={"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage: run_gauge_live.py" in result.stdout


def test_build_bare_runner_injects_hidden_collection_adapter_without_exposing_file_upload(monkeypatch):
    captured = {}
    upload_tool = object()
    query_spec = {"type": "function", "function": {
        "name": "domain_info", "description": "query", "parameters": {"type": "object", "properties": {}}
    }}

    monkeypatch.setattr(rgl, "_resolved_harness_model_route", lambda _cfg: {
        "provider": "openai", "model": "same-model", "api_key": "key", "api_endpoint": "https://invalid"
    })
    monkeypatch.setattr(rgl.live_seams, "make_model_fn", lambda *_args, **_kwargs: (lambda *_a: {"final": "done"}))
    monkeypatch.setattr(rgl.live_seams, "default_mythic_client", lambda: "mythic-client")
    monkeypatch.setattr(
        rgl.bare_bloodhound,
        "load_bloodhound_mcp_surface",
        lambda: ([query_spec], {"domain_info": object()}, upload_tool),
    )

    async def ingestor(_name, _blob):
        return {"job_id": 1}

    monkeypatch.setattr(rgl.bare_bloodhound, "make_collection_ingestor", lambda tool: ingestor if tool is upload_tool else None)
    monkeypatch.setattr(rgl.bare_bloodhound, "make_bloodhound_dispatcher", lambda _registry: (lambda _call: "bh"))

    def make_dispatcher(client, *, collection_ingestor=None):
        captured.update(client=client, collection_ingestor=collection_ingestor)
        return lambda _call: "mythic"

    monkeypatch.setattr(rgl.bare_mythic_tools, "make_mythic_dispatcher", make_dispatcher)
    cfg = SimpleNamespace(
        max_output_tokens=100,
        max_steps=0,
        solve_timeout=10,
        model_token_budget=1000,
        measurement_kind="diagnostic",
    )
    runner = rgl.build_bare_runner(cfg, require_bloodhound=True)
    names = {spec["function"]["name"] for spec in runner.tools_spec}
    assert captured == {"client": "mythic-client", "collection_ingestor": ingestor}
    assert "ingest_collection" in names
    assert "file_upload" not in names


def _telemetry(**overrides):
    value = {
        "policy_mode": "llm",
        "configured_policy_mode": "llm",
        "policy_identity_valid": True,
        "policy_switches": [],
        "model_calls": 2,
        "effective_backend_requests": [
            {
                "decision_id": "decision-1",
                "effective_backend": "openai:bedrock-claude-4-6-sonnet",
                "backend_provenance_source": "response_metadata.model_name",
            },
            {
                "decision_id": "decision-2",
                "effective_backend": "openai:bedrock-claude-4-6-sonnet",
                "backend_provenance_source": "response_metadata.model_name",
            },
        ],
        "backend_provenance_complete": True,
        "semantic_transaction_count": 2,
        "authorized_transaction_count": 2,
        "semantic_policy_coverage": 1.0,
    }
    value.update(overrides)
    return value


def test_runtime_telemetry_validation_accepts_complete_provenance():
    rgl.validate_harness_runtime_telemetry(
        "llm",
        _telemetry(
            model_provider="openai",
            model_id="bedrock-claude-4-6-sonnet",
        ),
        configured_provider="openai",
        configured_model="bedrock-claude-4-6-sonnet",
    )


def test_runtime_telemetry_validation_rejects_mislabeled_model():
    with pytest.raises(RuntimeError, match="did not match observed model"):
        rgl.validate_harness_runtime_telemetry(
            "llm",
            _telemetry(model_provider="openai", model_id="gpt-5.4-mini"),
            configured_provider="openai",
            configured_model="bedrock-claude-4-6-sonnet",
        )


def test_runtime_telemetry_validation_rejects_response_backend_route_drift():
    telemetry = _telemetry(
        model_provider="openai",
        model_id="bedrock-claude-4-6-sonnet",
    )
    for request in telemetry["effective_backend_requests"]:
        request["effective_backend"] = "unexpected-provider:unexpected-model"

    with pytest.raises(RuntimeError, match="response-derived effective backend differs"):
        rgl.validate_harness_runtime_telemetry(
            "llm",
            telemetry,
            configured_provider="openai",
            configured_model="bedrock-claude-4-6-sonnet",
        )


def test_gate1p_run_side_rejects_inherited_intervention_before_live_seams(monkeypatch):
    monkeypatch.setenv(
        "SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON",
        json.dumps([{"capability": "adcs-ca-private-key-export"}]),
    )

    with pytest.raises(rgl.gate1p_stagea.ProtocolError, match="incompatible evaluation override"):
        rgl.run_side(
            rgl.Config(measurement_kind="row"),
            "harness",
            rgl.gate1p_stagea.SCENARIO,
        )


def test_runtime_telemetry_validation_rejects_policy_mismatch():
    with pytest.raises(RuntimeError, match="did not match"):
        rgl.validate_harness_runtime_telemetry(
            "symbolic",
            _telemetry(policy_mode="llm"),
        )


def test_runtime_telemetry_validation_rejects_recorded_policy_switch():
    with pytest.raises(RuntimeError, match="policy identity invalid"):
        rgl.validate_harness_runtime_telemetry(
            "llm",
            _telemetry(
                policy_identity_valid=False,
                policy_switches=[{
                    "configured_policy_mode": "llm",
                    "observed_policy_mode": "symbolic",
                }],
            ),
        )


def test_runtime_telemetry_validation_rejects_missing_switch_records():
    telemetry = _telemetry()
    telemetry.pop("policy_switches")
    with pytest.raises(RuntimeError, match="omitted policy switch"):
        rgl.validate_harness_runtime_telemetry("llm", telemetry)


def test_runtime_telemetry_validation_rejects_incomplete_coverage():
    with pytest.raises(RuntimeError, match="1/2"):
        rgl.validate_harness_runtime_telemetry(
            "llm",
            _telemetry(
                authorized_transaction_count=1,
                semantic_policy_coverage=0.5,
            ),
        )


def test_runtime_telemetry_validation_rejects_missing_record():
    with pytest.raises(RuntimeError, match="no observed"):
        rgl.validate_harness_runtime_telemetry("llm", {})


def test_runtime_telemetry_validation_rejects_missing_effective_backend_provenance():
    with pytest.raises(RuntimeError, match="response-derived effective backend"):
        rgl.validate_harness_runtime_telemetry(
            "llm",
            _telemetry(
                effective_backend_requests=[
                    {
                        "decision_id": "decision-1",
                        "effective_backend": "",
                        "backend_provenance_source": "unavailable",
                    },
                    {
                        "decision_id": "decision-2",
                        "effective_backend": "openai:bedrock-claude-4-6-sonnet",
                        "backend_provenance_source": "response_metadata.model_name",
                    },
                ],
                backend_provenance_complete=False,
            ),
        )


def test_diagnostic_runtime_telemetry_allows_incomplete_response_provenance_but_not_wrong_model():
    incomplete = _telemetry(
        model_provider="openai",
        model_id="bedrock-claude-4-6-sonnet",
        effective_backend_requests=[],
        backend_provenance_complete=False,
    )
    rgl.validate_harness_runtime_telemetry(
        "llm",
        incomplete,
        configured_provider="openai",
        configured_model="bedrock-claude-4-6-sonnet",
        require_all_response_provenance=False,
    )

    with pytest.raises(RuntimeError, match="did not match observed model"):
        rgl.validate_harness_runtime_telemetry(
            "llm",
            {**incomplete, "model_id": "wrong-model"},
            configured_provider="openai",
            configured_model="bedrock-claude-4-6-sonnet",
            require_all_response_provenance=False,
        )


class _Scn:
    def __init__(self, subset):
        self.milestone_subset = subset


def test_config_results_path_honors_eval_override(tmp_path, monkeypatch):
    path = tmp_path / "purpose-range-clean.jsonl"
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(path))

    assert rgl.Config().results_path == path


def test_compare_passes_entire_gate1p_ledger_to_closed_envelope(tmp_path, monkeypatch):
    path = tmp_path / "pair.jsonl"
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(path))
    path.write_text(json.dumps({
        "record_type": "arm_started",
        "measurement_kind": "row",
        "protocol": {
            "protocol_id": rgl.gate1p_stagea.PROTOCOL_ID,
            "pair_id": "other-pair",
            "arm": "shipping_sage",
        },
    }) + "\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="closed envelope"):
        rgl.compare(
            rgl.Config(pair_id=rgl.gate1p_stagea.PAIR_ID),
            rgl.gate1p_stagea.SCENARIO,
        )


def test_compare_rejects_malformed_top_level_json_before_record_derivation(tmp_path, monkeypatch):
    path = tmp_path / "pair.jsonl"
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(path))
    path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="malformed record"):
        rgl.compare(
            rgl.Config(pair_id=rgl.gate1p_stagea.PAIR_ID),
            rgl.gate1p_stagea.SCENARIO,
        )


def test_compare_admits_complete_gate1p_ledger_before_constructing_cards(tmp_path, monkeypatch):
    path = tmp_path / "pair.jsonl"
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(path))
    records = [
        {
            "record_type": "result",
            "measurement_kind": "row",
            "scenario": rgl.gate1p_stagea.SCENARIO,
            "protocol": {
                "protocol_id": rgl.gate1p_stagea.PROTOCOL_ID,
                "pair_id": rgl.gate1p_stagea.PAIR_ID,
                "arm": arm,
            },
            "side": side,
            "card": {},
            "cost_countable": {},
        }
        for arm, side in (("shipping_sage", "harness"), ("true_naive", "bare"))
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    calls = []

    projected = [
        {
            "arm": arm,
            "side": side,
            "card": {"projection": arm},
            "cost_countable": {"model_calls": True},
        }
        for arm, side in (("shipping_sage", "harness"), ("true_naive", "bare"))
    ]

    def _admit(ledger):
        calls.append(("admit", ledger))
        return {"countable": True, "comparison_inputs": projected}

    class _Card:
        def __init__(self, **kwargs):
            assert calls and calls[0][0] == "admit"
            calls.append(("card", kwargs))

    class _Comparison:
        verdict = "test"
        delta = 0.0
        harness_objective_success_rate = 0.0
        bare_objective_success_rate = 0.0
        min_detectable_effect = 1.0

    monkeypatch.setattr(rgl.gate1p_stagea, "pair_disposition", _admit)
    monkeypatch.setattr(rgl, "ScoreCard", _Card)
    monkeypatch.setattr(
        rgl.bare_runner,
        "compare_bare_vs_harness",
        lambda *_args, **_kwargs: _Comparison(),
    )

    rgl.compare(
        rgl.Config(pair_id=rgl.gate1p_stagea.PAIR_ID),
        rgl.gate1p_stagea.SCENARIO,
    )

    assert [name for name, _value in calls] == ["admit", "card", "card"]
    assert [value for name, value in calls if name == "card"] == [
        {"projection": "shipping_sage"},
        {"projection": "true_naive"},
    ]


def test_compare_preserves_ordinary_non_gate_record_behavior(tmp_path, monkeypatch):
    path = tmp_path / "ordinary.jsonl"
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(path))
    records = [
        {
            "record_type": "result",
            "scenario": "ordinary-scenario",
            "side": side,
            "card": {"raw": side},
            "cost_countable": {"wall_seconds": True},
        }
        for side in ("harness", "bare")
    ]
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    constructed = []

    class _Card:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    class _Comparison:
        verdict = "test"
        delta = 0.0
        harness_objective_success_rate = 0.0
        bare_objective_success_rate = 0.0
        min_detectable_effect = 1.0

    monkeypatch.setattr(rgl, "ScoreCard", _Card)
    monkeypatch.setattr(
        rgl.bare_runner,
        "compare_bare_vs_harness",
        lambda *_args, **_kwargs: _Comparison(),
    )

    rgl.compare(rgl.Config(), "ordinary-scenario")

    assert constructed == [{"raw": "harness"}, {"raw": "bare"}]


def test_pair_arm_retry_ledger_ignores_prestart_snapshot_but_blocks_started_arm(tmp_path):
    path = tmp_path / "pair.jsonl"
    protocol = {"pair_id": rgl.gate1p_stagea.PAIR_ID, "arm": "shipping_sage"}
    rgl._append_jsonl(path, {
        "record_type": "pre_run_protocol_snapshot",
        "protocol": protocol,
    })
    assert rgl._pair_arm_has_started(
        path,
        pair_id=rgl.gate1p_stagea.PAIR_ID,
        arm="shipping_sage",
    ) is False

    rgl._append_jsonl(path, {"record_type": "arm_started", "protocol": protocol})
    assert rgl._pair_arm_has_started(
        path,
        pair_id=rgl.gate1p_stagea.PAIR_ID,
        arm="shipping_sage",
    ) is True


def test_headless_harness_route_uses_sage_defaults_when_no_treatment_override(monkeypatch):
    monkeypatch.setattr(
        rgl.live_seams,
        "load_sage_defaults",
        lambda: {
            "provider": "openai",
            "model": "default-model",
            "api_key": "default-key",
            "base_url": "http://127.0.0.1:8100/v1",
        },
    )

    assert rgl._resolved_harness_model_route(rgl.Config()) == {
        "provider": "openai",
        "model": "default-model",
        "api_endpoint": "http://127.0.0.1:8100/v1",
        "api_key": "default-key",
    }


def test_headless_harness_route_prefers_explicit_treatment_override(monkeypatch):
    monkeypatch.setattr(
        rgl.live_seams,
        "load_sage_defaults",
        lambda: {
            "provider": "openai",
            "model": "default-model",
            "api_key": "default-key",
            "base_url": "http://127.0.0.1:8100/v1",
        },
    )
    cfg = rgl.Config(
        model_provider="bedrock",
        model_id="strong-model",
        model_api_endpoint="https://bedrock-proxy.example/v1",
        model_api_key="treatment-key",
    )

    assert rgl._resolved_harness_model_route(cfg) == {
        "provider": "bedrock",
        "model": "strong-model",
        "api_endpoint": "https://bedrock-proxy.example/v1",
        "api_key": "treatment-key",
    }


def test_native_harness_threads_eval_force_prefix_into_channel_solver(tmp_path, monkeypatch):
    from types import SimpleNamespace

    raw = '[{"capability":"read-managed-local-admin-secret","exact_target":"target=birch-ops01"}]'
    monkeypatch.setenv("SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON", raw)
    monkeypatch.setenv("SAGE_EVAL_PHASE6_PLANNED_ROW_ID", "forced-maple-willow-r1")
    monkeypatch.setenv("SAGE_EVAL_PHASE6_ATTEMPT_INDEX", "2")
    monkeypatch.setenv("SAGE_EVAL_PHASE6_MAX_PRE_FRONTIER_DIAGNOSTIC_RETRIES", "1")
    monkeypatch.setenv("SAGE_EVAL_PHASE8_CONTRACT_HASH", "sha256:phase8-contract")
    monkeypatch.setenv("SAGE_EVAL_PHASE8_POLICY_ARM", "symbolic")
    monkeypatch.setenv("SAGE_EVAL_PHASE8_PLANNED_ROW_ID", "phase8-symbolic-seed-1")
    monkeypatch.setenv("SAGE_EVAL_PHASE8_ATTEMPT_INDEX", "1")
    monkeypatch.delenv("SAGE_EVAL_HEADLESS", raising=False)
    results_path = tmp_path / "native-prefix.jsonl"
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(results_path))
    monkeypatch.setattr(
        rgl,
        "_scenario",
        lambda _cfg, _name: SimpleNamespace(objective="objective"),
    )
    monkeypatch.setattr(rgl.live_seams, "default_mythic_client", lambda: "CLIENT")
    monkeypatch.setattr(rgl, "_scored_referee_domains", lambda _scn: set())
    monkeypatch.setattr(rgl, "build_probes", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        rgl,
        "_resolved_harness_model_route",
        lambda _cfg: {
            "provider": None,
            "model": None,
            "api_endpoint": None,
            "api_key": None,
        },
    )
    monkeypatch.setattr(rgl.live_seams, "load_sage_defaults", lambda: {})
    monkeypatch.setattr(rgl, "validate_harness_runtime_telemetry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rgl, "asdict", lambda _card: {})
    seen = {}

    def _make_solver(_client, **kwargs):
        seen.update(kwargs)

        def _solve(_objective):
            return "completed"

        _solve.last_result = {"runtime_telemetry": {}}
        return _solve

    monkeypatch.setattr(rgl.live_seams, "make_native_chat_solver", _make_solver)
    monkeypatch.setattr(
        rgl.bare_runner,
        "score_from_probes",
        lambda *_args, **_kwargs: SimpleNamespace(
            policy_mode="symbolic",
            configured_policy_mode="symbolic",
            policy_identity_valid=True,
            request_completed=True,
            objective_recognized=True,
            objective_proven=True,
            clean_stop=True,
            controller_terminal_reason="complete",
            semantic_transaction_count=1,
            authorized_transaction_count=1,
            semantic_policy_coverage=1.0,
            effective_backends=[],
            effective_backend_requests=[],
            capability=1.0,
            furthest_milestone="OBJECTIVE",
        ),
    )

    rgl.run_side(rgl.Config(policy_mode="symbolic", solve_timeout=1), "harness", "scenario")

    assert seen["eval_force_capability_prefix_json"] == raw
    row = json.loads(results_path.read_text(encoding="utf-8").strip())
    assert row["phase6_planned_row_id"] == "forced-maple-willow-r1"
    assert row["phase6_attempt_index"] == 2
    assert row["phase6_max_pre_frontier_diagnostic_retries"] == 1
    assert row["phase8_contract_hash"] == "sha256:phase8-contract"
    assert row["phase8_policy_arm"] == "symbolic"
    assert row["phase8_planned_row_id"] == "phase8-symbolic-seed-1"
    assert row["phase8_attempt_index"] == 1


def test_runtime_evidence_fields_persists_branch_and_cycle_provenance():
    evidence = rgl.runtime_evidence_fields(
        _telemetry(
            controller_status="complete",
            controller_cycle_count=2,
            controller_cycles=[{"cycle": 1, "action": "collect-graph"}, {"cycle": 2, "action": "gpo-controlled-system-exec"}],
            controller_blocker=None,
            achieved_effects=["domain-collected:range.local"],
            decisions=[{"selected_index": 0, "selected_family": "gpo-directory"}],
            transactions=[{"capability": "gpo-controlled-system-exec"}],
        )
    )

    assert evidence["controller_cycle_count"] == 2
    assert evidence["controller_cycles"][1]["action"] == "gpo-controlled-system-exec"
    assert evidence["decisions"][0]["selected_family"] == "gpo-directory"
    assert evidence["transactions"][0]["capability"] == "gpo-controlled-system-exec"


def test_build_probes_scopes_to_subset_child_da():
    """child-da scores DA_CHILD + KRBTGT but NOT OBJECTIVE -> no essos probe is built (so no essos
    query is issued and no essos referee creds are required)."""
    reader = lambda _d: set()
    scn = _Scn((Milestone.FOOTHOLD, Milestone.DA_CHILD, Milestone.KRBTGT_DUMPED))
    probes = rgl.build_probes(reader, {}, scn)
    assert Milestone.DA_CHILD in probes
    assert Milestone.KRBTGT_DUMPED in probes
    assert Milestone.OBJECTIVE not in probes


def test_build_probes_single_hop_uses_result_based_system_oracle(monkeypatch):
    seen = {}

    def system_probe(*, realm, host=None, **_kwargs):
        seen.update({"realm": realm, "host": host})
        return lambda: True

    monkeypatch.setattr(rgl.live_seams, "system_execution_probe", system_probe)
    scn = _Scn((Milestone.SYSTEM_ON_HOST,))
    probes = rgl.build_probes(lambda _domain: set(), {}, scn)
    assert probes[Milestone.SYSTEM_ON_HOST]() is True
    assert Milestone.KRBTGT_DUMPED not in probes
    assert seen == {"realm": rgl.CHILD, "host": None}


def test_diagnostic_dimensions_are_non_countable_and_preserve_missing_cost():
    from types import SimpleNamespace

    card = SimpleNamespace(
        probe_disagreements=[],
        objective_proven=True,
        objective_recognized=False,
        objective_clean_stop=False,
        clean_stop=False,
        tool_calls=3,
        model_calls=4,
        total_tokens=0,
        wall_seconds=12.0,
    )
    dimensions = rgl.measurement_dimensions(
        card,
        measurement_kind="diagnostic",
        cost_countable={
            "tool_calls": True,
            "model_calls": True,
            "total_tokens": False,
            "wall_seconds": True,
        },
        stop_observation_available=True,
    )
    assert dimensions["outcome"]["countable"] is False
    assert dimensions["outcome"]["objective_success"] is True
    assert dimensions["outcome"]["clean_stop"] is False
    assert dimensions["outcome"]["objective_recognized"] is False
    assert dimensions["outcome"]["objective_clean_stop"] is False
    assert dimensions["comparison"]["countable"] is False
    assert dimensions["cost"]["total_tokens"] == {"countable": False, "value": None}


def test_gate1p_row_mode_requires_exact_protocol_before_any_live_client(monkeypatch):
    monkeypatch.setattr(
        rgl.live_seams,
        "default_mythic_client",
        lambda: pytest.fail("live client must not be touched"),
    )
    with pytest.raises(rgl.gate1p_stagea.ProtocolError, match="protocol_id must be"):
        rgl.run_side(
            rgl.Config(measurement_kind="row"),
            "harness",
            "child-domain-control",
        )


def test_gate1p_row_mode_requires_separate_live_authority_before_any_live_client(monkeypatch):
    monkeypatch.setattr(
        rgl.live_seams,
        "default_mythic_client",
        lambda: pytest.fail("live client must not be touched"),
    )
    with pytest.raises(rgl.gate1p_stagea.ProtocolError, match="separate authority ID"):
        rgl.run_side(
            rgl.Config(
                measurement_kind="row",
                protocol_id=rgl.gate1p_stagea.PROTOCOL_ID,
                pair_id=rgl.gate1p_stagea.PAIR_ID,
                arm_index=0,
                model_provider=rgl.gate1p_stagea.MODEL_PROVIDER,
                model_id=rgl.gate1p_stagea.MODEL_ID,
                model_api_endpoint="https://provider.invalid/v1",
                model_context_limit=200_000,
                max_output_tokens=8_192,
                source_version="tree:test",
            ),
            "harness",
            rgl.gate1p_stagea.SCENARIO,
        )


@pytest.mark.parametrize(("side", "arm_index"), [("harness", 0), ("bare", 1)])
def test_gate1p_direct_row_entrypoint_refuses_without_pair_orchestrator_before_live_client(
    monkeypatch, tmp_path, side, arm_index,
):
    monkeypatch.delenv("SAGE_EVAL_PAIR_CONTROL_PATH", raising=False)
    monkeypatch.setenv("SAGE_EVAL_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setattr(rgl, "_validate_gate1p_artifact_destinations", lambda *_args: tmp_path / "episode.json")
    monkeypatch.setattr(
        rgl.live_seams,
        "default_mythic_client",
        lambda: pytest.fail("live client must not be touched"),
    )
    cfg = rgl.Config(
        measurement_kind="row",
        protocol_id=rgl.gate1p_stagea.PROTOCOL_ID,
        pair_id=rgl.gate1p_stagea.PAIR_ID,
        arm_index=arm_index,
        model_provider=rgl.gate1p_stagea.MODEL_PROVIDER,
        model_id=rgl.gate1p_stagea.MODEL_ID,
        model_api_endpoint="https://provider.invalid/v1",
        model_context_limit=200_000,
        max_output_tokens=8_192,
        source_version="tree:test",
        live_authority_id=rgl.gate1p_stagea.PAIR_ID,
    )

    with pytest.raises(rgl.gate1p_stagea.ProtocolError, match="pair orchestrator"):
        rgl.run_side(cfg, side, rgl.gate1p_stagea.SCENARIO)


def test_gate1p_naive_child_recomputes_shipping_lifecycle_before_live_client(monkeypatch, tmp_path):
    history = tmp_path / ".sage_history"
    artifact_dir = history / "evaluation" / "pair"
    artifact_dir.mkdir(parents=True)
    results = artifact_dir / "results.jsonl"
    control = artifact_dir / "pair-control.json"
    control.write_text(json.dumps({
        "schema": "gate1p-stagea-pair-control-v3",
        "protocol_id": rgl.gate1p_stagea.PROTOCOL_ID,
        "pair_id": rgl.gate1p_stagea.PAIR_ID,
        "source_version": "tree:test",
        "next_arm": "true_naive",
        "completed_arms": ["shipping_sage"],
        "prearm_state": {
            "arm": "true_naive",
            "status": "reset_started",
            "reset_generation": 1,
            "updated_at": 1.0,
        },
        "created_at": 1.0,
        "updated_at": 1.0,
    }), encoding="utf-8")
    monkeypatch.setattr(rgl, "_private_history_root", lambda: history)
    monkeypatch.setenv("SAGE_EVAL_ARTIFACT_DIR", str(artifact_dir))
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(results))
    monkeypatch.setenv("SAGE_EVAL_PAIR_CONTROL_PATH", str(control))
    monkeypatch.setattr(
        rgl.live_seams,
        "default_mythic_client",
        lambda: pytest.fail("live client must not be touched"),
    )
    cfg = rgl.Config(
        measurement_kind="row",
        protocol_id=rgl.gate1p_stagea.PROTOCOL_ID,
        pair_id=rgl.gate1p_stagea.PAIR_ID,
        arm_index=1,
        model_provider=rgl.gate1p_stagea.MODEL_PROVIDER,
        model_id=rgl.gate1p_stagea.MODEL_ID,
        model_api_endpoint="https://provider.invalid/v1",
        model_context_limit=200_000,
        max_output_tokens=8_192,
        source_version="tree:test",
        live_authority_id=rgl.gate1p_stagea.PAIR_ID,
    )

    with pytest.raises(rgl.gate1p_stagea.ProtocolError, match="shipping admission"):
        rgl.run_side(cfg, "bare", rgl.gate1p_stagea.SCENARIO)


def test_gate1p_child_rejects_idle_prearm_state_before_live_client(monkeypatch, tmp_path):
    history = tmp_path / ".sage_history"
    artifact_dir = history / "evaluation" / "pair"
    artifact_dir.mkdir(parents=True)
    control = artifact_dir / "pair-control.json"
    control.write_text(json.dumps({
        "schema": "gate1p-stagea-pair-control-v3",
        "protocol_id": rgl.gate1p_stagea.PROTOCOL_ID,
        "pair_id": rgl.gate1p_stagea.PAIR_ID,
        "source_version": "tree:test",
        "next_arm": "shipping_sage",
        "completed_arms": [],
        "prearm_state": {
            "arm": "shipping_sage",
            "status": "idle",
            "reset_generation": 0,
            "updated_at": 1.0,
        },
        "created_at": 1.0,
        "updated_at": 1.0,
    }), encoding="utf-8")
    monkeypatch.setattr(rgl, "_private_history_root", lambda: history)
    monkeypatch.setenv("SAGE_EVAL_ARTIFACT_DIR", str(artifact_dir))
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(artifact_dir / "results.jsonl"))
    monkeypatch.setenv("SAGE_EVAL_PAIR_CONTROL_PATH", str(control))
    monkeypatch.setattr(
        rgl.live_seams,
        "default_mythic_client",
        lambda: pytest.fail("live client must not be touched"),
    )
    cfg = rgl.Config(
        measurement_kind="row",
        protocol_id=rgl.gate1p_stagea.PROTOCOL_ID,
        pair_id=rgl.gate1p_stagea.PAIR_ID,
        arm_index=0,
        model_provider=rgl.gate1p_stagea.MODEL_PROVIDER,
        model_id=rgl.gate1p_stagea.MODEL_ID,
        model_api_endpoint="https://provider.invalid/v1",
        model_context_limit=200_000,
        max_output_tokens=8_192,
        source_version="tree:test",
        live_authority_id=rgl.gate1p_stagea.PAIR_ID,
    )

    with pytest.raises(rgl.gate1p_stagea.ProtocolError, match="does not prove reset start"):
        rgl.run_side(cfg, "harness", rgl.gate1p_stagea.SCENARIO)


def test_gate1p_real_orchestrator_control_reaches_live_child_client_seam(monkeypatch, tmp_path):
    history = tmp_path / ".sage_history"
    artifact_dir = history / "evaluation" / "pair"
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(artifact_dir)}
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage",
        source_version="tree:test",
        route_env=route_env,
    )
    monkeypatch.setattr(rgl, "_private_history_root", lambda: history)
    monkeypatch.setenv("SAGE_EVAL_ARTIFACT_DIR", str(artifact_dir))
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(artifact_dir / "results.jsonl"))
    monkeypatch.setenv("SAGE_EVAL_PAIR_CONTROL_PATH", str(control))
    monkeypatch.setattr(
        rgl.live_seams,
        "default_mythic_client",
        lambda: (_ for _ in ()).throw(RuntimeError("assembled client seam reached")),
    )
    cfg = rgl.Config(
        measurement_kind="row",
        protocol_id=rgl.gate1p_stagea.PROTOCOL_ID,
        pair_id=rgl.gate1p_stagea.PAIR_ID,
        arm_index=0,
        model_provider=rgl.gate1p_stagea.MODEL_PROVIDER,
        model_id=rgl.gate1p_stagea.MODEL_ID,
        model_api_endpoint="https://provider.invalid/v1",
        model_api_key="dummy",
        model_context_limit=200_000,
        max_output_tokens=8_192,
        source_version="tree:test",
        live_authority_id=rgl.gate1p_stagea.PAIR_ID,
    )

    with pytest.raises(RuntimeError, match="assembled client seam reached"):
        rgl.run_side(cfg, "harness", rgl.gate1p_stagea.SCENARIO)


def _stageb_row_cfg(pair_id, *, arm_index=0):
    return rgl.Config(
        measurement_kind="row",
        protocol_id=rgl.gate1p_stageb.PROTOCOL_ID,
        pair_id=pair_id,
        arm_index=arm_index,
        model_provider=rgl.gate1p_stageb.MODEL_PROVIDER,
        model_id=rgl.gate1p_stageb.MODEL_ID,
        model_api_endpoint="https://provider.invalid/v1",
        model_api_key="dummy",
        policy_mode=rgl.gate1p_stageb.POLICY_MODE,
        solve_timeout=rgl.gate1p_stageb.WALL_SECONDS,
        model_token_budget=rgl.gate1p_stageb.TOKEN_ADMISSION_CEILING,
        model_context_limit=rgl.gate1p_stageb.MODEL_CONTEXT_LIMIT,
        max_output_tokens=rgl.gate1p_stageb.MAX_OUTPUT_TOKENS,
        source_version="tree:test",
        live_authority_id=rgl.gate1p_stageb.ALLOCATION_ID,
    )


@pytest.mark.parametrize(
    "terminal_pair_id",
    tuple(f"gate1p-stageb-child-control-pair-{index:03d}" for index in range(1, 8)),
)
def test_stageb_terminal_pair_rejects_before_live_child_client(monkeypatch, terminal_pair_id):
    monkeypatch.setattr(
        rgl.live_seams,
        "default_mythic_client",
        lambda: pytest.fail("live client must not be touched"),
    )

    with pytest.raises(rgl.gate1p_stagea.ProtocolError, match="pair_id must be one of"):
        rgl.run_side(
            _stageb_row_cfg(terminal_pair_id),
            "harness",
            rgl.gate1p_stageb.SCENARIO,
        )


@pytest.mark.parametrize(
    "pair_id",
    (rgl.gate1p_stageb.PRIMARY_PAIR_IDS[0], rgl.gate1p_stageb.RESERVE_PAIR_IDS[-1]),
)
def test_stageb_real_orchestrator_control_reaches_live_child_client_seam(
    monkeypatch, tmp_path, pair_id,
):
    history = tmp_path / ".sage_history"
    artifact_dir = history / "evaluation" / "stageb" / pair_id
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(artifact_dir)}
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
        protocol=rgl.gate1p_stageb,
        pair_id=pair_id,
    )
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage",
        source_version="tree:test",
        route_env=route_env,
        protocol=rgl.gate1p_stageb,
        pair_id=pair_id,
    )
    monkeypatch.setattr(rgl, "_private_history_root", lambda: history)
    monkeypatch.setenv("SAGE_EVAL_ARTIFACT_DIR", str(artifact_dir))
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(artifact_dir / "results.jsonl"))
    monkeypatch.setenv("SAGE_EVAL_PAIR_CONTROL_PATH", str(control))
    monkeypatch.setattr(
        rgl.live_seams,
        "default_mythic_client",
        lambda: (_ for _ in ()).throw(RuntimeError("stageb assembled client seam reached")),
    )

    with pytest.raises(RuntimeError, match="stageb assembled client seam reached"):
        rgl.run_side(_stageb_row_cfg(pair_id), "harness", rgl.gate1p_stageb.SCENARIO)


@pytest.mark.parametrize(
    "mutation",
    ("stagea_schema", "wrong_protocol", "cross_pair", "wrong_source", "nonfinite_time", "cleanup_complete"),
)
def test_stageb_orchestrator_control_mutations_stop_before_live_client(
    monkeypatch, tmp_path, mutation,
):
    pair_id = rgl.gate1p_stageb.PRIMARY_PAIR_IDS[0]
    history = tmp_path / ".sage_history"
    artifact_dir = history / "evaluation" / "stageb" / pair_id
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(artifact_dir)}
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
        protocol=rgl.gate1p_stageb,
        pair_id=pair_id,
    )
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage",
        source_version="tree:test",
        route_env=route_env,
        protocol=rgl.gate1p_stageb,
        pair_id=pair_id,
    )
    state = json.loads(control.read_text(encoding="utf-8"))
    if mutation == "stagea_schema":
        state["schema"] = "gate1p-stagea-pair-control-v3"
    elif mutation == "wrong_protocol":
        state["protocol_id"] = rgl.gate1p_stagea.PROTOCOL_ID
    elif mutation == "cross_pair":
        state["pair_id"] = rgl.gate1p_stageb.PRIMARY_PAIR_IDS[1]
    elif mutation == "wrong_source":
        state["source_version"] = "tree:drifted"
    elif mutation == "nonfinite_time":
        state["created_at"] = float("inf")
    else:
        state["prearm_state"]["status"] = "cleanup_complete"
    control.write_text(json.dumps(state) + "\n", encoding="utf-8")

    monkeypatch.setattr(rgl, "_private_history_root", lambda: history)
    monkeypatch.setenv("SAGE_EVAL_ARTIFACT_DIR", str(artifact_dir))
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(artifact_dir / "results.jsonl"))
    monkeypatch.setenv("SAGE_EVAL_PAIR_CONTROL_PATH", str(control))
    monkeypatch.setattr(
        rgl.live_seams,
        "default_mythic_client",
        lambda: pytest.fail("invalid Stage B control must stop before live client"),
    )

    with pytest.raises(rgl.gate1p_stagea.ProtocolError):
        rgl.run_side(_stageb_row_cfg(pair_id), "harness", rgl.gate1p_stageb.SCENARIO)


def test_stageb_naive_child_recomputes_pair_specific_shipping_admission(monkeypatch, tmp_path):
    pair_id = rgl.gate1p_stageb.PRIMARY_PAIR_IDS[0]
    history = tmp_path / ".sage_history"
    artifact_dir = history / "evaluation" / "stageb" / pair_id
    artifact_dir.mkdir(parents=True)
    control = artifact_dir / "pair-control.json"
    control.write_text(json.dumps({
        "schema": rgl.gate1p_stageb.PAIR_CONTROL_SCHEMA,
        "protocol_id": rgl.gate1p_stageb.PROTOCOL_ID,
        "pair_id": pair_id,
        "source_version": "tree:test",
        "next_arm": "true_naive",
        "completed_arms": ["shipping_sage"],
        "prearm_state": {
            "arm": "true_naive",
            "status": "reset_started",
            "reset_generation": 1,
            "updated_at": 1.0,
        },
        "created_at": 1.0,
        "updated_at": 1.0,
    }) + "\n", encoding="utf-8")
    observed = {}

    def shipping_disposition(records, *, arm, pair_id, history_root):
        observed.update(records=records, arm=arm, pair_id=pair_id, history_root=history_root)
        return {"countable": False, "reason": "test non-countable shipping lifecycle"}

    monkeypatch.setattr(
        rgl.gate1p_stageb,
        "completed_arm_lifecycle_disposition",
        shipping_disposition,
    )
    monkeypatch.setattr(rgl, "_private_history_root", lambda: history)
    monkeypatch.setenv("SAGE_EVAL_ARTIFACT_DIR", str(artifact_dir))
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(artifact_dir / "results.jsonl"))
    monkeypatch.setenv("SAGE_EVAL_PAIR_CONTROL_PATH", str(control))
    monkeypatch.setattr(
        rgl.live_seams,
        "default_mythic_client",
        lambda: pytest.fail("non-countable shipping must stop before live client"),
    )

    with pytest.raises(rgl.gate1p_stagea.ProtocolError, match="shipping admission"):
        rgl.run_side(
            _stageb_row_cfg(pair_id, arm_index=1), "bare", rgl.gate1p_stageb.SCENARIO,
        )

    assert observed["arm"] == "shipping_sage"
    assert observed["pair_id"] == pair_id
    assert observed["records"] == []
    assert observed["history_root"] == history


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("schema", "gate1p-stagea-pair-control-v2"),
        ("schema", rgl.gate1p_stageb.PAIR_CONTROL_SCHEMA),
        ("missing_generation", None),
        ("generation", True),
        ("generation", 1.0),
        ("generation", 0),
        ("generation", -1),
    ],
)
def test_gate1p_real_orchestrator_control_mutations_stop_before_live_client(
    monkeypatch,
    tmp_path,
    mutation,
    value,
):
    history = tmp_path / ".sage_history"
    artifact_dir = history / "evaluation" / "pair"
    route_env = {"SAGE_EVAL_ARTIFACT_DIR": str(artifact_dir)}
    control = orchestrate.initialize_gate1p_pair_control(
        source_version="tree:test",
        route_env=route_env,
    )
    orchestrate.mark_gate1p_prearm_reset_started(
        arm="shipping_sage",
        source_version="tree:test",
        route_env=route_env,
    )
    state = json.loads(control.read_text(encoding="utf-8"))
    if mutation == "schema":
        state["schema"] = value
    elif mutation == "missing_generation":
        del state["prearm_state"]["reset_generation"]
    else:
        state["prearm_state"]["reset_generation"] = value
    control.write_text(json.dumps(state) + "\n", encoding="utf-8")

    monkeypatch.setattr(rgl, "_private_history_root", lambda: history)
    monkeypatch.setenv("SAGE_EVAL_ARTIFACT_DIR", str(artifact_dir))
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(artifact_dir / "results.jsonl"))
    monkeypatch.setenv("SAGE_EVAL_PAIR_CONTROL_PATH", str(control))
    monkeypatch.setattr(
        rgl.live_seams,
        "default_mythic_client",
        lambda: pytest.fail("invalid producer control must stop before live client"),
    )
    cfg = rgl.Config(
        measurement_kind="row",
        protocol_id=rgl.gate1p_stagea.PROTOCOL_ID,
        pair_id=rgl.gate1p_stagea.PAIR_ID,
        arm_index=0,
        model_provider=rgl.gate1p_stagea.MODEL_PROVIDER,
        model_id=rgl.gate1p_stagea.MODEL_ID,
        model_api_endpoint="https://provider.invalid/v1",
        model_api_key="dummy",
        model_context_limit=200_000,
        max_output_tokens=8_192,
        source_version="tree:test",
        live_authority_id=rgl.gate1p_stagea.PAIR_ID,
    )

    with pytest.raises(rgl.gate1p_stagea.ProtocolError):
        rgl.run_side(cfg, "harness", rgl.gate1p_stagea.SCENARIO)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_top_level",
        "missing_top_level",
        "created_at_object",
        "created_at_bool",
        "created_at_nan",
        "created_at_pos_inf",
        "created_at_neg_inf",
        "stale_schema",
        "extra_prearm_field",
        "missing_prearm_field",
        "reset_generation_bool",
        "reset_generation_float",
        "reset_generation_zero",
        "reset_generation_negative",
        "illegal_arm_combination",
        "source_version_drift",
    ],
)
def test_gate1p_child_rejects_same_noncanonical_pair_control_class(
    monkeypatch,
    tmp_path,
    mutation,
):
    history = tmp_path / ".sage_history"
    artifact_dir = history / "evaluation" / "pair"
    artifact_dir.mkdir(parents=True)
    control = artifact_dir / "pair-control.json"
    state = {
        "schema": "gate1p-stagea-pair-control-v3",
        "protocol_id": rgl.gate1p_stagea.PROTOCOL_ID,
        "pair_id": rgl.gate1p_stagea.PAIR_ID,
        "source_version": "tree:test",
        "next_arm": "shipping_sage",
        "completed_arms": [],
        "prearm_state": {
            "arm": "shipping_sage",
            "status": "reset_started",
            "reset_generation": 1,
            "updated_at": 1.0,
        },
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    if mutation == "extra_top_level":
        state["undeclared"] = None
    elif mutation == "missing_top_level":
        del state["updated_at"]
    elif mutation == "created_at_object":
        state["created_at"] = {}
    elif mutation == "created_at_bool":
        state["created_at"] = True
    elif mutation == "created_at_nan":
        state["created_at"] = float("nan")
    elif mutation == "created_at_pos_inf":
        state["created_at"] = float("inf")
    elif mutation == "created_at_neg_inf":
        state["created_at"] = float("-inf")
    elif mutation == "stale_schema":
        state["schema"] = "gate1p-stagea-pair-control-v2"
    elif mutation == "extra_prearm_field":
        state["prearm_state"]["undeclared"] = None
    elif mutation == "missing_prearm_field":
        del state["prearm_state"]["updated_at"]
    elif mutation == "reset_generation_bool":
        state["prearm_state"]["reset_generation"] = True
    elif mutation == "reset_generation_float":
        state["prearm_state"]["reset_generation"] = 1.0
    elif mutation == "reset_generation_zero":
        state["prearm_state"]["reset_generation"] = 0
    elif mutation == "reset_generation_negative":
        state["prearm_state"]["reset_generation"] = -1
    elif mutation == "illegal_arm_combination":
        state["next_arm"] = "true_naive"
        state["prearm_state"]["arm"] = "true_naive"
    elif mutation == "source_version_drift":
        state["source_version"] = "tree:drifted"
    control.write_text(json.dumps(state) + "\n", encoding="utf-8")
    monkeypatch.setattr(rgl, "_private_history_root", lambda: history)
    monkeypatch.setenv("SAGE_EVAL_ARTIFACT_DIR", str(artifact_dir))
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(artifact_dir / "results.jsonl"))
    monkeypatch.setenv("SAGE_EVAL_PAIR_CONTROL_PATH", str(control))
    cfg = rgl.Config(
        measurement_kind="row",
        protocol_id=rgl.gate1p_stagea.PROTOCOL_ID,
        pair_id=rgl.gate1p_stagea.PAIR_ID,
        arm_index=0,
        model_provider=rgl.gate1p_stagea.MODEL_PROVIDER,
        model_id=rgl.gate1p_stagea.MODEL_ID,
        model_api_endpoint="https://provider.invalid/v1",
        model_context_limit=200_000,
        max_output_tokens=8_192,
        source_version="tree:test",
        live_authority_id=rgl.gate1p_stagea.PAIR_ID,
    )

    with pytest.raises(rgl.gate1p_stagea.ProtocolError):
        rgl._validate_gate1p_pair_control(
            cfg,
            "shipping_sage",
            protocol=rgl.gate1p_protocol.protocol_for_request(
                protocol_id=cfg.protocol_id,
                scenario=rgl.gate1p_stagea.SCENARIO,
            ),
        )


def test_missing_harness_cost_telemetry_is_unavailable_not_zero():
    from types import SimpleNamespace

    card = SimpleNamespace(
        probe_disagreements=[],
        objective_proven=True,
        objective_recognized=True,
        objective_clean_stop=True,
        clean_stop=True,
        tool_calls=0,
        model_calls=7,
        total_tokens=0,
        wall_seconds=12.0,
    )
    dimensions = rgl.measurement_dimensions(
        card,
        measurement_kind="diagnostic",
        cost_countable={
            "tool_calls": False,
            "model_calls": True,
            "total_tokens": False,
            "wall_seconds": True,
        },
        stop_observation_available=True,
    )

    assert dimensions["cost"]["tool_calls"] == {"countable": False, "value": None}
    assert dimensions["cost"]["total_tokens"] == {"countable": False, "value": None}


def _valid_row_observer(*, objective_success=False, task_quiescent=True, status="done"):
    events = [{
        "event_id": f"{rgl.gate1p_stagea.PAIR_ID}:shipping_sage:coordinator:1",
        "source": "coordinator",
        "kind": "arm_started",
        "sequence": 1,
        "monotonic_ns": 1,
    }]
    if objective_success:
        events.append({
            "event_id": f"{rgl.gate1p_stagea.PAIR_ID}:shipping_sage:oracle:2",
            "source": "oracle",
            "kind": "objective_positive",
            "sequence": 2,
            "monotonic_ns": 2,
        })
    sequence = len(events) + 1
    events.append({
        "event_id": f"{rgl.gate1p_stagea.PAIR_ID}:shipping_sage:wrapper:{sequence}",
        "source": "wrapper",
        "kind": "terminal",
        "sequence": sequence,
        "monotonic_ns": sequence,
        "status": status,
    })
    recognized = bool(objective_success)
    tasks = [] if task_quiescent else [{
        "operation_id": None,
        "display_id": 1,
        "completed": False,
        "callback": {"display_id": None, "host": None, "domain": None, "user": None},
    }]
    return {
            "observer_version": "gate1p-stagea-observer-facts-v22",
        "oracle": {
            "success": objective_success,
            "proof_class": "directory_delta" if objective_success else None,
            "summary": {
                "domain": "north.sevenkingdoms.local",
                "baseline_member_count": 1,
                "final_member_count": 2 if objective_success else 1,
                "proof_states": {
                    "directory_delta": objective_success,
                    "certificate_control": False,
                    "ticket_control": False,
                },
            },
            "artifact_pointer": ".sage_history/example.json",
        },
        "task_window_observed": True,
        "wrapper_objective_recognized": recognized,
        "tasks": tasks,
        "events": events,
    }


def _row_response_records(*, model_calls, total_tokens):
    base, remainder = divmod(total_tokens, model_calls)
    records = []
    for index in range(model_calls):
        total = base + (1 if index < remainder else 0)
        records.append({
            "response_id": f"response-{index + 1}",
            "effective_backend": "openai:effective",
            "backend_provenance_source": "response_metadata.model_name",
            "input_tokens": total - 1,
            "output_tokens": 1,
            "total_tokens": total,
        })
    return records


def test_gate1p_row_cost_vector_keeps_coordinates_independent():
    from types import SimpleNamespace

    card = SimpleNamespace(
        probe_disagreements=[],
        objective_proven=False,
        objective_recognized=False,
        objective_clean_stop=False,
        clean_stop=True,
        model_calls=3,
        total_tokens=120,
        wall_seconds=9.5,
    )
    dimensions = rgl.measurement_dimensions(
        card,
        measurement_kind="row",
        cost_countable={
            "model_calls": True,
            "total_tokens": True,
            "environment_actions": True,
            "wrapper_tool_invocations": True,
            "control_plane_reads": False,
            "wall_seconds": True,
        },
        cost_values={
            "model_calls": 3,
            "total_tokens": 120,
            "environment_actions": 2,
            "wrapper_tool_invocations": 4,
            "control_plane_reads": None,
            "wall_seconds": 9.5,
        },
        stop_observation_available=True,
        observer_result=_valid_row_observer(objective_success=False, task_quiescent=True),
        arm="shipping_sage",
        model_response_records=_row_response_records(model_calls=3, total_tokens=120),
        model_calls=3,
        token_ceiling=rgl.gate1p_stagea.TOKEN_ADMISSION_CEILING,
    )

    assert dimensions["outcome"]["countable"] is True
    assert dimensions["cost"]["environment_actions"] == {"countable": True, "value": 2}
    assert dimensions["cost"]["control_plane_reads"] == {"countable": False, "value": None}


def test_gate1p_row_dimensions_use_shared_observer_semantics_and_fail_closed():
    from types import SimpleNamespace

    observer = _valid_row_observer(objective_success=True, task_quiescent=True)
    observer["task_window_observed"] = False
    sequence = len(observer["events"]) + 1
    observer["events"].append({
        "event_id": (
            f"{rgl.gate1p_stagea.PAIR_ID}:shipping_sage:observer:{sequence}"
        ),
        "source": "observer",
        "kind": "lane_unavailable",
        "sequence": sequence,
        "monotonic_ns": sequence,
        "lane": "task",
        "reason": "producer_deadline",
    })
    card = SimpleNamespace(
        probe_disagreements=[],
        objective_proven=True,
        objective_recognized=True,
        objective_clean_stop=True,
        clean_stop=True,
        model_calls=1,
        total_tokens=10,
        wall_seconds=1.0,
    )

    dimensions = rgl.measurement_dimensions(
        card,
        measurement_kind="row",
        cost_countable={"model_calls": True},
        stop_observation_available=True,
        observer_result=observer,
        arm="shipping_sage",
        model_response_records=_row_response_records(model_calls=1, total_tokens=10),
        model_calls=1,
        token_ceiling=rgl.gate1p_stagea.TOKEN_ADMISSION_CEILING,
    )

    assert dimensions["outcome"]["countable"] is True
    assert dimensions["outcome"]["stop_countable"] is False
    assert "task-stop evidence unavailable" in dimensions["outcome"]["reason"]
    assert dimensions["outcome"]["objective_success"] is True
    assert dimensions["outcome"]["clean_stop"] is None
    assert dimensions["comparison"]["countable"] is False


def test_gate1p_stage_a_objective_is_child_control_not_system():
    from scenarios import goad_scenarios  # noqa: E402

    scn = next(s for s in goad_scenarios("Operation_GOAD") if s.name == "child-domain-control")
    objective = scn.objective.casefold()

    assert "administrative control of north.sevenkingdoms.local" in objective
    assert "system" not in objective
    assert scn.milestone_subset == (Milestone.OBJECTIVE,)
    assert "system:" not in rgl._objective_effect_prefixes(scn)


def test_gate1p_stage_a_objective_completes_at_child_control_with_further_hops_available():
    from scenarios import goad_scenarios  # noqa: E402
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
    import engagement_state as es  # noqa: E402

    scn = next(s for s in goad_scenarios("Operation_GOAD") if s.name == "child-domain-control")
    state = es.EngagementState(objective=scn.objective)

    assert es._objective_target_domains(scn.objective) == {rgl.CHILD}
    assert es._objective_is_complete(state, has_next=True) is False

    state.footholds = [
        es.Foothold(
            callback_id="2",
            agent="apollo",
            host="CASTELBLACK",
            forest=rgl.CHILD,
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="mythic",
            timestamp="2026-08-27T00:00:00Z",
        )
    ]
    state.hops = [
        es.Hop(
            id="gpo:child-control",
            technique="gpo-controlled-system-exec",
            target="starkwallpaper",
            effect=f"da:{rgl.CHILD}",
            status="achieved",
            evidence={"task_id": "8"},
            preconditions=[],
            satisfied_effects=[f"da:{rgl.CHILD}"],
            source="mythic",
            timestamp="2026-08-27T00:00:01Z",
        )
    ]

    assert es._objective_is_complete(state, has_next=True) is True


def test_build_probes_includes_objective_only_when_scored():
    reader = lambda _d: set()
    scn = _Scn((Milestone.OBJECTIVE, Milestone.KRBTGT_DUMPED))
    probes = rgl.build_probes(reader, {}, scn)
    assert Milestone.OBJECTIVE in probes
    assert Milestone.DA_CHILD not in probes


def test_objective_probe_accepts_certificate_admin_control_without_ldap_wait(monkeypatch):
    monkeypatch.setattr(
        rgl.live_seams,
        "certificate_admin_control_probe",
        lambda *args, **kwargs: (lambda: True),
    )

    def unexpected_reader(_domain):
        raise AssertionError("LDAP membership path should short-circuit after certificate proof")

    scn = _Scn((Milestone.OBJECTIVE,))
    probes = rgl.build_probes(unexpected_reader, {}, scn, settle_timeout=300)

    assert probes[Milestone.OBJECTIVE]() is True


def test_objective_probe_accepts_ticket_admin_control_without_ldap_wait(monkeypatch):
    monkeypatch.setattr(
        rgl.live_seams,
        "certificate_admin_control_probe",
        lambda *args, **kwargs: (lambda: False),
    )
    monkeypatch.setattr(
        rgl.live_seams,
        "ticket_admin_control_probe",
        lambda *args, **kwargs: (lambda: True),
    )

    def unexpected_reader(_domain):
        raise AssertionError("LDAP membership path should short-circuit after ticket proof")

    scn = _Scn((Milestone.OBJECTIVE,))
    probes = rgl.build_probes(unexpected_reader, {}, scn, settle_timeout=300)

    assert probes[Milestone.OBJECTIVE]() is True


def test_build_probes_da_reads_via_reader_not_callback():
    """The DA probe must read membership via the injected out-of-band reader — proving ground truth is
    decoupled from the agent callback. An escalated reader set (vs baseline) -> probe True."""
    reader = lambda _d: {"administrator", "intruder"}
    scn = _Scn((Milestone.DA_CHILD,))
    probes = rgl.build_probes(reader, {rgl.CHILD: {"administrator"}}, scn)
    assert probes[Milestone.DA_CHILD]() is True


def test_remote_exec_objective_uses_task_probe_without_referee_ldap(monkeypatch):
    from laps_family_transfer_holdout import LAPS_FAMILY_TRANSFER_HOLDOUT  # noqa: E402
    from scenarios import laps_family_transfer_holdout_scenarios  # noqa: E402

    calls = {}

    def remote_probe(target_host, *, realm, **_kwargs):
        calls["target_host"] = target_host
        calls["realm"] = realm
        return lambda: True

    monkeypatch.setattr(rgl.live_seams, "remote_execution_probe", remote_probe)

    def unexpected_admin_probe(*_args, **_kwargs):
        raise AssertionError("remote-exec objective must not build an admin-control probe")

    monkeypatch.setattr(rgl.live_seams, "certificate_admin_control_probe", unexpected_admin_probe)
    monkeypatch.setattr(rgl.live_seams, "ticket_admin_control_probe", unexpected_admin_probe)

    scn = laps_family_transfer_holdout_scenarios("Operation_SAGE_LAPS_HOLDOUT")[0]
    probes = rgl.build_probes(lambda _domain: set(), {}, scn)
    first_variant = LAPS_FAMILY_TRANSFER_HOLDOUT.objective_variants[0]

    assert probes[Milestone.OBJECTIVE]() is True
    assert calls == {
        "target_host": first_variant.target_host.casefold(),
        "realm": first_variant.target_domain.casefold(),
    }
    assert rgl._scored_referee_domains(scn) == set()


# --- preflight: cheap pre-run guard on objective completion-recognizability (the seam that regressed) ---

def test_objective_recognizable_rejects_empty_and_opaque():
    ok, why = rgl.objective_recognizable("")
    assert ok is False and "opaque" in why
    ok, why = rgl.objective_recognizable("sage-engagement:ae56d02d-cb33")
    assert ok is False and "opaque" in why


def test_objective_recognizable_accepts_da_phrase_with_target():
    ok, why = rgl.objective_recognizable(
        "escalate to Domain Admin of north.sevenkingdoms.local and DCSync its krbtgt account")
    assert ok is True
    assert "north.sevenkingdoms.local" in why


def test_objective_recognizable_accepts_non_da_objective_via_fallback():
    # "SYSTEM on a host" has no DA-phrase target but is non-opaque -> recognizable via the fallback path,
    # NOT a hard failure (gating on target-parse would wrongly abort this valid scenario).
    ok, why = rgl.objective_recognizable(
        "From the CASTELBLACK foothold, obtain SYSTEM-level code execution on a Windows host in north.sevenkingdoms.local.")
    assert ok is True
    assert "fallback" in why


def test_objective_recognizable_accepts_remote_exec_target():
    ok, why = rgl.objective_recognizable(
        "From the current foothold, prove bounded remote execution on CEDAR-OPS01."
    )
    assert ok is True
    assert "remote-exec target-matched" in why
    assert "cedar-ops01" in why


def test_all_goad_scenarios_pass_preflight():
    from scenarios import goad_scenarios  # noqa: E402
    for scn in goad_scenarios("Operation_GOAD"):
        ok, why = rgl.objective_recognizable(scn.objective)
        assert ok is True, f"scenario {scn.name} failed preflight: {why}"


def _uncovered_probeable_milestones(scn) -> list:
    """Scored probe-able milestones with NONE of the three accepted declarations.

    Anti-decay coverage: a scored probe-able milestone must be backed by a construction-time
    `direct_probes` verifier, a run-time `recorded_probe_milestones` declaration (captured live, replayed
    from the ledger), OR an explicit `self_report_exempt` (genuinely unverifiable). Anything else has
    silently decayed back to unguarded Sage self-report — the exact regression this guards against.
    """
    spec = scn.spec()
    scored = scn.milestone_subset or tuple(
        m for m in Milestone if m == Milestone.FOOTHOLD or m in spec
    )
    covered = set(scn.direct_probes) | set(scn.recorded_probe_milestones) | set(scn.self_report_exempt)
    return [m for m in scored if m in PROBEABLE_MILESTONES and m not in covered]


def test_all_goad_scenarios_declare_probeable_milestone_verifiers():
    from scenarios import goad_scenarios  # noqa: E402
    for scn in goad_scenarios("Operation_GOAD"):
        uncovered = _uncovered_probeable_milestones(scn)
        assert not uncovered, (
            f"scenario {scn.name} scores probeable milestone(s) {[m.name for m in uncovered]} "
            "without a direct_probes verifier, recorded_probe_milestones, or self_report_exempt declaration")


def test_anti_decay_check_fails_on_undeclared_probeable_milestone():
    # NEGATIVE control: a scenario that scores a probe-able milestone with NONE of the three declarations
    # MUST be flagged. Without this, the coverage test above could silently pass on any input and the
    # anti-decay guarantee would be vacuous.
    from range_state import Scenario  # noqa: E402
    undeclared = Scenario(
        name="decayed",
        engagement_id="Operation_Decay",
        domains={"child": rgl.CHILD},
        milestone_subset=(Milestone.FOOTHOLD, Milestone.DA_CHILD),  # DA_CHILD is probe-able, declared nowhere
    )
    assert Milestone.DA_CHILD in _uncovered_probeable_milestones(undeclared)
    # And the same milestone is covered once declared via recorded_probe_milestones (the Option-A path).
    declared = Scenario(
        name="recovered",
        engagement_id="Operation_Recover",
        domains={"child": rgl.CHILD},
        milestone_subset=(Milestone.FOOTHOLD, Milestone.DA_CHILD),
        recorded_probe_milestones=frozenset({Milestone.DA_CHILD}),
    )
    assert _uncovered_probeable_milestones(declared) == []


def test_cross_forest_objective_is_target_matched_after_rewording():
    # The cross-forest objective must parse essos.local so completion-recognition uses the STRONG
    # target-matched path (not the weaker no-next-hop fallback) — guards the reworded phrasing.
    from scenarios import goad_scenarios  # noqa: E402
    scn = next(s for s in goad_scenarios("Operation_GOAD") if s.name == "cross-forest-objective")
    ok, why = rgl.objective_recognizable(scn.objective)
    assert ok is True
    assert "essos.local" in why and "target-matched" in why


# --- manifest-driven preflight: derived from declared preconditions, not a hand-maintained allowlist ---

def test_referee_creds_present_offline_check(tmp_path, monkeypatch):
    import json
    cfgp = tmp_path / "ref.json"
    cfgp.write_text(json.dumps({
        "north.sevenkingdoms.local": {"dc_ip": "10.4.10.10", "user": "ref", "password": "set"},
        "essos.local": {"dc_ip": "10.4.10.12", "user": "ref", "password": ""},  # blank -> missing
    }))
    monkeypatch.setenv("SAGE_REFEREE_LDAP_CONFIG", str(cfgp))
    ok, _ = rgl._referee_creds_present("north.sevenkingdoms.local")
    assert ok is True
    ok, why = rgl._referee_creds_present("essos.local")
    assert ok is False and "password" in why


def test_preflight_manifest_covers_every_scored_referee_domain():
    # ANTI-DECAY: any scenario that SCORES an AD-direct domain must DECLARE a referee-creds precondition for
    # it, so a future scenario can't reintroduce the "cred checked only at post-reset point-of-use" bug. This
    # fails at CI, not at a lab reset.
    from scenarios import goad_scenarios  # noqa: E402
    cfg = rgl.Config()
    for scn in goad_scenarios("Operation_GOAD"):
        names = {n for (n, _cost, _c) in rgl.scenario_preconditions(cfg, scn.name)}
        for d in rgl._scored_referee_domains(scn):
            assert f"referee-creds:{d}" in names, (
                f"scenario {scn.name} scores {d} but declares no referee-creds precondition "
                f"-> it would be validated only post-reset (the bug this guards)")


def test_preflight_manifest_offline_checks_are_callable():
    # Every declared offline check must be invocable and return (bool, str) — no half-declared preconditions.
    # 'smoke'-tier checks (live Mythic/BloodHound validity) are NOT called here — that would do real network
    # I/O; they are covered hermetically by test_preflight_smoke_checks_declared_and_fail_closed.
    cfg = rgl.Config()
    for name, cost, check in rgl.scenario_preconditions(cfg, "cross-forest-objective"):
        assert cost in ("offline", "smoke", "live")
        if cost == "offline":
            ok, detail = check()
            assert isinstance(ok, bool) and isinstance(detail, str)


def test_preflight_smoke_checks_declared_and_fail_closed(monkeypatch):
    # The 'smoke' tier validates the EXACT probe queries against the live schema. It must (1) be declared for
    # cross-forest (mythic + bloodhound), and (2) FAIL CLOSED when a service/query errors — the opposite of
    # the fail-open probes — so a GraphQL field typo fails preflight, not after a 2h solve.
    import live_seams as ls
    names = {n for (n, cost, _c) in rgl.scenario_preconditions(rgl.Config(), "cross-forest-objective")
             if cost == "smoke"}
    assert "mythic-queries-valid" in names and "bloodhound-reachable" in names
    # fail-closed: a raising I/O boundary -> (False, str), never a swallow-to-True
    def boom(*a, **k):
        raise RuntimeError("schema/field error")
    monkeypatch.setattr(ls, "_mythic_login_async_safe", boom)
    monkeypatch.setattr(ls, "bloodhound_domain_count", boom)
    ok_m, detail_m = ls.mythic_queries_valid(timeout=1)
    ok_b, detail_b = ls.bloodhound_reachable(timeout=1)
    assert ok_m is False and isinstance(detail_m, str)
    assert ok_b is False and isinstance(detail_b, str)


def test_preflight_declares_exact_model_route_authentication_smoke(monkeypatch):
    seen = {}

    def auth_smoke(provider, model, **kwargs):
        seen.update({"provider": provider, "model": model, **kwargs})
        return True, "authenticated"

    monkeypatch.setattr(rgl.live_seams, "model_route_auth_smoke", auth_smoke)
    cfg = rgl.Config(
        model_provider="openai",
        model_id="exact-model",
        model_api_endpoint="https://provider.invalid/v1",
        model_api_key="secret",
        max_output_tokens=8192,
    )
    checks = {name: check for name, cost, check in rgl.scenario_preconditions(cfg, "child-domain-control")
              if cost == "smoke"}

    assert "llm-endpoint-authenticated" in checks
    assert checks["llm-endpoint-authenticated"]() == (True, "authenticated")
    assert seen == {
        "provider": "openai",
        "model": "exact-model",
        "api_key": "secret",
        "base_url": "https://provider.invalid/v1",
        "max_output_tokens": 8192,
    }


def test_preflight_model_route_authentication_fails_closed_when_route_incomplete(monkeypatch):
    monkeypatch.setattr(rgl.live_seams, "load_sage_defaults", lambda: {})
    cfg = rgl.Config(model_provider="openai", model_id="exact-model")
    checks = {name: check for name, cost, check in rgl.scenario_preconditions(cfg, "child-domain-control")
              if cost == "smoke"}

    ok, detail = checks["llm-endpoint-authenticated"]()
    assert ok is False
    assert "incomplete" in detail
