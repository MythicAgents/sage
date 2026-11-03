"""Safe local tests for the image-level BloodHound ingest-contract checker. Nothing here runs Docker."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_bloodhound_ingest_contract.py"
SPEC = importlib.util.spec_from_file_location("check_bloodhound_ingest_contract", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class Runner:
    """Records docker invocations and answers them from a script."""

    def __init__(self, *, image_id="sha256:abc", inside=None, returncode=0, timeout=False):
        self.calls: list[list[str]] = []
        self.image_id = image_id
        self.inside = inside
        self.returncode = returncode
        self.timeout = timeout

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        if command[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(returncode=0 if self.image_id else 1, stdout=f"{self.image_id or ''}\n", stderr="")
        if command[:2] == ["docker", "run"]:
            if self.timeout:
                raise subprocess.TimeoutExpired(command, 1)
            stdout = "noise\n" + (checker.RESULT_MARKER + json.dumps(self.inside) + "\n" if self.inside else "")
            return SimpleNamespace(returncode=self.returncode, stdout=stdout, stderr="trace")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _docker(_name):
    return "/usr/bin/docker"


def test_missing_docker_is_a_blocker_not_a_pass():
    status, result = checker.run_check("candidate", runner=Runner(), which=lambda _name: None)
    assert status == 2 and result["status"] == "blocked"


def test_a_missing_image_is_a_blocker():
    runner = Runner(image_id=None)
    status, result = checker.run_check("candidate", runner=runner, which=_docker)
    assert status == 2 and result["status"] == "blocked"
    assert not any(call[:2] == ["docker", "run"] for call in runner.calls)


def test_the_container_is_isolated_and_mounts_only_this_checker_read_only():
    command = checker.docker_command("sha256:abc", "sage-ingest-check-x", SCRIPT)
    assert command[:3] == ["docker", "run", "--rm"]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--entrypoint") + 1] == "python3"
    mounts = [command[i + 1] for i, part in enumerate(command) if part in {"-v", "--volume", "--mount"}]
    assert mounts == [f"{SCRIPT.resolve()}:{checker.CONTAINER_SCRIPT}:ro"]
    assert "-e" not in command and "--env-file" not in command and "--privileged" not in command
    assert command[-4:] == ["sha256:abc", "-I", checker.CONTAINER_SCRIPT, "--inside"]


def test_the_resolved_image_id_is_what_runs_and_what_is_recorded():
    inside = {"base_sha": "92a37dd", "patch_sha256": "f" * 64, "checks": [{"name": "x", "ok": True}]}
    runner = Runner(image_id="sha256:resolved", inside=inside)
    status, result = checker.run_check("sage:candidate", runner=runner, which=_docker)
    run = next(call for call in runner.calls if call[:2] == ["docker", "run"])
    assert "sha256:resolved" in run and "sage:candidate" not in run
    assert status == 0 and result["status"] == "passed"
    assert (result["image_id"], result["base_sha"], result["patch_sha256"]) == ("sha256:resolved", "92a37dd", "f" * 64)
    assert ["docker", "rm", "-f"] == next(call for call in runner.calls if call[:2] == ["docker", "rm"])[:3]


def test_any_failed_check_fails_the_image():
    inside = {"checks": [{"name": "a", "ok": True}, {"name": "b", "ok": False}]}
    status, result = checker.run_check("candidate", runner=Runner(inside=inside), which=_docker)
    assert status == 1 and result["status"] == "failed"


def test_no_result_or_no_checks_is_a_failure():
    status, result = checker.run_check("candidate", runner=Runner(inside=None, returncode=1), which=_docker)
    assert status == 1 and "no check result" in result["reason"]
    status, _ = checker.run_check("candidate", runner=Runner(inside={"checks": []}), which=_docker)
    assert status == 1


def test_a_timeout_fails_and_removes_the_container():
    runner = Runner(timeout=True)
    status, result = checker.run_check("candidate", runner=runner, which=_docker)
    assert status == 1 and "time limit" in result["reason"]
    assert any(call[:3] == ["docker", "rm", "-f"] for call in runner.calls)


def test_the_fake_api_filters_by_exact_id_and_keeps_old_jobs_addressable():
    fake = checker.FakeBloodHound()
    status, page = fake.handle("GET", "/api/v2/file-upload?skip=0&limit=50", b"")
    assert status == 200 and 1 not in [row["id"] for row in page["data"]], "an unfiltered recent window misses job 1"
    status, exact = fake.handle("GET", "/api/v2/file-upload?id=eq%3A1&skip=0&limit=2", b"")
    assert status == 200 and [row["id"] for row in exact["data"]] == [1]
    status, empty = fake.handle("GET", "/api/v2/file-upload?id=eq%3A999&skip=0&limit=2", b"")
    assert status == 200 and empty["data"] == []
    status, _ = fake.handle("GET", f"/api/v2/file-upload?id=eq%3A{checker.AUTH_FAILURE_JOB}&limit=2", b"")
    assert status == 401


def test_the_fake_api_runs_one_upload_job_lifecycle():
    fake = checker.FakeBloodHound()
    _, started = fake.handle("POST", "/api/v2/file-upload/start", b"")
    job_id = started["data"]["id"]
    assert fake.handle("POST", f"/api/v2/file-upload/{job_id}", b"zip-bytes")[0] == 202
    assert fake.handle("POST", f"/api/v2/file-upload/{job_id}/end", b"")[0] == 200
    assert fake.jobs[job_id]["status"] == 6 and fake.jobs[job_id]["status_message"] == ""
    assert fake.upload_bytes == [len(b"zip-bytes")]


def test_the_generated_collection_is_an_inert_zip():
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(checker._inert_collection_zip())) as archive:
        names = archive.namelist()
        assert names == ["20261103000000_domains.json"]
        assert json.loads(archive.read(names[0]))["data"] == []


def test_the_command_line_requires_an_image(capsys):
    try:
        checker.main([])
    except SystemExit as exc:
        assert exc.code == 2
    assert "--image is required" in capsys.readouterr().err
