"""Exercise the operator reporting boundary with original typed effect producers."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket

import pytest

from ai.hillclimb import reproducible_claims as claims


def fixture(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(tmp_path, packet, binary=None):
    label = fixture("test_reproducible_claims").identity("shipping-sage")
    packet = {**packet, "measurement_identity": label}
    expected = claims.replay_capability_effect(**packet)
    document = {"packet": packet, "identities": [label], "expected": expected}
    sources = {}
    if binary:
        document = deepcopy(document)
        for key, value in binary.items():
            path = tmp_path / (key + ".bin")
            path.write_bytes(value)
            sources[key] = {"path": path.name, "sha256": hashlib.sha256(value).hexdigest(),
                            "version": "synthetic-v1", "format": "binary"}
            del document["packet"]["evidence"][key]
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(document), encoding="utf8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sources["packet"] = {"path": path.name, "sha256": digest, "version": "synthetic-v1", "format": "json"}
    def ref(source, pointer):
        return {"source": {"source_ref": source, "source_version": sources[source]["version"],
                           "source_sha256": sources[source]["sha256"], "field_pointer": pointer}}
    inputs = ref("packet", "/packet")
    if binary:
        inputs = {key: ref("packet", f"/packet/{key}") for key in packet if key != "evidence"}
        inputs["evidence"] = {
            key: ref(key, "/bytes") if key in binary else ref("packet", f"/packet/evidence/{key}")
            for key in packet["evidence"]}
    manifest = {"schema": claims.SCHEMA, "evaluator_bindings": claims.evaluator_bindings(),
                "sources": sources, "claims": [{
                    "id": "effect", "reducer": "capability_effect", "inputs": inputs,
                    "identities": ref("packet", "/identities"), "expected": ref("packet", "/expected"),
                    "supersedes": None, "amendment_reason": None}]}
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps(manifest))
    return target, expected


def _ticket_packet():
    return {"effect": "CROSS_DOMAIN_CONTEXT", "expected_target": "parent.local", "evidence": {
        "rows": fixture("test_hillclimb_ticket_context")._rows(), "operation_id": 7, "callback_id": 4,
        "target": "parent.local", "baseline_task_ids": [12]}}


def test_real_operator_command_replays_effect_and_records_offline_cost(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("offline replay opened a socket"))
    path, expected = _manifest(tmp_path, _ticket_packet())
    assert expected["disposition"] == "proven"
    command = claims._load_script("verify_claims")
    assert command.main([str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    cost = report["claims"][0]["offline_replay_cost"]
    assert cost["wall_seconds"] >= 0 and cost["control_plane_reads"] == cost["model_calls"] == 0
    assert cost["monetary_cost_usd"] is None
    assert report["claims"][0]["verifier_cost"]["declared_wall_seconds"] == 30


def test_replay_binding_refuses_synthetic_success_wrong_target_and_echo(tmp_path):
    packet = _ticket_packet()
    packet["evidence"]["rows"][0]["command_name"] = "shell"
    packet["evidence"]["rows"][0]["original_params"] = "echo golden ticket successfully imported"
    _, expected = _manifest(tmp_path, packet)
    assert expected["disposition"] == "unmeasured"
    packet = _ticket_packet()
    packet["expected_target"] = "unrelated.local"
    _, expected = _manifest(tmp_path, packet)
    assert expected["disposition"] == "invalid"


def test_binary_certificate_source_is_replayed_and_missing_or_changed_bytes_fail(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from ai.hillclimb import certificate_material
    fixtures = fixture("test_hillclimb_certificate_material")
    key, cert = fixtures._ca()
    artifact = fixtures._pfx(key, cert)
    captured = {}
    original = certificate_material.certificate_material_observation
    def capture(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(certificate_material, "certificate_material_observation", capture)
    assert fixtures._positive("CA_KEY_EXPORTED", artifact, ca_cert=cert) is not None
    monkeypatch.setattr(certificate_material, "certificate_material_observation", original)
    captured.pop("kind")
    captured["expected_ca_certificate"] = cert.public_bytes(serialization.Encoding.PEM)
    if isinstance(captured.get("artifact_password"), bytes):
        captured["artifact_password"] = captured["artifact_password"].decode()
    packet = {"effect": "CA_KEY_EXPORTED", "expected_target": fixtures.TARGET, "evidence": captured}
    binary = {name: captured[name] for name in ("artifact_bytes", "expected_ca_certificate")}
    path, expected = _manifest(tmp_path, packet, binary)
    assert expected["disposition"] == "proven"
    report = claims.verify_claims(path)
    assert report["verified"] is True
    assert "PRIVATE KEY" not in json.dumps(report)
    artifact_path = tmp_path / "artifact_bytes.bin"
    artifact_path.write_bytes(b"changed")
    with pytest.raises(claims.ClaimError, match="source bytes changed"):
        claims.verify_claims(path)
    artifact_path.unlink()
    with pytest.raises(FileNotFoundError):
        claims.verify_claims(path)


def test_claim_identity_cannot_disagree_with_effect_producer_metadata(tmp_path):
    path, _ = _manifest(tmp_path, _ticket_packet())
    document = json.loads((tmp_path / "packet.json").read_text())
    document["identities"][0]["wrapper_revision"] = "other-revision"
    (tmp_path / "packet.json").write_text(json.dumps(document))
    manifest = json.loads(path.read_text())
    old = manifest["sources"]["packet"]["sha256"]
    new = hashlib.sha256((tmp_path / "packet.json").read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest).replace(old, new))
    with pytest.raises(claims.ClaimError, match="effect labels"):
        claims.verify_claims(path)


def test_effect_timeout_preserves_cost_and_restores_timer(monkeypatch):
    import signal
    import time
    prior = signal.getsignal(signal.SIGALRM)
    monkeypatch.setattr(claims, "replay_capability_effect", lambda **kwargs: time.sleep(1))
    with pytest.raises(claims.ClaimError) as raised:
        claims._bounded_effect_replay(_ticket_packet(), wall_limit=0.01)
    cost = raised.value.verifier_cost
    assert cost["timeout_invocations"] == cost["failed_invocations"] == 1
    assert cost["limit_disposition"] == "exceeded" and cost["wall_seconds"] >= 0.01
    assert cost["monetary_cost_usd"] is None and cost["control_plane_reads"] == 0
    assert signal.getsignal(signal.SIGALRM) == prior and signal.getitimer(signal.ITIMER_REAL)[0] == 0


@pytest.mark.parametrize("entry", ["script", "module"])
def test_both_cli_entries_preserve_failed_parser_cost(tmp_path, monkeypatch, capsys, entry):
    from argparse import Namespace
    from ai.hillclimb import __main__ as module
    path, _ = _manifest(tmp_path, _ticket_packet())
    def failed(**kwargs):
        raise TimeoutError("sensitive parser body must never appear")
    monkeypatch.setattr(claims, "replay_capability_effect", failed)
    code = (claims._load_script("verify_claims").main([str(path)]) if entry == "script"
            else module._cmd_verify_claims(Namespace(manifest=path)))
    raw = capsys.readouterr().out
    report = json.loads(raw)
    assert code == 2 and report["verified"] is False
    assert report["verifier_cost"]["failed_invocations"] == 1
    assert report["verifier_cost"]["timeout_invocations"] == 1
    assert "sensitive" not in raw
