"""Offline preparation must produce bindings accepted by the real pair producer."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/sage-eval-gauge/scripts/run_matrix_cohort.py"
SPEC = importlib.util.spec_from_file_location("matrix_cohort_prepare", SCRIPT)
driver = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(driver)


def inputs(tmp_path, monkeypatch):
    pair = driver._pair_module()
    monkeypatch.setattr(driver, "_pair_module", lambda: pair)
    monkeypatch.setattr(driver, "_private_work_dir", lambda path: path.resolve())
    monkeypatch.setattr(pair, "_fixture_at", lambda *args: {"ready": True})
    monkeypatch.setattr(pair, "_orchestrate", lambda: SimpleNamespace(
        _load_sage_model_environment=lambda: {
            "provider": "openai", "model": "test/model",
            "API_ENDPOINT": "https://example.invalid/api/v1", "API_KEY": "private-test-key",
        }))
    catalog = tmp_path / "matrix.json"
    catalog.write_text(json.dumps({"schema": "sage-trust-walker-model-matrix-v1", "routes": [{
        "route_id": "test-route", "provider": "openai", "model": "test/model",
        "api_endpoint": "https://example.invalid/api/v1",
    }]}))
    smoke_dir = tmp_path / "smoke"
    smoke_dir.mkdir()
    smoke = smoke_dir / "route-smoke.json"
    smoke.write_text(json.dumps({"ok": True, "detail": {
        "configured_provider": "openai", "configured_model": "test/model",
        "observed_backend": {"effective_backend": "openai:test/model",
                             "observed_provider": "openai", "observed_model": "test/model"},
        "usage": {"total_tokens": 1},
    }}))
    (smoke_dir / "matrix-runtime-route-probe.json").write_text(json.dumps({
        "provider": "openai", "model": "test/model", "endpoint_host": "example.invalid",
        "endpoint_port": 443, "endpoint_path": "/api/v1", "reachable": True,
        "listed": True, "inference_calls": 0,
    }))
    output = tmp_path / "prepared"
    argv = ["prepare", "--cohort-id", "test-cohort", "--route-id", "test-route",
            "--contract-file", str(catalog), "--pairs", "2", "--case", "samwell",
            "--route-smoke", str(smoke), "--output-dir", str(output)]
    return pair, catalog, smoke, output, argv


def test_active_prepare_binds_each_pair_to_real_producer_validator(tmp_path, monkeypatch):
    pair, catalog, smoke, output, argv = inputs(tmp_path, monkeypatch)
    argv += ["--same-arm-recovery", "--activate-authority", "--authorization-source",
             "Operator authorized this two-pair test in the active session."]
    assert driver.main(argv) == 0
    plan = driver.load_plan(output / "cohort-plan.json")
    assert len(plan["slots"]) == 2
    assert [slot["arm_order"] for slot in plan["slots"]] == ["shipping-first", "raw-first"]
    assert plan["same_arm_recovery"] is True
    assert plan["continue_after_time_limit"] is True
    assert plan["continue_after_refusal"] is True
    assert plan["rehearsal_policy"] == "once-per-cohort"
    assert "Setup rehearsal policy: once-per-cohort." in (output / "protocol.md").read_text()
    assert driver.main(["plan", "--plan", str(output / "cohort-plan.json")]) == 0
    assert pair._validate_protected(output / "protected-source-manifest.json", ROOT)["count"] == 7
    matrix = pair.load_matrix_contract(catalog)
    for slot in plan["slots"]:
        order = pair.ARM_ORDER if slot["arm_order"] == "shipping-first" else tuple(reversed(pair.ARM_ORDER))
        contract = pair.MatrixPairContract.for_route(
            matrix, "test-route", start_case="samwell-castelblack", arm_order=order,
            source_identity=pair._source_identity_at(ROOT), wall_seconds=3600,
            continue_after_time_limit=True, continue_after_refusal=True)
        authority = slot["attempts"][0]["live_authority_file"]
        receipt = pair._resolve_live_authority(SimpleNamespace(
            command="run-pair", live_authority_file=authority,
            live_authority_id=None, resumable=True, resume=False), contract)
        assert receipt["contract_binding_sha256"] == contract.identity
        assert contract.to_record()["termination_policy"]["numeric_wall_token_task_ceilings"] == {
            "wall_seconds": 3600, "token_ceiling": None, "payload_task_limit": None}
    assert "private-test-key" not in "".join(path.read_text() for path in output.iterdir())
    assert driver.main(argv) == 1  # no prospective overwrite


def test_prepare_can_disable_refusal_continuation(tmp_path, monkeypatch):
    pair, catalog, smoke, output, argv = inputs(tmp_path, monkeypatch)
    assert driver.main(argv + ["--no-continue-after-refusal"]) == 0
    plan = driver.load_plan(output / "cohort-plan.json")
    assert plan["continue_after_refusal"] is False
    authority = json.loads((output / "authority-slot-001.json").read_text())
    assert "continue_after_refusal" not in authority["scope"]
    slot = plan["slots"][0]
    assert "--continue-after-refusal" not in driver._cli(
        "run-pair", plan, slot, slot["attempts"][0], output / "pair")


def test_draft_and_missing_prerequisites_fail_closed(tmp_path, monkeypatch):
    pair, catalog, smoke, output, argv = inputs(tmp_path, monkeypatch)
    assert driver.main(argv) == 0
    authority = json.loads((output / "authority-slot-001.json").read_text())
    assert authority["status"] == "draft"
    assert driver.main(argv) == 1

    second = tmp_path / "second"
    missing = [*argv[:-1], str(second)]
    smoke.unlink()
    assert driver.main(missing) == 1
    assert not second.exists()


def test_activation_requires_explicit_authorization_source(tmp_path, monkeypatch):
    _, _, _, output, argv = inputs(tmp_path, monkeypatch)
    assert driver.main([*argv, "--activate-authority"]) == 1
    assert not output.exists()


def test_route_mismatch_stops_before_any_files(tmp_path, monkeypatch):
    pair, _, _, output, argv = inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(pair, "_orchestrate", lambda: SimpleNamespace(
        _load_sage_model_environment=lambda: {
            "provider": "openai", "model": "wrong-model",
            "API_ENDPOINT": "https://example.invalid/api/v1", "API_KEY": "secret",
        }))
    assert driver.main(argv) == 1
    assert not output.exists()


def test_balanced_cases_bind_qualified_descriptor_without_copying_it(tmp_path, monkeypatch):
    pair, _, _, output, argv = inputs(tmp_path, monkeypatch)
    from ai.hillclimb import trust_walker_pilot as pilot
    descriptor = tmp_path / "jorah-case.json"
    descriptor.write_text(json.dumps({
        "schema": pair.ALTERNATE_START_SCHEMA, "case_id": pilot.ALTERNATE_START_CASE,
        "snapshot": pilot.ALTERNATE_START_SNAPSHOT, "range_profile": pair.RANGE_PROFILE,
        "inventory": list(pair.CANONICAL_INVENTORY),
        "principal": pilot.ALTERNATE_START_PRINCIPAL, "host": pilot.ALTERNATE_START_HOST,
        "host_ip": pilot.ALTERNATE_START_IP, "payload_type": "Apollo",
        "retained_config": {"path": str(tmp_path / "config.json"), "sha256": "0" * 64,
                            "payload_uuid": "test-payload"},
        "staged_executable": {"task_name": "test-task", "task_prelaunch_state": "Ready",
                              "remote_path": r"C:\test.exe", "sha256": "1" * 64},
    }))
    args = ["balanced" if value == "samwell" else "4" if value == "2" else value for value in argv]
    args += ["--case-descriptor", str(descriptor)]
    assert driver.main(args) == 0
    plan = driver.load_plan(output / "cohort-plan.json")
    assert [slot["case_descriptor"] for slot in plan["slots"]] == [
        None, str(descriptor), None, str(descriptor)]
    assert [slot["arm_order"] for slot in plan["slots"]] == [
        "shipping-first", "shipping-first", "raw-first", "raw-first"]
    authority = json.loads((output / "authority-slot-002.json").read_text())
    assert authority["allowed_start_cases"] == ["jorah-meereen"]
    assert authority["snapshot_bindings"]["jorah-meereen"] == pilot.ALTERNATE_START_SNAPSHOT
    assert not (output / descriptor.name).exists()
