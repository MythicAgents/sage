from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

common = importlib.import_module("arch_governor_common")
review_lease = importlib.import_module("review_lease")
hook = importlib.import_module("pre_tool_use_arch_gate")


@pytest.fixture(autouse=True)
def _use_repository_index(monkeypatch):
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)


def _repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=tmp_path,
        check=True,
    )
    source = tmp_path / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return tmp_path


def _record_artifact(
    root: Path,
    relative: str,
    payload: dict,
    *,
    artifact_type: str,
    recorded_at: datetime,
    manifest: bool = True,
) -> Path:
    history = root / ".sage_history"
    history.mkdir(mode=0o700, exist_ok=True)
    path = history / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if manifest:
        record = {
            "schema": "sage-artifact-manifest-v1",
            "record_kind": "artifact",
            "artifact_path": relative,
            "artifact_type": artifact_type,
            "recorded_at": recorded_at.isoformat(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        with (history / "manifest.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    elif not (history / "manifest.jsonl").exists():
        (history / "manifest.jsonl").write_text("")
    return path


BASE_TIME = datetime(2026, 8, 26, 12, tzinfo=UTC)


def _gate_contract(
    root: Path,
    *,
    lane: str = "runtime_bugfix",
    epoch: int = 1,
    mechanism: str = "turn-authority",
    governed_paths: list[str] | None = None,
    production_path: str = "service -> model -> authority",
    approval_scope: str = "approved process change",
    name: str | None = None,
    isa_path: Path | None = None,
) -> Path:
    payload = {
        "schema": "sage-architecture-gate-v1",
        "review_lane": lane,
        "isa_path": str(isa_path or root / "source.py"),
        "isa_gate": "Gate Process",
        "isa_criteria": ["ISC-30", "ISC-7"],
        "review_domain": "conversation_behavior",
        "mechanism_id": mechanism,
        "design_epoch": epoch,
        "exact_production_path": production_path,
        "governed_paths": governed_paths or ["source.py"],
        "operator_approval_scope": approval_scope,
    }
    filename = name or f"gate-{lane}-e{epoch}-{mechanism}.json"
    return _record_artifact(
        root,
        f"contracts/governance/{filename}",
        payload,
        artifact_type="architecture-gate-contract",
        recorded_at=BASE_TIME,
    )


def _reviewer() -> dict:
    return {
        "profile_name": "sage_eval_reviewer",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "profile_config_sha256": "a" * 64,
        "route_verified": True,
        "authority": "read_only",
    }


def _design_receipt(
    root: Path,
    gate_path: Path,
    *,
    supersedes: dict | None = None,
    overrides: dict | None = None,
    manifest: bool = True,
    name: str = "design-review.json",
) -> Path:
    gate = review_lease._load_gate_contract(root, gate_path)
    payload = {
        "artifact_type": "sealed_evaluation_design_review",
        "disposition": "DESIGN_ACCEPT",
        "reviewer": _reviewer(),
        "gate_contract_path": gate["repo_path"],
        "gate_contract_sha256": gate["sha256"],
        "operator_approval_scope": gate["operator_approval_scope"],
        "process_guard_key": gate["process_guard_key"],
        "design_epoch": gate["design_epoch"],
        "exact_production_path": gate["exact_production_path"],
        "governed_paths": gate["governed_paths"],
    }
    if supersedes is not None:
        payload["supersedes"] = supersedes
    payload.update(overrides or {})
    return _record_artifact(
        root,
        f"reviews/governance/{name}",
        payload,
        artifact_type="sealed-evaluation-design-review",
        recorded_at=BASE_TIME + timedelta(minutes=1),
        manifest=manifest,
    )


def _recovery_receipt(
    root: Path,
    gate_path: Path,
    rejected_candidate_id: str,
    *,
    overrides: dict | None = None,
    manifest: bool = True,
    name: str = "recovery-review.json",
) -> Path:
    gate = review_lease._load_gate_contract(root, gate_path)
    payload = {
        "artifact_type": "sealed_evaluation_recovery_review",
        "disposition": "RECOVERY_ACCEPT",
        "reviewer": _reviewer(),
        "gate_contract_path": gate["repo_path"],
        "gate_contract_sha256": gate["sha256"],
        "operator_approval_scope": gate["operator_approval_scope"],
        "process_guard_key": gate["process_guard_key"],
        "design_epoch": gate["design_epoch"],
        "exact_production_path": gate["exact_production_path"],
        "governed_paths": gate["governed_paths"],
        "rejected_candidate_id": rejected_candidate_id,
        "root_cause_classes": ["caller-controlled lane"],
        "simplification": "derive lane from the gate",
        "named_falsifier": "a caller-selected lane changes the lease",
        "property_probe_evidence": [
            {
                "command": "pytest -q test_review_lease.py",
                "output": "1 passed",
                "exit_code": 0,
                "reviewer_reproduced": True,
            }
        ],
    }
    payload.update(overrides or {})
    return _record_artifact(
        root,
        f"reviews/governance/{name}",
        payload,
        artifact_type="sealed-evaluation-recovery-review",
        recorded_at=BASE_TIME + timedelta(minutes=2),
        manifest=manifest,
    )


def _freeze_args(
    root: Path,
    *,
    gate_contract: Path | None = None,
    governed_paths: list[str] | None = None,
) -> argparse.Namespace:
    gate_contract = gate_contract or _gate_contract(
        root,
        governed_paths=governed_paths,
        isa_path=root / (governed_paths or ["source.py"])[0],
    )
    return argparse.Namespace(
        repo=str(root),
        paths=governed_paths or ["source.py"],
        protected=[],
        review_stage="source_candidate",
        review_domain="conversation_behavior",
        independence_class="internal_subagent",
        mechanism_id="turn-authority",
        review_round=1,
        governing_gate="approved-gate",
        gate_contract=str(gate_contract),
        design_receipt=None,
        recovery_receipt=None,
    )


def _stage_source(root: Path, value: int) -> None:
    (root / "source.py").write_text(f"value = {value}\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)


def _close_active(root: Path, disposition: str) -> dict:
    lease = json.loads(common.review_lease_path(root).read_text())
    assert review_lease.close(
        argparse.Namespace(
            repo=str(root),
            lease_id=lease["lease_id"],
            disposition=disposition,
        )
    ) == 0
    return lease


def _sealed_args(
    root: Path,
    gate_path: Path,
    design_path: Path,
    recovery_path: Path | None = None,
) -> argparse.Namespace:
    gate = review_lease._load_gate_contract(root, gate_path)
    args = _freeze_args(
        root,
        gate_contract=gate_path,
        governed_paths=gate["governed_paths"],
    )
    args.design_receipt = str(design_path)
    args.recovery_receipt = str(recovery_path) if recovery_path else None
    return args


def _exhaust_epoch(
    root: Path,
    gate_path: Path,
    design_path: Path,
) -> tuple[dict, dict]:
    _stage_source(root, 2)
    assert review_lease.freeze(_sealed_args(root, gate_path, design_path)) == 0
    first = _close_active(root, "rejected")
    _stage_source(root, 3)
    recovery = _recovery_receipt(root, gate_path, first["candidate_id"])
    assert review_lease.freeze(
        _sealed_args(root, gate_path, design_path, recovery)
    ) == 0
    second = _close_active(root, "rejected")
    return first, second


def test_review_lease_verifies_and_detects_drift(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    source = root / "source.py"
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)

    assert review_lease.freeze(_freeze_args(root)) == 0
    assert review_lease.verify(argparse.Namespace(repo=str(root))) == 0
    lease = json.loads(common.review_lease_path(root).read_text())

    source.write_text("value = 3\n", encoding="utf-8")
    assert review_lease.verify(argparse.Namespace(repo=str(root))) == 3
    assert "invalidated_candidate_drift" in capsys.readouterr().out

    assert (
        review_lease.close(
            argparse.Namespace(
                repo=str(root),
                lease_id=lease["lease_id"],
                disposition="invalidated",
            )
        )
        == 0
    )
    assert not common.review_lease_path(root).exists()


def test_close_keeps_active_lease_temporary_and_archives_receipt_durably(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path)
    lease_dir = tmp_path / "temporary-leases"
    history = tmp_path / "private-history"
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(lease_dir))
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(history))
    source = root / "source.py"
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)

    assert review_lease.freeze(_freeze_args(root)) == 0
    lease_path = common.review_lease_path(root)
    assert lease_path.is_relative_to(lease_dir)
    lease = json.loads(lease_path.read_text())
    capsys.readouterr()

    assert (
        review_lease.close(
            argparse.Namespace(
                repo=str(root),
                lease_id=lease["lease_id"],
                disposition="accepted",
            )
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    archive = Path(output["archive"])
    assert not lease_path.exists()
    assert archive.is_relative_to(history)
    assert archive.name.endswith(".closed.json")
    assert archive.stat().st_mode & 0o777 == 0o600
    assert (history / "manifest.jsonl").is_file()


def test_runtime_bugfix_derives_contract_fields_and_keeps_lightweight_path(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_path = _gate_contract(root, lane="runtime_bugfix")
    _stage_source(root, 2)
    args = _freeze_args(root, gate_contract=gate_path)
    args.review_domain = "provenance"
    args.mechanism_id = "caller-label"
    args.review_round = 17
    args.governing_gate = "caller-gate"

    assert review_lease.freeze(args) == 0
    lease = json.loads(common.review_lease_path(root).read_text())
    gate = review_lease._load_gate_contract(root, gate_path)
    assert lease["review_lane"] == "runtime_bugfix"
    assert lease["review_domain"] == gate["review_domain"]
    assert lease["mechanism_id"] == gate["mechanism_id"]
    assert lease["governing_gate"] == gate["isa_gate"]
    assert lease["process_guard_key"] == gate["process_guard_key"]
    assert lease["review_round"] == 1
    assert review_lease.verify(argparse.Namespace(repo=str(root))) == 0


def test_sealed_round_one_requires_and_records_fresh_design_accept(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_path = _gate_contract(root, lane="sealed_evaluation")
    design_path = _design_receipt(root, gate_path)
    args = _sealed_args(root, gate_path, design_path)
    _stage_source(root, 2)

    assert review_lease.freeze(_sealed_args(root, gate_path, design_path)) == 0
    lease = json.loads(common.review_lease_path(root).read_text())
    assert lease["review_lane"] == "sealed_evaluation"
    assert lease["review_round"] == 1
    assert lease["design_receipt_path"].endswith("design-review.json")
    assert "recovery_receipt_path" not in lease


@pytest.mark.parametrize(
    "case",
    ["missing", "self_attested", "mismatched", "stale", "orphan"],
)
def test_sealed_round_one_refuses_invalid_design_results(
    tmp_path, monkeypatch, case
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_path = _gate_contract(root, lane="sealed_evaluation")
    design_path = None
    if case == "self_attested":
        design_path = _design_receipt(
            root,
            gate_path,
            overrides={"reviewer": {}, "accepted": True},
        )
    elif case == "mismatched":
        design_path = _design_receipt(
            root,
            gate_path,
            overrides={"gate_contract_sha256": "0" * 64},
        )
    elif case == "orphan":
        design_path = _design_receipt(root, gate_path, manifest=False)
    elif case == "stale":
        design_path = _design_receipt(root, gate_path)
        payload = json.loads(design_path.read_text())
        payload["extra"] = "changed after retention"
        design_path.write_text(json.dumps(payload))
    _stage_source(root, 2)
    args = _freeze_args(root, gate_contract=gate_path)
    args.design_receipt = str(design_path) if design_path else None

    assert review_lease.freeze(args) == 2
    assert not common.review_lease_path(root).exists()


@pytest.mark.parametrize(
    "component", ["history_root", "manifest", "artifact", "intervening"]
)
def test_sealed_design_refuses_symlinked_history_components(
    tmp_path, monkeypatch, component
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_path = _gate_contract(root, lane="sealed_evaluation")
    design_path = _design_receipt(root, gate_path)
    args = _sealed_args(root, gate_path, design_path)
    if component == "history_root":
        real = root / "real-history"
        (root / ".sage_history").rename(real)
        (root / ".sage_history").symlink_to(real, target_is_directory=True)
    elif component == "manifest":
        manifest = root / ".sage_history" / "manifest.jsonl"
        real = manifest.with_name("manifest.real")
        manifest.rename(real)
        manifest.symlink_to(real)
    elif component == "artifact":
        real = design_path.with_name("design.real.json")
        design_path.rename(real)
        design_path.symlink_to(real)
    else:
        review_dir = design_path.parent
        real = review_dir.with_name("governance-real")
        review_dir.rename(real)
        review_dir.symlink_to(real, target_is_directory=True)
    _stage_source(root, 2)

    assert review_lease.freeze(args) == 2


def test_source_candidate_requires_gate_and_exact_governed_paths(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    _stage_source(root, 2)
    args = _freeze_args(root)
    args.gate_contract = None
    assert review_lease.freeze(args) == 2

    gate_path = _gate_contract(
        root,
        governed_paths=["source.py", "other.py"],
        name="path-scope.json",
    )
    args = _freeze_args(root, gate_contract=gate_path)
    assert review_lease.freeze(args) == 2


def test_sealed_cli_labels_and_round_cannot_override_gate_contract(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_path = _gate_contract(root, lane="sealed_evaluation")
    design_path = _design_receipt(root, gate_path)
    gate = review_lease._load_gate_contract(root, gate_path)
    _stage_source(root, 2)
    args = _sealed_args(root, gate_path, design_path)
    args.review_domain = "provenance"
    args.mechanism_id = "new-family-label"
    args.review_round = 99
    args.governing_gate = "different gate"

    assert review_lease.freeze(args) == 0
    lease = json.loads(common.review_lease_path(root).read_text())
    assert lease["review_domain"] == gate["review_domain"]
    assert lease["mechanism_id"] == gate["mechanism_id"]
    assert lease["review_round"] == 1
    assert lease["governing_gate"] == gate["isa_gate"]


def test_sealed_round_two_requires_and_records_recovery_accept(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_path = _gate_contract(root, lane="sealed_evaluation")
    design_path = _design_receipt(root, gate_path)
    _stage_source(root, 2)
    assert review_lease.freeze(_sealed_args(root, gate_path, design_path)) == 0
    first = _close_active(root, "rejected")
    recovery = _recovery_receipt(root, gate_path, first["candidate_id"])
    _stage_source(root, 3)

    assert review_lease.freeze(
        _sealed_args(root, gate_path, design_path, recovery)
    ) == 0
    lease = json.loads(common.review_lease_path(root).read_text())
    assert lease["review_round"] == 2
    assert lease["recovery_receipt_path"].endswith("recovery-review.json")


def test_recovery_evidence_is_validated_but_never_executed(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_path = _gate_contract(root, lane="sealed_evaluation")
    design_path = _design_receipt(root, gate_path)
    _stage_source(root, 2)
    assert review_lease.freeze(_sealed_args(root, gate_path, design_path)) == 0
    first = _close_active(root, "rejected")
    marker = root / "freeze-must-not-run-this"
    recovery = _recovery_receipt(
        root,
        gate_path,
        first["candidate_id"],
        overrides={
            "property_probe_evidence": [
                {
                    "command": f"touch {marker}",
                    "output": "reviewer observed success",
                    "exit_code": 0,
                    "reviewer_reproduced": True,
                }
            ]
        },
    )
    _stage_source(root, 3)

    assert review_lease.freeze(
        _sealed_args(root, gate_path, design_path, recovery)
    ) == 0
    assert not marker.exists()


@pytest.mark.parametrize("case", ["missing", "mismatched", "self_attested"])
def test_sealed_round_two_refuses_invalid_recovery_results(
    tmp_path, monkeypatch, case
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_path = _gate_contract(root, lane="sealed_evaluation")
    design_path = _design_receipt(root, gate_path)
    _stage_source(root, 2)
    assert review_lease.freeze(_sealed_args(root, gate_path, design_path)) == 0
    first = _close_active(root, "rejected")
    recovery = None
    if case == "mismatched":
        recovery = _recovery_receipt(
            root,
            gate_path,
            "sha256:not-the-rejected-candidate",
        )
    elif case == "self_attested":
        recovery = _recovery_receipt(
            root,
            gate_path,
            first["candidate_id"],
            overrides={"reviewer": {"profile_name": "candidate_owner"}},
        )
    _stage_source(root, 3)

    assert review_lease.freeze(
        _sealed_args(root, gate_path, design_path, recovery)
    ) == 2


def test_third_candidate_is_refused_even_with_history_root_and_label_laundering(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_path = _gate_contract(root, lane="sealed_evaluation")
    design_path = _design_receipt(root, gate_path)
    _first, second = _exhaust_epoch(root, gate_path, design_path)
    fake_history = tmp_path / "empty-history"
    fake_history.mkdir()
    monkeypatch.setenv("SAGE_HISTORY_ROOT", str(fake_history))
    _stage_source(root, 4)
    recovery = _recovery_receipt(
        root,
        gate_path,
        second["candidate_id"],
        name="unused-third-recovery.json",
    )
    args = _sealed_args(root, gate_path, design_path, recovery)
    args.mechanism_id = "renamed-family"
    args.review_round = 1

    assert review_lease.freeze(args) == 2


@pytest.mark.parametrize("disposition", ["rejected", "accepted"])
def test_duplicate_and_contradictory_candidate_receipts_fail_closed(
    tmp_path, monkeypatch, disposition
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_path = _gate_contract(root, lane="sealed_evaluation")
    design_path = _design_receipt(root, gate_path)
    _stage_source(root, 2)
    assert review_lease.freeze(_sealed_args(root, gate_path, design_path)) == 0
    first = _close_active(root, "rejected")
    original = next(
        path
        for path in (root / ".sage_history").rglob("*.closed.json")
        if json.loads(path.read_text()).get("candidate_id") == first["candidate_id"]
    )
    duplicate = json.loads(original.read_text())
    duplicate["close_disposition"] = disposition
    _record_artifact(
        root,
        f"2026/08/governance/architecture-reviews/duplicate-{disposition}.closed.json",
        duplicate,
        artifact_type="closed-architecture-review",
        recorded_at=BASE_TIME + timedelta(minutes=3),
    )
    _stage_source(root, 3)

    assert review_lease.freeze(_sealed_args(root, gate_path, design_path)) == 2


def test_malformed_counted_receipt_fails_closed(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_path = _gate_contract(root, lane="sealed_evaluation")
    design_path = _design_receipt(root, gate_path)
    gate = review_lease._load_gate_contract(root, gate_path)
    malformed = {
        "process_guard_key": gate["process_guard_key"],
        "candidate_id": "sha256:malformed",
    }
    _record_artifact(
        root,
        "2026/08/governance/architecture-reviews/malformed.closed.json",
        malformed,
        artifact_type="closed-architecture-review",
        recorded_at=BASE_TIME + timedelta(minutes=3),
    )
    _stage_source(root, 2)

    assert review_lease.freeze(_sealed_args(root, gate_path, design_path)) == 2


def test_higher_epoch_requires_exhausted_predecessor_binding(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_one = _gate_contract(root, lane="sealed_evaluation")
    design_one = _design_receipt(root, gate_one, name="design-e1.json")
    _exhaust_epoch(root, gate_one, design_one)
    gate_two = _gate_contract(
        root,
        lane="sealed_evaluation",
        epoch=2,
        mechanism="simplified-turn-authority",
    )
    unbound = _design_receipt(root, gate_two, name="design-e2-unbound.json")
    _stage_source(root, 4)

    assert review_lease.freeze(_sealed_args(root, gate_two, unbound)) == 2


def test_higher_epoch_refuses_contradictory_accepted_predecessor(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_one = _gate_contract(root, lane="sealed_evaluation")
    design_one = _design_receipt(root, gate_one, name="design-e1.json")
    _first, second = _exhaust_epoch(root, gate_one, design_one)
    original = next(
        path
        for path in (root / ".sage_history").rglob("*.closed.json")
        if json.loads(path.read_text()).get("candidate_id")
        == second["candidate_id"]
    )
    contradictory = json.loads(original.read_text())
    contradictory["candidate_id"] = "sha256:contradictory-accepted"
    contradictory["lease_id"] = "contradictory-accepted-lease"
    contradictory["close_disposition"] = "accepted"
    _record_artifact(
        root,
        "2026/08/governance/architecture-reviews/"
        "contradictory-accepted.closed.json",
        contradictory,
        artifact_type="closed-architecture-review",
        recorded_at=BASE_TIME + timedelta(minutes=3),
    )
    gate_two = _gate_contract(
        root,
        lane="sealed_evaluation",
        epoch=2,
        mechanism="simplified-turn-authority",
    )
    gate = review_lease._load_gate_contract(root, gate_two)
    design_two = _design_receipt(
        root,
        gate_two,
        supersedes={
            "process_guard_key": gate["process_guard_key"],
            "design_epoch": 1,
            "operator_approval_scope": gate["operator_approval_scope"],
        },
        name="design-e2-bound.json",
    )
    _stage_source(root, 4)

    assert review_lease.freeze(_sealed_args(root, gate_two, design_two)) == 2


def test_bound_higher_epoch_starts_new_round_one(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_one = _gate_contract(root, lane="sealed_evaluation")
    design_one = _design_receipt(root, gate_one, name="design-e1.json")
    _exhaust_epoch(root, gate_one, design_one)
    gate_two = _gate_contract(
        root,
        lane="sealed_evaluation",
        epoch=2,
        mechanism="simplified-turn-authority",
    )
    gate = review_lease._load_gate_contract(root, gate_two)
    supersedes = {
        "process_guard_key": gate["process_guard_key"],
        "design_epoch": 1,
        "operator_approval_scope": gate["operator_approval_scope"],
    }
    design_two = _design_receipt(
        root,
        gate_two,
        supersedes=supersedes,
        name="design-e2-bound.json",
    )
    _stage_source(root, 4)

    assert review_lease.freeze(_sealed_args(root, gate_two, design_two)) == 0
    lease = json.loads(common.review_lease_path(root).read_text())
    assert lease["design_epoch"] == 2
    assert lease["review_round"] == 1


def test_same_epoch_name_and_path_changes_do_not_reset_process_key(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    gate_one = _gate_contract(root, lane="sealed_evaluation")
    design_one = _design_receipt(root, gate_one, name="design-original.json")
    _exhaust_epoch(root, gate_one, design_one)
    other = root / "other.py"
    other.write_text("value = 4\n")
    subprocess.run(["git", "add", "other.py"], cwd=root, check=True)
    changed_gate = _gate_contract(
        root,
        lane="sealed_evaluation",
        mechanism="renamed-mechanism",
        governed_paths=["other.py"],
        production_path="a completely renamed production path",
        name="changed-same-epoch.json",
    )
    changed_design = _design_receipt(
        root,
        changed_gate,
        name="changed-same-epoch-design.json",
    )

    assert review_lease.freeze(
        _sealed_args(root, changed_gate, changed_design)
    ) == 2


def test_review_lease_detects_unrelated_index_drift(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    source = root / "source.py"
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)
    unrelated = root / "unrelated.txt"
    unrelated.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=root, check=True)

    assert review_lease.freeze(_freeze_args(root)) == 0
    capsys.readouterr()
    unrelated.write_text("after\n", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=root, check=True)

    assert review_lease.verify(argparse.Namespace(repo=str(root))) == 3
    output = json.loads(capsys.readouterr().out)
    assert any(row["field"] == "index_sha256" for row in output["drift"])


def test_freeze_refuses_unstaged_and_split_candidate_bytes(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    source = root / "source.py"

    source.write_text("value = 2\n", encoding="utf-8")
    assert review_lease.freeze(_freeze_args(root)) == 2

    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)
    source.write_text("value = 3\n", encoding="utf-8")
    assert review_lease.freeze(_freeze_args(root)) == 2


def test_hook_blocks_write_to_active_review_lease(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    source = root / "source.py"
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)
    assert review_lease.freeze(_freeze_args(root)) == 0

    patch = (
        "*** Begin Patch\n"
        "*** Update File: source.py\n"
        "@@\n"
        "-value = 2\n"
        "+value = 3\n"
        "*** End Patch\n"
    )
    _code, payload = hook.run_hook(
        {"tool_name": "apply_patch", "tool_input": {"command": patch}}
    )
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "active Sage review lease" in payload["hookSpecificOutput"][
        "permissionDecisionReason"
    ]

    _code, index_payload = hook.run_hook(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "git add --all"},
        }
    )
    assert index_payload["hookSpecificOutput"]["permissionDecision"] == "deny"

    for command in (
        "echo changed>source.py",
        "printf changed >>source.py",
        "command 2>source.py",
        f'echo changed>"{source}"',
    ):
        _code, redirect_payload = hook.run_hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
            }
        )
        assert (
            redirect_payload["hookSpecificOutput"]["permissionDecision"]
            == "deny"
        )


def test_hook_blocks_explicit_shell_writers_and_parent_removal(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    frozen_dir = root / "frozen"
    frozen_dir.mkdir()
    source = root / "source.py"
    source.rename(frozen_dir / "source.py")
    protected = frozen_dir / "protected.txt"
    protected.write_text("do not change\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    args = _freeze_args(root, governed_paths=["frozen/source.py"])
    args.protected = ["frozen/protected.txt"]
    assert review_lease.freeze(args) == 0

    for command in (
        "printf changed|tee frozen/source.py",
        "printf changed | tee -a ./frozen/protected.txt",
        "truncate -s 0 frozen/source.py",
        "dd if=/dev/zero of=frozen/protected.txt bs=1 count=1",
        "rm frozen",
    ):
        _code, payload = hook.run_hook(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        )
        assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_shell_writer_positive_controls_do_not_claim_source_arguments(
    tmp_path, monkeypatch
):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("SAGE_ARCH_REVIEW_DIR", str(tmp_path / "leases"))
    source = root / "source.py"
    source.write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=root, check=True)
    assert review_lease.freeze(_freeze_args(root)) == 0

    for command in (
        "printf source.py",
        "printf changed | tee unrelated.txt",
        "truncate --reference=source.py unrelated.txt",
        "dd if=source.py of=unrelated.txt",
        "rm unrelated",
        "cp source.py unrelated.txt",
        "cat ./source.py",
    ):
        _code, payload = hook.run_hook(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        )
        assert payload == {}


def test_authority_and_chat_lifecycle_paths_are_high_risk(tmp_path):
    paths = common.high_risk_paths(
        [
            "Payload_Type/sage/ai/langgraph/turn_authority.py",
            "Payload_Type/sage/ai/langgraph/objective_contract.py",
            "Payload_Type/sage/sage_chat/service.py",
        ],
        tmp_path,
    )
    assert paths == [
        "Payload_Type/sage/ai/langgraph/objective_contract.py",
        "Payload_Type/sage/ai/langgraph/turn_authority.py",
        "Payload_Type/sage/sage_chat/service.py",
    ]


def test_dot_prefixed_repo_paths_are_preserved(tmp_path):
    assert (
        common.normalize_repo_path(
            ".codex/agents/sage_eval_reviewer.toml", tmp_path
        )
        == ".codex/agents/sage_eval_reviewer.toml"
    )
    assert common.matches_any(
        ".codex/agents/sage_eval_reviewer.toml",
        [".codex/agents/**"],
    )
