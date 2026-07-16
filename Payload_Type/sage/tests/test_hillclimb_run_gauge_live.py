"""Offline tests for the live driver's probe assembly: ground truth is scoped to the scenario's
milestones and read via the OUT-OF-BAND reader (never the agent callback). The live solve/baseline are
validated on the range; this pins the wiring that prevents the pollution + wrong-domain bugs.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import run_gauge_live as rgl  # noqa: E402
from range_state import Milestone, PROBEABLE_MILESTONES  # noqa: E402


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


class _Scn:
    def __init__(self, subset):
        self.milestone_subset = subset


def test_config_results_path_honors_eval_override(tmp_path, monkeypatch):
    path = tmp_path / "purpose-range-clean.jsonl"
    monkeypatch.setenv("SAGE_EVAL_RESULTS_PATH", str(path))

    assert rgl.Config().results_path == path


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
