"""Exercise append-only recovery and service limits through the shared consumer."""
import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

from ai.resource_budget import ResourceBudgetError, ResourceBudgetExceeded, ResourceLedger, configured_ledger


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path):
    config, ledger = tmp_path / "budget.json", tmp_path / "ledger.jsonl"
    resources = {name: {"limit": limit, "cleanup_reserve": cleanup} for name, limit, cleanup in (
        ("capture_correction_cycles", 2, 0), ("control_requests", 10000, 2000),
        ("provider_requests", 100, 0), ("provider_tokens", 1000000, 0),
        ("payload_task_attempts", 100, 0), ("episodes", 100, 0),
        ("burned_setup_retries", 6, 0))}
    config.write_text(json.dumps({"schema": "sage-shared-resource-budget-v1", "resources": resources,
                                 "calendar_seconds": 10000, "cleanup_seconds": 1000}))
    clock = [100.0]
    budget = ResourceLedger(config, ledger, now=lambda: clock[0])
    budget.reserve({"capture_correction_cycles": 2}, "old-corrections")
    request = budget.reserve({"provider_requests": 1, "provider_tokens": 100}, "old-provider")
    budget.settle_provider(request, 74)
    document, parent = tmp_path / "approved.md", tmp_path / "parent.json"
    document.write_text("Prospective recovery with unchanged aggregate limits.")
    parent.write_text(json.dumps({"status": "active", "revoked_at": None}))
    approval = tmp_path / "approval.json"
    data = {"schema": "sage-resource-recovery-amendment-v1", "status": "approved",
        "user_approval": "Approve this bounded recovery", "amendment_id": "recovery-v2",
        "base_config_sha256": _sha(config), "ledger_prefix_sha256": _sha(ledger),
        "ledger_prefix_records": 4, "next_sequence": 4, "original_aggregate_limits_unchanged": True,
        "document": {"path": str(document), "sha256": _sha(document)},
        "parent_authority": {"path": str(parent), "sha256": _sha(parent)},
        "limit_changes": {"capture_correction_cycles": {"old": 2, "new": 4}},
        "service_probe": {"max_starts": 1, "active_seconds": 900, "total_seconds": 1200,
            "control_requests": 1024, "active_control_requests": 896, "cleanup_control_requests": 128,
            "allowed_resources": ["control_requests"], "provider_authorized": False,
            "payload_tasks_authorized": False, "range_mutations_authorized": False}}
    approval.write_text(json.dumps(data))
    return budget, config, ledger, approval, clock


def _replacement(tmp_path, *, closed=True):
    budget, config, ledger, approval, clock = _fixture(tmp_path)
    budget.apply_amendment(approval, _sha(approval))
    budget.reserve({"capture_correction_cycles": 1}, "third-correction")
    budget.start_service_probe("service-first", "recovery-v2")
    scoped = ResourceLedger(config, ledger, now=lambda: clock[0], service_probe_id="service-first")
    scoped.reserve({"control_requests": 59}, "first-probe")
    if closed: budget.close_service_probe("service-first")
    value = json.loads(approval.read_text())
    value.update(schema="sage-resource-recovery-amendment-v2", amendment_id="recovery-v3",
        amendment_ordinal=2, replacement_service_probe=True, engineering_seconds=7200,
        correction_engineering_seconds=1500,
        prior_amendment={"path": str(approval), "sha256": _sha(approval)},
        ledger_prefix_records=len(ledger.read_text().splitlines()),
        next_sequence=len(ledger.read_text().splitlines()), ledger_prefix_sha256=_sha(ledger),
        limit_changes={"capture_correction_cycles": {"old": 4, "new": 5}})
    replacement = tmp_path / "replacement.json"; replacement.write_text(json.dumps(value))
    return budget, config, ledger, approval, replacement, clock


def _diagnostic(tmp_path):
    budget, config, ledger, _approval, replacement, clock = _replacement(tmp_path)
    budget.apply_amendment(replacement, _sha(replacement))
    budget.start_service_probe("second-service", "recovery-v3")
    budget.close_service_probe("second-service")
    value = json.loads(replacement.read_text())
    value.update(schema="sage-resource-recovery-amendment-v3", amendment_id="diagnostic-v4",
        amendment_ordinal=3, replacement_service_probe=False,
        prior_amendment={"path": str(replacement), "sha256": _sha(replacement)},
        ledger_prefix_records=len(ledger.read_text().splitlines()), next_sequence=len(ledger.read_text().splitlines()),
        ledger_prefix_sha256=_sha(ledger), limit_changes={"capture_correction_cycles": {"old": 5, "new": 6}},
        diagnostic_session={"stages": ["A", "B", "C"], "starts_per_stage": 2,
            "max_stage_starts": 6, "stage_active_seconds": 120, "stage_total_seconds": 180})
    value["service_probe"].update(active_seconds=2580, total_seconds=2700)
    approval = tmp_path / "diagnostic.json"; approval.write_text(json.dumps(value))
    budget.apply_amendment(approval, _sha(approval))
    budget.start_service_probe("diagnostic-session", "diagnostic-v4")
    scoped = ResourceLedger(config, ledger, now=lambda: clock[0], service_probe_id="diagnostic-session")
    return budget, scoped, approval, clock


def _close_diagnostic(budget, tmp_path, stage, *, passed=True, contained=True, suffix=""):
    result = tmp_path / f"{stage}{suffix}.json"
    result.write_text(json.dumps({"stage": stage, "passed": passed, "contained": contained}))
    return budget.close_diagnostic_stage("diagnostic-session", passed, contained, str(result), _sha(result))


def _continuation(tmp_path):
    budget, _scoped, previous, clock = _diagnostic(tmp_path)
    budget.close_service_probe("diagnostic-session")
    budget.reserve({"capture_correction_cycles": 2}, "remaining-historical-corrections")
    value = json.loads(previous.read_text())
    engineering = tmp_path / "engineering.jsonl"
    engineering.write_text(json.dumps({"event": "start", "interval_id": "work", "epoch": clock[0]}) + "\n")
    value.update(schema="sage-resource-recovery-amendment-v4", amendment_id="continuation-v5",
        amendment_ordinal=4, engineering_seconds=43200, engineering_cleanup_seconds=900,
        engineering_previously_used_seconds=6911.85, engineering_started_epoch=clock[0],
        engineering_intervals_path=str(engineering), engineering_prefix_records=0,
        engineering_prefix_sha256=hashlib.sha256(b"").hexdigest(), continuation_cycle=6,
        prior_amendment={"path": str(previous), "sha256": _sha(previous)},
        ledger_prefix_records=len(budget.ledger_path.read_text().splitlines()),
        next_sequence=len(budget.ledger_path.read_text().splitlines()), ledger_prefix_sha256=_sha(budget.ledger_path),
        limit_changes={"capture_correction_cycles": {"old": 6, "new": 6}},
        diagnostic_session={"stages": ["A", "B", "C"], "max_stage_starts": 24,
            "pooled_active_seconds": 7200, "stage_active_seconds": 600, "stage_cleanup_seconds": 120,
            "stage_total_seconds": 720, "calendar_deadlines_inherited": True},
        capture_recovery={"max_attempts": 6, "max_before_first_composite": 4,
            "consume_episode_and_setup_retry": True, "interpretable_outcome_replacement": False})
    value["service_probe"].update(max_starts=24, active_seconds=9000, total_seconds=10000,
        control_requests=8192, active_control_requests=7168, cleanup_control_requests=1024)
    approval = tmp_path / "continuation.json"; approval.write_text(json.dumps(value))
    prefix = budget.ledger_path.read_bytes(); before = budget.status()
    budget.apply_amendment(approval, _sha(approval))
    assert budget.ledger_path.read_bytes().startswith(prefix)
    assert budget.status()["effective_resource_charges"] == {**before["effective_resource_charges"], "capture_correction_cycles": 6}
    assert budget.status()["started_epoch"] == before["started_epoch"]
    return budget, approval, clock


def _continuation_close(budget, tmp_path, probe, stage, *, passed=True, contained=True,
                        extra=None, after_end=None):
    budget.end_diagnostic_active(probe)
    if after_end is not None:
        after_end()
    result = tmp_path / f"result-{budget.status()['next_sequence']}.json"
    result.write_text(json.dumps({"stage": stage, "passed": passed, "contained": contained, **(extra or {})}))
    return budget.close_diagnostic_stage(probe, passed, contained, str(result), _sha(result))


def _containment(tmp_path, expected_stage, epoch, **extra):
    path = tmp_path / "containment.json"
    path.write_text(json.dumps({"stage": expected_stage, "contained_epoch": epoch,
        "owned_process_dead": True, **extra}))
    return {"path": str(path), "sha256": _sha(path)}


def test_timed_containment_addendum_allows_late_admission_and_retry(tmp_path):
    budget, _approval, clock = _continuation(tmp_path)
    budget.start_service_probe("timely", "continuation-v5")
    stage = budget.start_diagnostic_stage("timely", "A")
    clock[0] += 1
    contained_epoch = clock[0]; addendum = _containment(tmp_path, "A", contained_epoch)
    closed = _continuation_close(budget, tmp_path, "timely", "A", passed=False,
        extra={"containment_epoch": contained_epoch, "containment_addendum": addendum},
        after_end=lambda: clock.__setitem__(0, stage["total_deadline_epoch"] + 10))
    replayed = budget.status()["service_probes"]["timely"]["stages"][-1]
    assert replayed["closed_epoch"] == clock[0] and replayed["containment_epoch"] == contained_epoch
    budget.start_diagnostic_stage("timely", "A", _continuation_retry(tmp_path, closed, "timely"))


def test_timed_containment_addendum_allows_late_passed_prerequisite(tmp_path):
    budget, _approval, clock = _continuation(tmp_path)
    budget.start_service_probe("current", "continuation-v5")
    first = budget.start_diagnostic_stage("current", "A")
    clock[0] += 1
    contained_epoch = clock[0]; addendum = _containment(tmp_path, "A", contained_epoch)
    _continuation_close(budget, tmp_path, "current", "A",
        extra={"containment_epoch": contained_epoch, "containment_addendum": addendum},
        after_end=lambda: clock.__setitem__(0, first["total_deadline_epoch"] + 10))
    second = budget.start_diagnostic_stage("current", "B")
    assert second["started_epoch"] > first["total_deadline_epoch"]


def test_direct_runner_contained_epoch_alias_is_replayed(tmp_path):
    budget, _approval, clock = _continuation(tmp_path)
    budget.start_service_probe("direct", "continuation-v5")
    budget.start_diagnostic_stage("direct", "A"); clock[0] += 1
    closed = _continuation_close(budget, tmp_path, "direct", "A", passed=False,
        extra={"contained_epoch": clock[0]})
    assert closed["containment_epoch"] == clock[0]


@pytest.mark.parametrize("case", ["missing", "negative", "nonfinite", "mismatched", "not-dead", "wrong-stage"])
def test_containment_evidence_fails_closed(tmp_path, case):
    fixture = tmp_path / case; fixture.mkdir(); budget, _approval, clock = _continuation(fixture)
    budget.start_service_probe("bad", "continuation-v5"); budget.start_diagnostic_stage("bad", "A")
    clock[0] += 1
    epoch = -1 if case == "negative" else float("nan") if case == "nonfinite" else clock[0]
    addendum = _containment(fixture, "A", clock[0],
        **({"contained_epoch": clock[0] + 1} if case == "mismatched" else {}),
        **({"owned_process_dead": False} if case == "not-dead" else {}),
        **({"stage": "B"} if case == "wrong-stage" else {}))
    extra = {"containment_addendum": addendum}
    if case != "missing": extra["containment_epoch"] = epoch
    with pytest.raises(ResourceBudgetError, match="containment evidence"):
        _continuation_close(budget, fixture, "bad", "A", passed=False, extra=extra)


def test_late_containment_and_drifted_addendum_block_replay(tmp_path):
    budget, _approval, clock = _continuation(tmp_path)
    budget.start_service_probe("late", "continuation-v5"); stage = budget.start_diagnostic_stage("late", "A")
    clock[0] = stage["total_deadline_epoch"] + 1
    addendum = _containment(tmp_path, "A", clock[0])
    closed = _continuation_close(budget, tmp_path, "late", "A", passed=False,
        extra={"containment_epoch": clock[0], "containment_addendum": addendum})
    with pytest.raises(ResourceBudgetError):
        budget.start_diagnostic_stage("late", "A", _continuation_retry(tmp_path, closed, "late"))
    Path(addendum["path"]).write_text(json.dumps({"stage": "A", "contained_epoch": clock[0],
                                                   "owned_process_dead": True, "changed": True}))
    with pytest.raises(ResourceBudgetError): budget.status()


def _continuation_retry(tmp_path, closed, nonce):
    probe = tmp_path / f"discriminator-{nonce}.json"; probe.write_text(json.dumps({"observed": nonce}))
    evidence = tmp_path / f"retry-{nonce}.json"
    evidence.write_text(json.dumps({"stage": closed["stage"], "prior_result": closed["evidence"],
        "cleanup_complete": True, "focused_probe_passed": True, "cause": f"supported change {nonce}",
        "source_identity": "a" * 64, "discriminating_probe": {"path": str(probe), "sha256": _sha(probe)}}))
    return {"path": str(evidence), "sha256": _sha(evidence)}


def _episode(budget, episode_id, *, activated=False, closed=False):
    budget.register_episode(episode_id, action_seconds=60, settlement_seconds=10,
        provider_token_limit=100, per_call_token_reservation=10, payload_task_limit=5)
    if activated: budget.activate_episode(episode_id)
    if closed: budget.close_episode(episode_id)


def _capture_disposition(tmp_path, episode_id, *, interpretable=False, missing=False):
    source = tmp_path / f"source-{episode_id}.json"
    if missing: source.unlink(missing_ok=True)
    else: source.write_text(json.dumps({"source": episode_id}))
    disposition = tmp_path / f"disposition-{episode_id}.json"
    disposition.write_text(json.dumps({"episode_id": episode_id, "protocol_valid": False,
        "interpretable": interpretable, "contained": True,
        "source_evidence": [{"path": str(source), "sha256": _sha(source) if source.exists() else "0" * 64}]}))
    return disposition


def _capture_result(tmp_path, episode_id, *, fixture=False, arm="shipping_sage"):
    from tests.test_capture_composite_bridge import _inputs, _load
    compiler, outer = _inputs(); packet = compiler["redacted_episode_packet"]
    packet["episode_id"] = episode_id
    packet["scorecard"]["measurement_identity"]["policy_arm"] = arm
    packet["requested_identity"].update(resource_episode_id=episode_id,
        measurement_arm=arm)
    output = tmp_path / f"composite-output-{episode_id}"; output.mkdir()
    result_value = _load().compose_capture(compiler, outer, output_dir=output)
    result_value["fixture_only"] = False
    result = tmp_path / f"composite-{episode_id}.json"
    result.write_text(json.dumps(result_value, sort_keys=True, separators=(",", ":")))
    evidence = tmp_path / f"first-{episode_id}.json"
    episode_source = output / "episode.json"
    evidence.write_text(json.dumps({"schema": "sage-m3-first-capture-composite-v1",
        "episode_id": episode_id, "fixture_only": fixture,
        "episode_source": {"path": str(episode_source), "sha256": _sha(episode_source)},
        "bridge_result": {"path": str(result), "sha256": _sha(result)}}))
    return evidence


def _reserve_replacement(budget, tmp_path, episode_id, source_id):
    disposition = _capture_disposition(tmp_path, source_id)
    budget.reserve_capture_recovery(episode_id, prior_disposition_path=disposition,
        prior_disposition_sha256=_sha(disposition))
    return disposition


def _capture_worker(config, ledger, episode_id, disposition, digest, now, queue):
    try:
        ResourceLedger(config, ledger, now=lambda: now).reserve_capture_recovery(episode_id,
            prior_disposition_path=disposition, prior_disposition_sha256=digest)
        queue.put("reserved")
    except ResourceBudgetError:
        queue.put("rejected")


def test_capture_recovery_pool_latches_first_composite_and_stops_at_six(tmp_path):
    budget, _approval, _clock = _continuation(tmp_path)
    _episode(budget, "historical", activated=True, closed=True)
    source = "historical"
    for number in range(1, 5):
        episode = f"replacement-{number}"; _episode(budget, episode)
        _reserve_replacement(budget, tmp_path, episode, source); budget.close_episode(episode); source = episode
    blocked = "replacement-pre-latch"; _episode(budget, blocked)
    disposition = _capture_disposition(tmp_path, source)
    with pytest.raises(ResourceBudgetError, match="pool is exhausted"):
        budget.reserve_capture_recovery(blocked, prior_disposition_path=disposition,
            prior_disposition_sha256=_sha(disposition))
    budget.close_episode(blocked)
    _episode(budget, "observed", activated=True, closed=True)
    first = _capture_result(tmp_path, "observed")
    budget.record_first_capture_composite("observed", first, _sha(first))
    source = blocked
    for number in range(5, 7):
        episode = f"replacement-{number}"; _episode(budget, episode)
        _reserve_replacement(budget, tmp_path, episode, source); budget.close_episode(episode); source = episode
    final = "replacement-seven"; _episode(budget, final)
    disposition = _capture_disposition(tmp_path, source)
    with pytest.raises(ResourceBudgetError, match="pool is exhausted"):
        budget.reserve_capture_recovery(final, prior_disposition_path=disposition,
            prior_disposition_sha256=_sha(disposition))
    state = budget.status()["capture_recovery"]
    assert state == {"total_attempts": 6, "before_first_composite": 4,
        "first_composite_evidence": {"path": str(first), "sha256": _sha(first)}}


def test_capture_recovery_charges_immediately_without_activation_double_charge(tmp_path):
    budget, _approval, _clock = _continuation(tmp_path)
    _episode(budget, "prior", activated=True, closed=True); _episode(budget, "retry")
    before = budget.status()["effective_resource_charges"]
    _reserve_replacement(budget, tmp_path, "retry", "prior")
    reserved = budget.status()
    assert reserved["effective_resource_charges"] == {
        **before, "episodes": before["episodes"] + 1,
        "burned_setup_retries": before["burned_setup_retries"] + 1}
    budget.activate_episode("retry")
    assert budget.status()["effective_resource_charges"] == reserved["effective_resource_charges"]


def test_unactivated_capture_recovery_remains_charged(tmp_path):
    budget, _approval, _clock = _continuation(tmp_path)
    _episode(budget, "prior", closed=True); _episode(budget, "unused")
    _reserve_replacement(budget, tmp_path, "unused", "prior"); budget.close_episode("unused")
    assert budget.status()["episodes"]["unused"]["activated_epoch"] is None
    assert budget.status()["effective_resource_charges"]["burned_setup_retries"] == 1


def test_capture_recovery_rejects_duplicate_prior_and_interpretable_outcome(tmp_path):
    budget, _approval, _clock = _continuation(tmp_path)
    _episode(budget, "prior", closed=True); _episode(budget, "first")
    disposition = _reserve_replacement(budget, tmp_path, "first", "prior"); budget.close_episode("first")
    _episode(budget, "second")
    with pytest.raises(ResourceBudgetError):
        budget.reserve_capture_recovery("second", prior_disposition_path=disposition,
            prior_disposition_sha256=_sha(disposition))
    budget.close_episode("second"); _episode(budget, "interpretable", closed=True); _episode(budget, "third")
    disposition = _capture_disposition(tmp_path, "interpretable", interpretable=True)
    with pytest.raises(ResourceBudgetError):
        budget.reserve_capture_recovery("third", prior_disposition_path=disposition,
            prior_disposition_sha256=_sha(disposition))


def test_capture_recovery_rejects_active_service_and_missing_evidence(tmp_path):
    budget, _approval, _clock = _continuation(tmp_path)
    _episode(budget, "prior", closed=True); _episode(budget, "closed-target", closed=True)
    budget.start_service_probe("active-service", "continuation-v5")
    disposition = _capture_disposition(tmp_path, "prior")
    with pytest.raises(ResourceBudgetError):
        budget.reserve_capture_recovery("closed-target", prior_disposition_path=disposition,
            prior_disposition_sha256=_sha(disposition))
    budget.close_service_probe("active-service"); _episode(budget, "target")
    disposition = _capture_disposition(tmp_path, "prior", missing=True)
    with pytest.raises(ResourceBudgetError):
        budget.reserve_capture_recovery("target", prior_disposition_path=disposition,
            prior_disposition_sha256=_sha(disposition))


def test_capture_recovery_drift_and_fixture_latch_fail_closed(tmp_path):
    budget, _approval, _clock = _continuation(tmp_path)
    _episode(budget, "prior", closed=True); _episode(budget, "target")
    disposition = _reserve_replacement(budget, tmp_path, "target", "prior"); budget.activate_episode("target")
    budget.close_episode("target"); fixture = _capture_result(tmp_path, "target", fixture=True)
    with pytest.raises(ResourceBudgetError):
        budget.record_first_capture_composite("target", fixture, _sha(fixture))
    Path(json.loads(disposition.read_text())["source_evidence"][0]["path"]).write_text("drift")
    with pytest.raises(ResourceBudgetError, match="drifted"): budget.status()


def test_first_capture_composite_duplicate_malformed_and_drift_fail_closed(tmp_path):
    budget, _approval, _clock = _continuation(tmp_path)
    _episode(budget, "malformed", activated=True, closed=True)
    bad = tmp_path / "fabricated.json"
    bad.write_text(json.dumps({"episode_id": "malformed", "fixture_only": False,
        "composite_result": {"path": str(tmp_path / "missing"), "sha256": "0" * 64},
        "record": {"record": 1}, "row": {"row": 1}}))
    with pytest.raises(ResourceBudgetError):
        budget.record_first_capture_composite("malformed", bad, _sha(bad))

    _episode(budget, "accepted", activated=True, closed=True)
    good = _capture_result(tmp_path, "accepted")
    budget.record_first_capture_composite("accepted", good, _sha(good))
    with pytest.raises(ResourceBudgetError):
        budget.record_first_capture_composite("accepted", good, _sha(good))
    Path(json.loads(good.read_text())["bridge_result"]["path"]).write_text("drift")
    with pytest.raises(ResourceBudgetError, match="drifted"): budget.status()


@pytest.mark.parametrize("arm", ["baseline", "candidate-thin-fix"])
def test_first_capture_composite_accepts_requested_harness_measurement_arm(tmp_path, arm):
    budget, _approval, _clock = _continuation(tmp_path)
    _episode(budget, "observed", activated=True, closed=True)
    evidence = _capture_result(tmp_path, "observed", arm=arm)
    budget.record_first_capture_composite("observed", evidence, _sha(evidence))
    assert budget.status()["capture_recovery"]["first_composite_evidence"] == {
        "path": str(evidence), "sha256": _sha(evidence)}


@pytest.mark.parametrize("case", ["envelope-fixture", "bridge-fixture", "raw", "bare", "arm",
                                      "measurement-arm",
                                      "episode-id", "episode-hash", "result-hash", "mapping", "drift"])
def test_first_capture_composite_structural_near_matches_fail_closed(tmp_path, case):
    fixture = tmp_path / case; fixture.mkdir(); budget, _approval, _clock = _continuation(fixture)
    _episode(budget, "observed", activated=True, closed=True)
    envelope_path = _capture_result(fixture, "observed")
    envelope = json.loads(envelope_path.read_text())
    episode_path = Path(envelope["episode_source"]["path"])
    bridge_path = Path(envelope["bridge_result"]["path"])
    episode = json.loads(episode_path.read_text()); bridge = json.loads(bridge_path.read_text())
    if case == "envelope-fixture": envelope["fixture_only"] = True
    elif case == "bridge-fixture": bridge["fixture_only"] = True
    elif case == "raw": episode["raw_result"] = {"answer": "bare"}
    elif case == "bare": episode["requested_identity"]["side"] = "bare"
    elif case == "arm": episode["scorecard"]["measurement_identity"]["policy_arm"] = "true_naive"
    elif case == "measurement-arm": episode["requested_identity"]["measurement_arm"] = "candidate"
    elif case == "episode-id": episode["requested_identity"]["resource_episode_id"] = "other"
    elif case == "episode-hash": bridge["episode_sha256"] = "0" * 64
    elif case == "result-hash": bridge["admission"]["result_hash"] = "sha256:" + "0" * 64
    elif case == "mapping": bridge["admission"] = []
    elif case == "drift": episode_path.write_text("drift")
    if case != "drift":
        episode_path.write_text(json.dumps(episode, sort_keys=True, separators=(",", ":")))
        if case in {"raw", "bare", "arm", "measurement-arm", "episode-id"}:
            bridge["episode_sha256"] = _sha(episode_path)
        bridge_path.write_text(json.dumps(bridge, sort_keys=True, separators=(",", ":")))
        envelope["episode_source"]["sha256"] = _sha(episode_path)
        envelope["bridge_result"]["sha256"] = _sha(bridge_path)
        envelope_path.write_text(json.dumps(envelope))
    with pytest.raises(ResourceBudgetError):
        budget.record_first_capture_composite("observed", envelope_path, _sha(envelope_path))


def test_concurrent_fourth_capture_recovery_reserves_one_slot(tmp_path):
    budget, _approval, _clock = _continuation(tmp_path)
    _episode(budget, "prior", closed=True); source = "prior"
    for number in range(1, 4):
        episode = f"used-{number}"; _episode(budget, episode)
        _reserve_replacement(budget, tmp_path, episode, source); budget.close_episode(episode); source = episode
    _episode(budget, "last"); disposition = _capture_disposition(tmp_path, source)
    queue = multiprocessing.Queue()
    processes = [multiprocessing.Process(target=_capture_worker, args=(budget.config_path,
        budget.ledger_path, "last", disposition, _sha(disposition), budget.status()["started_epoch"], queue))
        for _ in range(2)]
    for process in processes: process.start()
    for process in processes: process.join(timeout=5)
    assert all(process.exitcode == 0 for process in processes)
    assert sorted(queue.get(timeout=1) for _ in processes) == ["rejected", "reserved"]
    assert budget.status()["capture_recovery"]["total_attempts"] == 4


def test_continuation_pools_starts_across_sessions_and_reestablishes_prerequisites(tmp_path):
    budget, approval, clock = _continuation(tmp_path)
    with pytest.raises(ResourceBudgetExceeded): budget.reserve({"capture_correction_cycles": 1}, "extra-cycle")
    prior_a = None
    for number in range(8):
        name = f"session-{number}"
        budget.start_service_probe(name, "continuation-v5")
        with pytest.raises(ResourceBudgetError): budget.start_diagnostic_stage(name, "C")
        closed = {}
        for stage in "ABC":
            old = prior_a if stage == "A" else previous.get(stage) if number else None
            retry = _continuation_retry(tmp_path, old, f"{number}-{stage}") if old else None
            state = budget.start_diagnostic_stage(name, stage, retry)
            assert state["active_deadline_epoch"] == clock[0] + 600
            clock[0] += 1
            closed[stage] = _continuation_close(budget, tmp_path, name, stage)
        prior_a = closed["A"]; previous = closed
        budget.close_service_probe(name)
    budget.start_service_probe("exhausted", "continuation-v5")
    with pytest.raises(ResourceBudgetError):
        budget.start_diagnostic_stage("exhausted", "A", _continuation_retry(tmp_path, prior_a, "extra"))


@pytest.mark.parametrize("last_duration", [599, 600])
def test_continuation_total_active_time_and_early_cleanup_deadlines(tmp_path, last_duration):
    budget, approval, clock = _continuation(tmp_path)
    budget.start_service_probe("pooled", "continuation-v5")
    prior = None
    for i in range(12):
        retry = _continuation_retry(tmp_path, prior, i) if prior else None
        state = budget.start_diagnostic_stage("pooled", "A", retry)
        clock[0] += last_duration if i == 11 else 600
        prior = _continuation_close(budget, tmp_path, "pooled", "A", passed=False)
    with pytest.raises(ResourceBudgetExceeded):
        budget.start_diagnostic_stage("pooled", "A", _continuation_retry(tmp_path, prior, 13))


def test_continuation_end_active_prevents_dispatch_and_bounds_cleanup(tmp_path):
    budget, approval, clock = _continuation(tmp_path)
    budget.start_service_probe("pooled", "continuation-v5")
    scoped = ResourceLedger(budget.config_path, budget.ledger_path, now=lambda: clock[0], service_probe_id="pooled")
    budget.start_diagnostic_stage("pooled", "A")
    with pytest.raises(ResourceBudgetError): budget.close_service_probe("pooled")
    with pytest.raises(ResourceBudgetExceeded):
        scoped.reserve({"control_requests": 1024}, "premature-cleanup", cleanup=True)
    clock[0] += 1
    ended = budget.end_diagnostic_active("pooled")
    assert ended["total_deadline_epoch"] == clock[0] + 120
    with pytest.raises(ResourceBudgetExceeded): scoped.reserve({"control_requests": 1}, "late-active")
    scoped.reserve({"control_requests": 1}, "cleanup", cleanup=True)
    clock[0] += 120
    with pytest.raises(ResourceBudgetExceeded): scoped.reserve({"control_requests": 1}, "late-cleanup", cleanup=True)


def test_continuation_controls_do_not_reset_between_sessions(tmp_path):
    budget, approval, clock = _continuation(tmp_path)
    budget.start_service_probe("first", "continuation-v5")
    budget.start_diagnostic_stage("first", "A")
    first = ResourceLedger(budget.config_path, budget.ledger_path, now=lambda: clock[0], service_probe_id="first")
    first.reserve({"control_requests": 7167}, "almost-all")
    closed = _continuation_close(budget, tmp_path, "first", "A", passed=False)
    budget.close_service_probe("first")
    budget.start_service_probe("second", "continuation-v5")
    budget.start_diagnostic_stage("second", "A", _continuation_retry(tmp_path, closed, "new"))
    second = ResourceLedger(budget.config_path, budget.ledger_path, now=lambda: clock[0], service_probe_id="second")
    second.reserve({"control_requests": 1}, "last")
    with pytest.raises(ResourceBudgetExceeded): second.reserve({"control_requests": 1}, "overflow")
    with pytest.raises(ResourceBudgetExceeded): second.reserve({"provider_requests": 1, "provider_tokens": 100}, "forbidden")


def test_continuation_engineering_reserve_stops_active_work(tmp_path):
    budget, approval, clock = _continuation(tmp_path)
    clock[0] += 43200 - 900 - 6911.85
    with pytest.raises(ResourceBudgetExceeded, match="engineering"):
        budget.start_service_probe("too-late", "continuation-v5")
    with pytest.raises(ResourceBudgetExceeded, match="engineering"):
        budget.reserve({"control_requests": 1}, "too-late")


def test_continuation_rejects_unchanged_retry_and_stale_prerequisite(tmp_path):
    budget, approval, clock = _continuation(tmp_path)
    budget.start_service_probe("pooled", "continuation-v5")
    budget.start_diagnostic_stage("pooled", "A")
    a = _continuation_close(budget, tmp_path, "pooled", "A")
    budget.start_diagnostic_stage("pooled", "B")
    b = _continuation_close(budget, tmp_path, "pooled", "B")
    retry = _continuation_retry(tmp_path, a, "change")
    clock[0] += 1
    budget.start_diagnostic_stage("pooled", "A", retry)
    a2 = _continuation_close(budget, tmp_path, "pooled", "A")
    with pytest.raises(ResourceBudgetError, match="prerequisites"):
        budget.start_diagnostic_stage("pooled", "C")
    data = json.loads(Path(retry["path"]).read_text()); data["prior_result"] = a2["evidence"]
    duplicate = tmp_path / "duplicate.json"; duplicate.write_text(json.dumps(data))
    with pytest.raises(ResourceBudgetError, match="unchanged"):
        budget.start_diagnostic_stage("pooled", "A", {"path": str(duplicate), "sha256": _sha(duplicate)})


def test_diagnostic_orders_real_scopes_and_preserves_one_shared_control_cap(tmp_path):
    budget, scoped, approval, clock = _diagnostic(tmp_path)
    assert budget.status()["effective_limits"]["capture_correction_cycles"] == 6
    with pytest.raises(ResourceBudgetExceeded): scoped.reserve({"control_requests": 1}, "before-start")
    with pytest.raises(ResourceBudgetError): budget.start_diagnostic_stage("diagnostic-session", "B")
    for stage in ("A", "B", "C"):
        state = budget.start_diagnostic_stage("diagnostic-session", stage)
        assert state["attempt"] == 1 and state["active_deadline_epoch"] == clock[0] + 120
        with pytest.raises(ResourceBudgetError): budget.start_diagnostic_stage("diagnostic-session", stage)
        scoped.reserve({"control_requests": 298}, f"stage-{stage.lower()}")
        with pytest.raises(ResourceBudgetExceeded):
            scoped.reserve({"provider_requests": 1, "provider_tokens": 10}, "forbidden")
        if stage == "C":
            with pytest.raises(ResourceBudgetExceeded): scoped.reserve({"control_requests": 3}, "shared-overflow")
        _close_diagnostic(budget, tmp_path, stage)
        with pytest.raises(ResourceBudgetError): budget.start_diagnostic_stage("diagnostic-session", stage)
    budget.close_service_probe("diagnostic-session")
    assert budget.status()["service_probes"]["diagnostic-session"]["active_control_requests"] == 894


def test_diagnostic_one_supported_retry_and_deadlines(tmp_path):
    budget, scoped, approval, clock = _diagnostic(tmp_path)
    budget.start_diagnostic_stage("diagnostic-session", "A")
    clock[0] += 120
    with pytest.raises(ResourceBudgetExceeded): scoped.reserve({"control_requests": 1}, "active-expired")
    scoped.reserve({"control_requests": 1}, "cleanup", cleanup=True)
    closed = _close_diagnostic(budget, tmp_path, "A", passed=False)
    with pytest.raises(ResourceBudgetError): budget.start_diagnostic_stage("diagnostic-session", "A")
    retry = tmp_path / "retry.json"
    retry.write_text(json.dumps({"stage": "A", "prior_result": closed["evidence"],
        "cleanup_complete": True, "focused_probe_passed": True, "cause": "fixture import failure",
        "source_identity": "a" * 64}))
    budget.start_diagnostic_stage("diagnostic-session", "A", {"path": str(retry), "sha256": _sha(retry)})
    _close_diagnostic(budget, tmp_path, "A", passed=False, suffix="-retry")
    with pytest.raises(ResourceBudgetError):
        budget.start_diagnostic_stage("diagnostic-session", "A", {"path": str(retry), "sha256": _sha(retry)})
    with pytest.raises(ResourceBudgetError): budget.start_diagnostic_stage("diagnostic-session", "B")


def test_diagnostic_failed_containment_and_forged_result_do_not_advance(tmp_path):
    budget, scoped, approval, clock = _diagnostic(tmp_path)
    budget.start_diagnostic_stage("diagnostic-session", "A")
    result = tmp_path / "forged.json"; result.write_text(json.dumps({"stage": "A", "passed": False, "contained": True}))
    with pytest.raises(ResourceBudgetError):
        budget.close_diagnostic_stage("diagnostic-session", True, True, str(result), _sha(result))
    _close_diagnostic(budget, tmp_path, "A", passed=False, contained=False)
    with pytest.raises(ResourceBudgetError): budget.start_diagnostic_stage("diagnostic-session", "B")


def test_diagnostic_late_containment_is_recorded_but_blocks_retry(tmp_path):
    budget, scoped, approval, clock = _diagnostic(tmp_path)
    budget.start_diagnostic_stage("diagnostic-session", "A")
    clock[0] += 181
    closed = _close_diagnostic(budget, tmp_path, "A", passed=False)
    retry = tmp_path / "late-retry.json"
    retry.write_text(json.dumps({"stage": "A", "prior_result": closed["evidence"],
        "cleanup_complete": True, "focused_probe_passed": True, "cause": "fixture import failure",
        "source_identity": "a" * 64}))
    with pytest.raises(ResourceBudgetError):
        budget.start_diagnostic_stage("diagnostic-session", "A", {"path": str(retry), "sha256": _sha(retry)})


def test_replacement_replays_old_charges_and_allows_one_scoped_probe(tmp_path):
    budget, config, ledger, approval, replacement, clock = _replacement(tmp_path)
    prefix = ledger.read_bytes(); old = budget.status()
    budget.apply_amendment(replacement, _sha(replacement))
    assert ledger.read_bytes().startswith(prefix)
    assert budget.status()["effective_resource_charges"] == old["effective_resource_charges"]
    assert budget.status()["effective_limits"]["capture_correction_cycles"] == 5
    budget.reserve({"capture_correction_cycles": 1}, "fourth-correction")
    budget.reserve({"capture_correction_cycles": 1}, "fifth-correction")
    with pytest.raises(ResourceBudgetExceeded): budget.reserve({"capture_correction_cycles": 1}, "sixth")
    with pytest.raises(ResourceBudgetError): budget.start_service_probe("old-retry", "recovery-v2")
    budget.start_service_probe("replacement-service", "recovery-v3")
    with pytest.raises(ResourceBudgetError): budget.start_service_probe("parallel", "recovery-v3")
    scoped = ResourceLedger(config, ledger, now=lambda: clock[0], service_probe_id="replacement-service")
    scoped.reserve({"control_requests": 896}, "active-full")
    with pytest.raises(ResourceBudgetExceeded): scoped.reserve({"control_requests": 1}, "overflow")
    with pytest.raises(ResourceBudgetExceeded): budget.reserve({"control_requests": 1}, "unscoped")
    with pytest.raises(ResourceBudgetExceeded):
        scoped.reserve({"provider_requests": 1, "provider_tokens": 100}, "forbidden")
    scoped.reserve({"control_requests": 128}, "cleanup-full", cleanup=True)
    budget.close_service_probe("replacement-service")
    with pytest.raises(ResourceBudgetError): budget.start_service_probe("third-service", "recovery-v3")
    assert budget.status()["effective_resource_charges"]["control_requests"] == 1083
    assert budget.status()["effective_resource_charges"]["provider_tokens"] == 74
    with pytest.raises(ResourceBudgetError): budget.apply_amendment(replacement, _sha(replacement))


@pytest.mark.parametrize("mutation", ["unclosed", "prior", "delta", "engineering", "seconds", "ordinal", "missing-prior"])
def test_replacement_rejects_wrong_lineage_broader_limits_and_open_scope(tmp_path, mutation):
    budget, config, ledger, approval, replacement, clock = _replacement(tmp_path, closed=mutation != "unclosed")
    value = json.loads(replacement.read_text())
    if mutation == "prior": value["prior_amendment"]["sha256"] = "0" * 64
    elif mutation == "delta": value["limit_changes"]["capture_correction_cycles"]["new"] = 6
    elif mutation == "engineering": value["engineering_seconds"] += 1
    elif mutation == "seconds": value["service_probe"]["active_seconds"] += 1
    elif mutation == "ordinal": value["amendment_ordinal"] = 3
    elif mutation == "missing-prior": value.pop("prior_amendment")
    replacement.write_text(json.dumps(value)); original = ledger.read_bytes()
    with pytest.raises(ResourceBudgetError): budget.apply_amendment(replacement, _sha(replacement))
    assert ledger.read_bytes() == original


def test_amendment_preserves_prefix_clock_charges_and_exact_other_caps(tmp_path):
    budget, config, ledger, approval, clock = _fixture(tmp_path)
    before = budget.status()
    original_config, original_prefix = config.read_bytes(), ledger.read_bytes()
    with pytest.raises(ResourceBudgetExceeded):
        budget.reserve({"capture_correction_cycles": 1}, "unamended")
    budget.apply_amendment(approval, _sha(approval))
    after = ResourceLedger(config, ledger).status()
    assert ledger.read_bytes().startswith(original_prefix) and config.read_bytes() == original_config
    for field in ("started_epoch", "active_deadline_epoch", "total_deadline_epoch", "effective_resource_charges"):
        assert after[field] == before[field]
    assert after["effective_limits"] == {**before["effective_limits"], "capture_correction_cycles": 4}
    budget.reserve({"capture_correction_cycles": 1}, "third-correction")
    budget.reserve({"capture_correction_cycles": 1}, "fourth-correction")
    with pytest.raises(ResourceBudgetExceeded):
        budget.reserve({"capture_correction_cycles": 1}, "fifth-correction")
    assert budget.status()["effective_resource_charges"]["provider_tokens"] == 74
    with pytest.raises(ResourceBudgetError, match="duplicate"):
        budget.apply_amendment(approval, _sha(approval))


@pytest.mark.parametrize("mutation", ["proposed", "wrong-prefix", "wrong-base", "wrong-sequence", "broader-limit",
                                      "extra-correction", "extra-control", "extra-second"])
def test_unapproved_or_misbound_amendment_cannot_append(tmp_path, mutation):
    budget, config, ledger, approval, clock = _fixture(tmp_path)
    value = json.loads(approval.read_text())
    if mutation == "proposed": value["status"] = "proposed"
    elif mutation == "wrong-prefix": value["ledger_prefix_sha256"] = "0" * 64
    elif mutation == "wrong-base": value["base_config_sha256"] = "0" * 64
    elif mutation == "wrong-sequence": value["next_sequence"] += 1
    elif mutation == "extra-correction": value["limit_changes"]["capture_correction_cycles"]["new"] = 5
    elif mutation == "extra-control":
        value["service_probe"]["active_control_requests"] += 1
        value["service_probe"]["control_requests"] += 1
    elif mutation == "extra-second": value["service_probe"]["active_seconds"] += 1
    else: value["limit_changes"]["provider_tokens"] = {"old": 1000000, "new": 2000000}
    approval.write_text(json.dumps(value))
    before = ledger.read_bytes()
    with pytest.raises(ResourceBudgetError): budget.apply_amendment(approval, _sha(approval))
    assert ledger.read_bytes() == before


def test_amendment_replay_rejects_reordering_historical_drift_and_approval_drift(tmp_path):
    budget, config, ledger, approval, clock = _fixture(tmp_path)
    budget.apply_amendment(approval, _sha(approval))
    original = ledger.read_bytes()
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    events[1]["operation"] = "historically-modified"
    ledger.write_text("".join(json.dumps(e) + "\n" for e in events))
    with pytest.raises(ResourceBudgetError, match="prefix"): budget.status()
    ledger.write_bytes(original)
    rows = ledger.read_text().splitlines(keepends=True)
    rows[3], rows[4] = rows[4], rows[3]
    ledger.write_text("".join(rows))
    with pytest.raises(ResourceBudgetError, match="sequence"): budget.status()
    ledger.write_bytes(original)
    approval.write_text(approval.read_text() + "\n")
    with pytest.raises(ResourceBudgetError, match="drifted"): budget.status()


def test_service_scope_blocks_nested_provider_tasks_unscoped_and_second_start(tmp_path, monkeypatch):
    budget, config, ledger, approval, clock = _fixture(tmp_path)
    budget.apply_amendment(approval, _sha(approval))
    info = budget.start_service_probe("service-one", "recovery-v2")
    assert info["active_deadline_epoch"] == 1000 and info["total_deadline_epoch"] == 1300
    for key, value in {"SAGE_RESOURCE_BUDGET_CONFIG": str(config), "SAGE_RESOURCE_BUDGET_LEDGER": str(ledger),
                       "SAGE_RESOURCE_SERVICE_PROBE_ID": "service-one"}.items(): monkeypatch.setenv(key, value)
    monkeypatch.delenv("SAGE_RESOURCE_EPISODE_ID", raising=False)
    monkeypatch.delenv("SAGE_RESOURCE_BUDGET_PHASE", raising=False)
    child = configured_ledger(now=lambda: clock[0])
    with pytest.raises(ResourceBudgetExceeded):
        child.reserve({"provider_requests": 1, "provider_tokens": 32768}, "forbidden-provider")
    with pytest.raises(ResourceBudgetExceeded): child.reserve({"payload_task_attempts": 1}, "forbidden-task")
    with pytest.raises(ResourceBudgetExceeded): budget.reserve({"control_requests": 1}, "missing-service-scope")
    child.reserve({"control_requests": 896}, "active-cap")
    with pytest.raises(ResourceBudgetExceeded): child.reserve({"control_requests": 1}, "active-over-cap")
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_PHASE", "cleanup")
    cleanup = configured_ledger(now=lambda: clock[0])
    cleanup.reserve({"control_requests": 128}, "cleanup-cap")
    with pytest.raises(ResourceBudgetExceeded): cleanup.reserve({"control_requests": 1}, "cleanup-over-cap")
    with pytest.raises(ResourceBudgetExceeded):
        budget.register_episode("cannot-overlap", action_seconds=10, settlement_seconds=5,
                                provider_token_limit=100, per_call_token_reservation=50, payload_task_limit=2)
    budget.close_service_probe("service-one")
    with pytest.raises(ResourceBudgetExceeded): child.reserve({"control_requests": 1}, "closed")
    with pytest.raises(ResourceBudgetError): budget.start_service_probe("service-two", "recovery-v2")
    status = budget.status()
    assert status["effective_resource_charges"]["control_requests"] == 1024
    assert status["effective_resource_charges"]["provider_requests"] == 1
    assert status["effective_resource_charges"]["payload_task_attempts"] == 0


def test_service_deadlines_are_enforced_at_shared_dispatch_boundary(tmp_path):
    budget, config, ledger, approval, clock = _fixture(tmp_path)
    budget.apply_amendment(approval, _sha(approval))
    budget.start_service_probe("probe", "recovery-v2")
    child = ResourceLedger(config, ledger, now=lambda: clock[0], service_probe_id="probe")
    clock[0] = 999.99
    child.reserve({"control_requests": 1}, "last-active")
    clock[0] = 1000
    with pytest.raises(ResourceBudgetExceeded): child.reserve({"control_requests": 1}, "late-active")
    child.reserve({"control_requests": 1}, "cleanup", cleanup=True)
    clock[0] = 1300
    with pytest.raises(ResourceBudgetExceeded): child.reserve({"control_requests": 1}, "late-cleanup", cleanup=True)


def _service_racer(config, ledger, gate, results):
    gate.wait()
    budget = ResourceLedger(config, ledger, now=lambda: 100, service_probe_id="probe")
    try:
        budget.reserve({"control_requests": 1}, "racing-child")
        results.put("reserved")
    except ResourceBudgetExceeded:
        results.put("denied")


def test_service_children_cannot_race_past_local_limit(tmp_path):
    budget, config, ledger, approval, clock = _fixture(tmp_path)
    budget.apply_amendment(approval, _sha(approval))
    budget.start_service_probe("probe", "recovery-v2")
    ResourceLedger(config, ledger, now=lambda: 100, service_probe_id="probe").reserve(
        {"control_requests": 895}, "all-but-one")
    context = multiprocessing.get_context("fork")
    gate, results = context.Event(), context.Queue()
    children = [context.Process(target=_service_racer, args=(config, ledger, gate, results)) for _ in range(6)]
    for child in children: child.start()
    gate.set()
    observed = [results.get(timeout=5) for child in children]
    for child in children:
        child.join(5)
        assert child.exitcode == 0
    assert observed.count("reserved") == 1 and observed.count("denied") == 5
    assert budget.status()["service_probes"]["probe"]["active_control_requests"] == 896
