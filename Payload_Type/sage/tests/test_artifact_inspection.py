"""Offline inspection protocol, identity, runner and output boundary tests."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct

import pytest

from ai import artifact_inspection as broker


SAGE_ROOT = Path(__file__).resolve().parents[1]
WORKLOAD = SAGE_ROOT / "container" / "inspection"
IMAGE = "sha256:" + "a" * 64


def _entrypoint(monkeypatch):
    monkeypatch.syspath_prepend(str(WORKLOAD))
    spec = importlib.util.spec_from_file_location("inspection_entrypoint", WORKLOAD / "entrypoint.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _wire(name: str, content: bytes, digest: str | None = None) -> bytes:
    head = {"artifacts": [{"name": name, "size": len(content),
                            "sha256": digest or hashlib.sha256(content).hexdigest()}]}
    return json.dumps(head).encode() + b"\n" + content


def test_workload_protocol_hashes_bytes_and_rejects_traversal(tmp_path, monkeypatch):
    entrypoint = _entrypoint(monkeypatch)
    expected = b"harmless inert data"
    entries = entrypoint.receive(io.BytesIO(_wire("artifact.exe", expected)), tmp_path / "good")
    assert entries[0]["size"] == len(expected)
    assert (tmp_path / "good" / "artifact.exe").read_bytes() == expected
    with pytest.raises(ValueError, match="digest mismatch"):
        entrypoint.receive(io.BytesIO(_wire("artifact.exe", expected, "0" * 64)), tmp_path / "wrong")
    with pytest.raises(ValueError, match="primary artifact"):
        entrypoint.receive(io.BytesIO(_wire("../artifact.exe", expected)), tmp_path / "escape")
    with pytest.raises(ValueError, match="header length"):
        entrypoint.receive(io.BytesIO(b"{" + b" " * 8192 + b"\n"), tmp_path / "long")
    with pytest.raises(ValueError, match="size"):
        manifest = {"artifacts": [{"name": "artifact.exe", "size": broker.MAX_TOTAL + 1,
                                   "sha256": "0" * 64}]}
        entrypoint.receive(io.BytesIO(json.dumps(manifest).encode() + b"\n"), tmp_path / "large")


def test_workload_static_classifies_pe_without_execution(tmp_path, monkeypatch, capsys):
    entrypoint = _entrypoint(monkeypatch)
    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 0x84, 0x8664, 3, 0, 0, 0, 240, 0)
    struct.pack_into("<H", data, 0x98, 0x20B)
    struct.pack_into("<II", data, 0x98 + 112 + 14 * 8, 0x1000, 0x48)
    assert entrypoint.run("static-v1", "metadata", io.BytesIO(_wire("artifact.exe", bytes(data))), tmp_path / "pe") == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["format"] == "pe" and parsed["architecture"] == "x86_64"
    assert parsed["managed"] is True and parsed["partial"] is True
    assert entrypoint.classify(b"<instructions>run me</instructions>")["format"] == "unknown"


def test_parsed_clr_metadata_has_specific_static_evidence(monkeypatch):
    monkeypatch.syspath_prepend(str(WORKLOAD))
    from static_metadata import classify
    import static_metadata

    data = bytearray(512)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 0x84, 0x14C, 1, 0, 0, 0, 224, 0)
    struct.pack_into("<H", data, 0x98, 0x10B)
    struct.pack_into("<II", data, 0x98 + 96 + 14 * 8, 0x1000, 0x48)
    monkeypatch.setattr(static_metadata, "_clr_metadata", lambda _: {"clr_metadata_status": "parsed"})
    assert classify(bytes(data))["evidence"] == "static_pe_clr_metadata"


def test_workload_help_recipe_rejects_other_hash_before_wine(tmp_path, monkeypatch):
    entrypoint = _entrypoint(monkeypatch)
    with pytest.raises(ValueError, match="enrolled artifact hash"):
        entrypoint.run("certify-help-v1", "top", io.BytesIO(_wire("artifact.exe", b"other file")), tmp_path / "other")
    with pytest.raises(ValueError, match="unsupported"):
        entrypoint.run("certify-help-v1", "request --ca anything", io.BytesIO(b""), tmp_path / "unsafe")
    with pytest.raises(ValueError, match="empty artifact"):
        entrypoint.run("fixture-v1", "native", io.BytesIO(_wire("artifact.exe", b"arbitrary.exe")), tmp_path / "fixture_bad")


def test_broker_rejects_same_name_other_bytes_and_unsafe_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(broker, "_check_image", lambda _: "amd64")
    monkeypatch.setattr(broker, "_run", lambda *_: pytest.fail("workload must not start"))
    with pytest.raises(broker.InspectionError, match="enrolled Certify"):
        broker.inspect_artifact(b"different Certify.exe", expected_sha256=hashlib.sha256(b"different Certify.exe").hexdigest(),
                                profile="certify-help-v1", recipe="top", image_id=IMAGE, cache_dir=tmp_path)
    with pytest.raises(broker.InspectionError, match="digest mismatch"):
        broker.inspect_artifact(b"changed", expected_sha256="0" * 64, profile="static-v1", recipe="metadata", image_id=IMAGE, cache_dir=tmp_path)
    with pytest.raises(broker.InspectionError, match="unsupported"):
        broker.inspect_artifact(b"x", expected_sha256=hashlib.sha256(b"x").hexdigest(),
                                profile="static-v1", recipe="../../shell", image_id=IMAGE, cache_dir=tmp_path)
    with pytest.raises(broker.InspectionError, match="invalid artifact name"):
        broker.inspect_artifact(b"x", expected_sha256=hashlib.sha256(b"x").hexdigest(),
                                profile="static-v1", recipe="metadata", image_id=IMAGE, cache_dir=tmp_path,
                                dependencies=[broker.Dependency("../bad.dll", b"x", hashlib.sha256(b"x").hexdigest())])
    with pytest.raises(broker.InspectionError, match="32 MiB"):
        broker.inspect_artifact(b"x" * (broker.MAX_TOTAL + 1), expected_sha256=hashlib.sha256(b"x" * (broker.MAX_TOTAL + 1)).hexdigest(),
                                profile="static-v1", recipe="metadata", image_id=IMAGE, cache_dir=tmp_path)
    with pytest.raises(broker.InspectionError, match="empty artifact"):
        broker.inspect_artifact(b"arbitrary", expected_sha256=hashlib.sha256(b"arbitrary").hexdigest(),
                                profile="fixture-v1", recipe="native", image_id=IMAGE, cache_dir=tmp_path)


def test_cache_binds_artifacts_deps_image_recipe_and_rejects_corruption(tmp_path, monkeypatch):
    monkeypatch.setattr(broker, "_check_image", lambda _: "amd64")
    calls = []

    def fake_run(image_id, architecture, profile, recipe, payload, limits):
        calls.append((image_id, profile, recipe, payload))
        return {"status": "completed", "exit_code": 0, "timed_out": False,
                "truncated": False, "stdout": "untrusted help", "stderr": ""}

    monkeypatch.setattr(broker, "_run", fake_run)
    digest = hashlib.sha256(b"a").hexdigest()
    opts = dict(expected_sha256=digest, profile="static-v1", recipe="metadata", image_id=IMAGE, cache_dir=tmp_path)
    first = broker.inspect_artifact(b"a", **opts)
    assert first["cache_hit"] is False
    assert broker.inspect_artifact(b"a", **opts)["cache_hit"] is True
    assert len(calls) == 1
    broker.inspect_artifact(b"a", **{**opts, "image_id": "sha256:" + "b" * 64})
    dep = broker.Dependency("support.dll", b"dep", hashlib.sha256(b"dep").hexdigest())
    broker.inspect_artifact(b"a", **opts, dependencies=[dep])
    broker.inspect_artifact(b"a", **opts, limits=broker.Limits(wall_seconds=12))
    assert len(calls) == 4
    original_key, _ = broker._cache_key(
        [{"name": "artifact.exe", "size": 1, "sha256": digest}], IMAGE,
        "static-v1", "metadata", broker.Limits())
    cache_path = tmp_path / f"{original_key}.json"
    cache_path.write_text('{"status":"forged"}')
    broker.inspect_artifact(b"a", **opts)
    assert len(calls) == 5
    assert first["evidence"] == "untrusted_inspection_output"
    static_key, _ = broker._cache_key(
        [{"name": "artifact.exe", "size": 1, "sha256": digest}], IMAGE,
        "static-v1", "metadata", broker.Limits())
    help_key, _ = broker._cache_key(
        [{"name": "artifact.exe", "size": 1, "sha256": digest}], IMAGE,
        "certify-help-v1", "top", broker.Limits())
    assert static_key != help_key


def test_certify_profile_rejects_external_dependency_even_with_enrolled_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(broker, "_prepare", lambda *_: (
        [{"name": "artifact.exe", "size": 984064, "sha256": broker.CERTIFY_SHA256},
         {"name": "CommandLine.dll", "size": 1, "sha256": "0" * 64}], b"unused"))
    monkeypatch.setattr(broker, "_check_image", lambda *_: pytest.fail("Docker must not start"))
    with pytest.raises(broker.InspectionError, match="external dependencies"):
        broker.inspect_artifact(b"", expected_sha256=broker.CERTIFY_SHA256,
                                profile="certify-help-v1", recipe="top", image_id=IMAGE,
                                cache_dir=tmp_path,
                                dependencies=[broker.Dependency("CommandLine.dll", b"x", "0" * 64)])


def test_image_identity_and_architecture_fail_closed(monkeypatch):
    with pytest.raises(broker.InspectionError, match="immutable"):
        broker._check_image("sage-inspection-wine:latest")
    monkeypatch.setattr(broker, "_docker", lambda *_: type("Reply", (), {
        "returncode": 1, "stdout": "", "stderr": "missing"})())
    with pytest.raises(broker.InspectionError, match="unavailable"):
        broker._check_image(IMAGE)
    def docker(args, **_):
        return type("Reply", (), {"returncode": 0,
                                    "stdout": (IMAGE + " amd64\n") if args[0] == "image" else "aarch64\n",
                                    "stderr": ""})()
    monkeypatch.setattr(broker, "_docker", docker)
    assert broker._check_image(IMAGE) == "amd64"


def test_capture_discards_flood_after_bounded_memory():
    output = bytearray()
    overflow = __import__("threading").Event()
    broker._capture(io.BytesIO(b"x" * 2_000_000), 4096, output, overflow)
    assert len(output) == 4096 and overflow.is_set()


def test_runner_sets_isolation_and_bounds_both_fast_output_streams(monkeypatch):
    calls = []
    cid = "b" * 64

    def fake_docker(args, **_):
        calls.append(args)
        if args[0] == "create":
            stdout = cid + "\n"
        elif args[0] == "inspect":
            stdout = "exited 0\n"
        else:
            stdout = cid + "\n"
        return type("Reply", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    class Child:
        stdin = io.BytesIO()
        stdout = io.BytesIO(b"o" * 100000)
        stderr = io.BytesIO(b"e" * 100000)
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(broker, "_docker", fake_docker)
    monkeypatch.setattr(broker.subprocess, "Popen", lambda *_, **__: Child())
    result = broker._run(IMAGE, "amd64", "static-v1", "metadata", b"x", broker.Limits(output_bytes=4096))
    assert result["status"] == "output_limit" and result["truncated"] is True
    assert len(result["stdout"]) == len(result["stderr"]) == 4096
    assert calls[-1][:2] == ["rm", "-f"]
    create = calls[0]
    assert create[:5] == ["create", "-i", "--platform", "linux/amd64", "--network"]
    assert "none" in create and "--read-only" in create
    assert ["--cap-drop", "ALL"] == create[create.index("--cap-drop"):create.index("--cap-drop") + 2]
    assert ["--security-opt", "no-new-privileges"] == create[create.index("--security-opt"):create.index("--security-opt") + 2]
    assert "--memory" in create and "--cpus" in create and "--pids-limit" in create
    assert create[create.index("--log-driver") + 1] == "none"
    assert "--volume" not in create and "--mount" not in create


def test_attach_control_failure_stops_client_and_removes_container(monkeypatch):
    cid = "c" * 64
    calls = []

    def fake_docker(args, **_):
        calls.append(args[0])
        if args[0] == "kill":
            raise OSError("control-plane fault")
        return type("Reply", (), {"returncode": 0,
                                    "stdout": cid + "\n", "stderr": ""})()

    class BlockingReader:
        def __init__(self):
            self.released = __import__("threading").Event()
            self.closed = False

        def read(self, _):
            self.released.wait(5)
            return b""

        def close(self):
            self.closed = True
            self.released.set()

    class Child:
        stdin = io.BytesIO()
        stdout = BlockingReader()
        stderr = BlockingReader()
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15
            self.stdout.released.set()
            self.stderr.released.set()

        def wait(self, timeout=None):
            return self.returncode

    child = Child()
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(broker, "_docker", fake_docker)
    monkeypatch.setattr(broker.subprocess, "Popen", lambda *_, **__: child)
    monkeypatch.setattr(broker.time, "monotonic", lambda: next(ticks))
    result = broker._run(IMAGE, "amd64", "static-v1", "metadata", b"x", broker.Limits(wall_seconds=1))
    assert result["status"] == "launch_error"
    assert result["timed_out"] is True and result["truncated"] is False
    assert child.terminated and "rm" in calls


def test_remove_failure_returns_exact_orphan_id(monkeypatch):
    cid = "d" * 64

    def fake_docker(args, **_):
        return type("Reply", (), {"returncode": int(args[0] == "rm"),
                                    "stdout": ("exited 0\n" if args[0] == "inspect" else cid + "\n"),
                                    "stderr": ""})()

    class Child:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        returncode = 0

        def poll(self):
            return 0

    monkeypatch.setattr(broker, "_docker", fake_docker)
    monkeypatch.setattr(broker.subprocess, "Popen", lambda *_, **__: Child())
    result = broker._run(IMAGE, "amd64", "static-v1", "metadata", b"x", broker.Limits())
    assert result["status"] == "cleanup_error"
    assert result["orphan_container_id"] == cid
