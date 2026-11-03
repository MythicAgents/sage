"""Container release decisions: which references a build publishes, and how a release tag verifies the image.

Runs the real `.github/scripts/release_image.py`, and the workflow's own `run:` blocks, against a throwaway git
repository (with an `origin/main`) and a stand-in `docker` that serves fixture registry contents. Nothing here
touches a registry or the user's git configuration.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "release_image.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "container.yml"
IMAGE = "ghcr.io/mythicagents/sage"
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
registry = json.load(open(os.environ["FAKE_REGISTRY"]))
if args[:3] == ["buildx", "imagetools", "inspect"]:
    entry = registry.get(args[3])
    if entry is None:
        sys.stderr.write(f"ERROR: {args[3]}: not found\n")
        sys.exit(1)
    field = args[args.index("--format") + 1][len("{{json ."):-2]
    print(json.dumps(entry[field]))
    sys.exit(0)
if args[:3] == ["buildx", "imagetools", "create"]:
    target, source = args[args.index("--tag") + 1], args[-1]
    registry[target] = registry[source]
    json.dump(registry, open(os.environ["FAKE_REGISTRY"], "w"))
    sys.exit(0)
sys.exit(2)
'''


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(FAKE_DOCKER)
        (self.bin / "docker").chmod(0o755)
        self.registry = root / "registry.json"
        self.log = root / "docker.log"
        self.log.write_text("")
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            # Never the operator's git configuration (signing, hooks, credential helpers).
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "FAKE_REGISTRY": str(self.registry), "FAKE_DOCKER_LOG": str(self.log),
            "REGISTRY": "ghcr.io", "GITHUB_REPOSITORY": "MythicAgents/sage",
        }
        self.env.pop("GITHUB_OUTPUT", None)
        origin = root / "origin"
        self.work = root / "work"
        self.git("init", "-q", "-b", "main", str(origin), cwd=root)
        (origin / "agent_capabilities.json").write_text(json.dumps({"agent_version": "0.3.0"}))
        (origin / ".github" / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPT, origin / ".github" / "scripts" / "release_image.py")
        self.git("add", "-A", cwd=origin)
        self.git("commit", "-q", "-m", "candidate", cwd=origin)
        self.git("clone", "-q", str(origin), str(self.work), cwd=root)
        self.main_commit = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "side")
        self.git("commit", "-q", "--allow-empty", "-m", "unmerged")
        self.side_commit = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "main")
        self.write_registry({})

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.work, env=self.env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def tag(self, name: str, commit: str, message: str | None) -> None:
        if message is None:
            self.git("tag", name, commit)
        else:
            self.git("tag", "-a", name, commit, "-m", message)

    def write_registry(self, contents: dict) -> None:
        self.registry.write_text(json.dumps(contents))

    def publish(self, reference: str, digest: str, *, revision: str, version: str = "0.3.0",
                platforms: tuple[str, ...] = ("linux/amd64", "linux/arm64")) -> None:
        manifest = {"digest": digest, "manifests": [
            *({"digest": "sha256:" + str(i) * 64, "platform": {"os": p.split("/")[0], "architecture": p.split("/")[1]}}
              for i, p in enumerate(platforms, start=1)),
            {"digest": "sha256:" + "9" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
        ]}
        labels = {"org.opencontainers.image.revision": revision, "org.opencontainers.image.version": version}
        entry = {"Manifest": manifest, "Image": {p: {"config": {"Labels": labels}} for p in platforms}}
        contents = json.loads(self.registry.read_text())
        contents[reference] = entry
        contents[f"{IMAGE}@{digest}"] = entry
        self.write_registry(contents)

    def script(self, *args: str, output: Path | None = None) -> subprocess.CompletedProcess:
        env = {**self.env, **({"GITHUB_OUTPUT": str(output)} if output else {})}
        return subprocess.run([sys.executable, str(self.work / ".github/scripts/release_image.py"), *args],
                              cwd=self.work, env=env, capture_output=True, text=True)

    def docker_calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def fx(tmp_path):
    return Fixture(tmp_path)


def _outputs(path: Path) -> dict[str, str]:
    values, lines, index = {}, path.read_text().splitlines(), 0
    while index < len(lines):
        key, sep, value = lines[index].partition("=")
        if "<<" in key and not sep:
            name, marker = key.split("<<", 1)
            end = lines.index(marker, index + 1)
            values[name] = "\n".join(lines[index + 1:end])
            index = end + 1
            continue
        values[key] = value
        index += 1
    return values


# --- what a build publishes ---------------------------------------------------------------------------------

def _select(fx: Fixture, event: str, ref: str, tmp_path: Path) -> dict[str, str]:
    out = tmp_path / "select.out"
    out.write_text("")
    result = fx.script("select", "--event", event, "--ref", ref, "--sha", fx.main_commit, "--declared", "0.3.0",
                       "--image", "ghcr.io/MythicAgents/sage", output=out)
    assert result.returncode == 0, result.stderr
    return _outputs(out)


def test_a_push_to_main_publishes_the_declared_version_with_latest_and_sha(fx, tmp_path):
    values = _select(fx, "push", "refs/heads/main", tmp_path)

    assert values["publish"] == "true"
    assert values["tags"].splitlines() == [f"{IMAGE}:latest", f"{IMAGE}:sha-{fx.main_commit[:7]}", f"{IMAGE}:0.3.0"]


def test_main_leaves_an_already_released_version_reference_alone(fx, tmp_path):
    fx.tag("v0.3.0", fx.main_commit, f"Sage 0.3.0\n\nImage-Digest: {DIGEST}")
    values = _select(fx, "push", "refs/heads/main", tmp_path)

    assert values["publish"] == "true"
    assert f"{IMAGE}:0.3.0" not in values["tags"].splitlines()


@pytest.mark.parametrize("event, ref", [
    ("push", "refs/heads/dev"), ("pull_request", "refs/pull/7/merge"), ("workflow_dispatch", "refs/heads/main"),
    ("push", "refs/heads/feature"),
])
def test_other_builds_do_not_publish(fx, tmp_path, event, ref):
    values = _select(fx, event, ref, tmp_path)

    assert values["publish"] == "false"
    assert values["tags"] == f"{IMAGE}:sha-{fx.main_commit[:7]}"


def test_main_refuses_a_declared_version_that_is_not_a_release_version(fx):
    result = fx.script("select", "--event", "push", "--ref", "refs/heads/main", "--sha", fx.main_commit,
                       "--declared", "latest", "--image", IMAGE)
    assert result.returncode == 1 and "not a release version" in result.stderr


# --- how a release tag verifies the already-published image -------------------------------------------------

def _verify(fx: Fixture, tmp_path: Path, tag: str = "v0.3.0", declared: str = "0.3.0"):
    out = tmp_path / "verify.out"
    out.write_text("")
    result = fx.script("verify", "--tag", tag, "--declared", declared, "--image", IMAGE, output=out)
    return result, _outputs(out)


def test_a_tag_naming_the_published_digest_verifies_without_rebuilding(fx, tmp_path):
    fx.publish(f"{IMAGE}:0.3.0", DIGEST, revision=fx.main_commit)
    fx.tag("v0.3.0", fx.main_commit, f"Sage 0.3.0\n\nImage-Digest: {DIGEST}")
    result, values = _verify(fx, tmp_path)

    assert result.returncode == 0, result.stderr
    assert values == {"digest": DIGEST, "commit": fx.main_commit, "minor_alias": f"{IMAGE}:0.3"}
    # Only reads: nothing was built, pushed or re-tagged.
    assert {tuple(call[:3]) for call in fx.docker_calls()} == {("buildx", "imagetools", "inspect")}


def test_re_verifying_an_older_release_never_moves_the_minor_alias_back(fx, tmp_path):
    fx.publish(f"{IMAGE}:0.3.0", DIGEST, revision=fx.main_commit)
    fx.tag("v0.3.0", fx.main_commit, f"Sage 0.3.0\n\nImage-Digest: {DIGEST}")
    fx.tag("v0.3.1", fx.main_commit, f"Sage 0.3.1\n\nImage-Digest: {OTHER}")
    fx.tag("v0.3.2-rc.1", fx.main_commit, "rc")  # a newer pre-release does not count
    result, values = _verify(fx, tmp_path)

    assert result.returncode == 0, result.stderr
    assert values["minor_alias"] == ""


def test_a_pre_release_tag_gets_no_minor_alias(fx, tmp_path):
    fx.publish(f"{IMAGE}:0.3.0-rc.1", DIGEST, revision=fx.main_commit, version="0.3.0-rc.1")
    fx.tag("v0.3.0-rc.1", fx.main_commit, f"rc\n\nImage-Digest: {DIGEST}")
    result, values = _verify(fx, tmp_path, "v0.3.0-rc.1", "0.3.0-rc.1")

    assert result.returncode == 0, result.stderr
    assert values["minor_alias"] == ""


@pytest.mark.parametrize("case, error", [
    ("wrong-version", "!= agent_capabilities.json agent_version"),
    ("not-on-main", "not on main"),
    ("image-from-another-commit", "image revision"),
    ("image-labelled-another-version", "image version"),
    ("missing-image", "is not available"),
    ("digest-mismatch", "not the qualified"),
    ("missing-architecture", "lacks linux/arm64"),
    ("no-digest-line", "exactly one 'Image-Digest"),
    ("two-digest-lines", "exactly one 'Image-Digest"),
    ("lightweight-tag", "lightweight tag"),
])
def test_a_release_tag_fails_without_changing_any_reference(fx, tmp_path, case, error):
    tag, declared, commit, message = "v0.3.0", "0.3.0", fx.main_commit, f"Sage 0.3.0\n\nImage-Digest: {DIGEST}"
    published = {"digest": DIGEST, "revision": fx.main_commit}
    if case == "wrong-version":
        declared = "0.3.1"
    elif case == "not-on-main":
        commit = published["revision"] = fx.side_commit
    elif case == "image-from-another-commit":
        published["revision"] = fx.side_commit
    elif case == "image-labelled-another-version":
        published["version"] = "latest"
    elif case == "digest-mismatch":
        published["digest"] = OTHER
    elif case == "missing-architecture":
        published["platforms"] = ("linux/amd64",)
    elif case == "no-digest-line":
        message = "Sage 0.3.0"
    elif case == "two-digest-lines":
        message += f"\nImage-Digest: {OTHER}"
    elif case == "lightweight-tag":
        message = None
    if case != "missing-image":
        fx.publish(f"{IMAGE}:0.3.0", **published)
    fx.tag(tag, commit, message)
    before = fx.registry.read_text()
    result, values = _verify(fx, tmp_path, tag, declared)

    assert result.returncode == 1
    assert error in result.stderr
    assert values == {}  # no digest or alias is handed to the alias step
    assert fx.registry.read_text() == before
    assert all(call[:3] == ["buildx", "imagetools", "inspect"] for call in fx.docker_calls())


# --- the workflow's own commands ----------------------------------------------------------------------------

def _steps(job: str) -> dict[str, dict]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return {step.get("id") or step["name"]: step for step in workflow["jobs"][job]["steps"]}


def _run_step(fx: Fixture, step: dict, tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, dict]:
    out = tmp_path / "step.out"
    out.write_text("")
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=fx.work, capture_output=True, text=True,
                            env={**fx.env, **{k: str(v) for k, v in (step.get("env") or {}).items()}, **env,
                                 "GITHUB_OUTPUT": str(out)})
    return result, _outputs(out)


def test_the_workflow_is_valid_and_its_jobs_split_on_tags():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    jobs = workflow["jobs"]
    assert jobs["image"]["if"] == "github.ref_type != 'tag'"
    assert jobs["verify-release"]["if"] == "github.event_name == 'push' && github.ref_type == 'tag'"
    # The tag job never builds: no build-push step and no QEMU.
    uses = [step.get("uses", "") for step in jobs["verify-release"]["steps"]]
    assert not any(u.startswith(("docker/build-push-action", "docker/setup-qemu-action")) for u in uses)
    for job in jobs.values():
        for step in job["steps"]:
            assert bool(step.get("uses")) != bool(step.get("run")), step
            if step.get("run"):
                check = subprocess.run(["bash", "-n", "-c", step["run"]], capture_output=True, text=True)
                assert check.returncode == 0, (step["name"], check.stderr)


def test_the_build_job_gate_publishes_the_version_from_main(fx, tmp_path):
    gate = _steps("image")["gate"]
    result, values = _run_step(fx, gate, tmp_path, GITHUB_EVENT_NAME="push", GITHUB_REF="refs/heads/main",
                               GITHUB_SHA=fx.main_commit)
    assert result.returncode == 0, result.stderr
    assert values["agent_version"] == "0.3.0" and values["publish"] == "true"
    assert f"{IMAGE}:0.3.0" in values["tags"].splitlines()  # lowercased repository

    result, values = _run_step(fx, gate, tmp_path, GITHUB_EVENT_NAME="pull_request",
                               GITHUB_REF="refs/pull/1/merge", GITHUB_SHA=fx.main_commit)
    assert result.returncode == 0 and values["publish"] == "false"


def test_the_tag_job_verifies_then_points_the_minor_alias_at_the_same_manifest(fx, tmp_path):
    fx.publish(f"{IMAGE}:0.3.0", DIGEST, revision=fx.main_commit)
    fx.tag("v0.3.0", fx.main_commit, f"Sage 0.3.0\n\nImage-Digest: {DIGEST}")
    steps = _steps("verify-release")
    result, values = _run_step(fx, steps["verify"], tmp_path, GITHUB_REF_NAME="v0.3.0")
    assert result.returncode == 0, result.stderr

    alias = steps["Point the minor alias at the verified manifest"]
    result, _ = _run_step(fx, alias, tmp_path, ALIAS=values["minor_alias"], DIGEST=values["digest"])
    assert result.returncode == 0, result.stderr
    registry = json.loads(fx.registry.read_text())
    assert registry[f"{IMAGE}:0.3"]["Manifest"]["digest"] == DIGEST
    creates = [call for call in fx.docker_calls() if call[:3] == ["buildx", "imagetools", "create"]]
    assert creates == [["buildx", "imagetools", "create", "--tag", f"{IMAGE}:0.3", f"{IMAGE}@{DIGEST}"]]


def test_the_tag_job_stops_before_the_alias_on_a_digest_mismatch(fx, tmp_path):
    fx.publish(f"{IMAGE}:0.3.0", OTHER, revision=fx.main_commit)
    fx.tag("v0.3.0", fx.main_commit, f"Sage 0.3.0\n\nImage-Digest: {DIGEST}")
    result, values = _run_step(fx, _steps("verify-release")["verify"], tmp_path, GITHUB_REF_NAME="v0.3.0")

    assert result.returncode != 0 and "not the qualified" in result.stderr
    assert "minor_alias" not in values  # the alias step's condition stays false
