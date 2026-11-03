#!/usr/bin/env python3
"""Release decisions for the Sage container image, run by .github/workflows/container.yml.

The version image is published from main before the release tag exists, so the exact image can be qualified
first. The tag then verifies that already-published image and never rebuilds or overwrites it.

  select   Which references a build publishes. A push to main publishes `latest`, `sha-<short>` and the declared
           `agent_version`; once `v<agent_version>` is tagged, that version reference is left on its released
           image. Every other event builds without publishing.
  verify   For a `v*` tag: the tag version equals `agent_version`, the tagged commit is on main, the annotated tag
           carries `Image-Digest: sha256:<digest>`, the version reference resolves to exactly that digest with
           every required architecture, and each architecture's OCI revision/version labels match the tag.
  inspect  Print a reference's digest, architecture manifests and OCI revision/version labels, for the record.

Standard library only; `git` and `docker buildx imagetools` are the only commands it runs. Output lines are
`key=value` and are appended to $GITHUB_OUTPUT when it is set.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any

REQUIRED_PLATFORMS = ("linux/amd64", "linux/arm64")
VERSION_LABEL = "org.opencontainers.image.version"
REVISION_LABEL = "org.opencontainers.image.revision"
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?$")
_DIGEST_LINE_RE = re.compile(r"^Image-Digest:\s*(\S+)\s*$", re.MULTILINE)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReleaseError(Exception):
    pass


def _run(*command: str) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _git(*args: str) -> str:
    result = _run("git", *args)
    if result.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _emit(values: dict[str, str]) -> None:
    lines = []
    for key, value in values.items():
        lines.append(f"{key}<<__SAGE_EOF__\n{value}\n__SAGE_EOF__" if "\n" in value else f"{key}={value}")
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def _version(value: str) -> re.Match:
    match = _VERSION_RE.match(value or "")
    if not match:
        raise ReleaseError(f"agent_version '{value}' is not a release version (MAJOR.MINOR.PATCH[-PRERELEASE])")
    return match


def select(event: str, ref: str, sha: str, declared: str, image: str) -> dict[str, str]:
    image = image.lower()
    short = f"{image}:sha-{sha[:7]}"
    if event != "push" or ref != "refs/heads/main":
        return {"publish": "false", "tags": short}
    _version(declared)
    tags = [f"{image}:latest", short]
    released = _run("git", "rev-parse", "-q", "--verify", f"refs/tags/v{declared}").returncode == 0
    if released:
        print(f"::warning::v{declared} is already tagged; {image}:{declared} stays on its released image. "
              "Bump agent_version to publish a new version image.", file=sys.stderr)
    else:
        tags.append(f"{image}:{declared}")
    return {"publish": "true", "tags": "\n".join(tags)}


def _imagetools(reference: str, field: str) -> Any:
    result = _run("docker", "buildx", "imagetools", "inspect", reference, "--format", "{{json ." + field + "}}")
    if result.returncode != 0:
        raise ReleaseError(f"{reference} is not available: {result.stderr.strip() or 'inspect failed'}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise ReleaseError(f"{reference}: unreadable {field} ({exc})") from exc


def _repository(reference: str) -> str:
    if "@" in reference:
        return reference.split("@", 1)[0]
    name, _, tag = reference.rpartition(":")
    return name if name and "/" not in tag else reference


def inspect(reference: str) -> dict[str, Any]:
    """The digest, per-architecture manifests and OCI labels a reference resolves to."""
    manifest = _imagetools(reference, "Manifest")
    digest = str(manifest.get("digest") or "")
    if not _DIGEST_RE.match(digest):
        raise ReleaseError(f"{reference} resolved to no manifest digest")
    platforms = {}
    for entry in manifest.get("manifests") or []:
        platform = entry.get("platform") or {}
        name = f"{platform.get('os')}/{platform.get('architecture')}"
        if name in REQUIRED_PLATFORMS:
            platforms[name] = str(entry.get("digest") or "")
    # Read the labels through the digest itself, so they describe the manifest just resolved, not a later push.
    configs = _imagetools(f"{_repository(reference)}@{digest}", "Image")
    labels = {name: ((configs.get(name) or {}).get("config") or {}).get("Labels") or {} for name in platforms}
    return {"digest": digest, "platforms": platforms, "labels": labels}


def _newest_final(minor: str) -> str:
    """The highest final (not pre-release) v<minor>.* tag, so re-verifying an older tag never moves the alias back."""
    finals = []
    for name in _git("tag", "-l", f"v{minor}.*").split():
        found = _VERSION_RE.match(name[1:])
        if found and not found.group(4):
            finals.append((tuple(int(part) for part in found.group(1, 2, 3)), name))
    return max(finals)[1] if finals else ""


def verify(tag: str, declared: str, image: str, main_ref: str = "origin/main") -> dict[str, str]:
    image = image.lower()
    version = tag[1:] if tag.startswith("v") else tag
    if version != declared:
        raise ReleaseError(f"release tag '{tag}' != agent_capabilities.json agent_version '{declared}'")
    match = _version(declared)
    if _git("cat-file", "-t", f"refs/tags/{tag}") != "tag":
        raise ReleaseError(f"{tag} is a lightweight tag; create an annotated tag carrying Image-Digest")
    commit = _git("rev-parse", f"refs/tags/{tag}^{{commit}}")
    if _run("git", "merge-base", "--is-ancestor", commit, main_ref).returncode != 0:
        raise ReleaseError(f"{tag} points at {commit}, which is not on main")
    digests = _DIGEST_LINE_RE.findall(_git("for-each-ref", "--format=%(contents)", f"refs/tags/{tag}"))
    if len(digests) != 1 or not _DIGEST_RE.match(digests[0]):
        raise ReleaseError(f"{tag} must carry exactly one 'Image-Digest: sha256:<64 hex>' line; found {digests}")
    expected = digests[0]
    found = inspect(f"{image}:{version}")
    if found["digest"] != expected:
        raise ReleaseError(f"{image}:{version} resolves to {found['digest']}, not the qualified {expected}")
    missing = [name for name in REQUIRED_PLATFORMS if name not in found["platforms"]]
    if missing:
        raise ReleaseError(f"{image}@{expected} lacks {', '.join(missing)}")
    for name in REQUIRED_PLATFORMS:
        labels = found["labels"].get(name) or {}
        if labels.get(REVISION_LABEL) != commit:
            raise ReleaseError(f"{name} image revision '{labels.get(REVISION_LABEL)}' != tagged commit {commit}")
        if labels.get(VERSION_LABEL) != declared:
            raise ReleaseError(f"{name} image version '{labels.get(VERSION_LABEL)}' != {declared}")
    minor = "" if match.group(4) else f"{match.group(1)}.{match.group(2)}"
    if minor and tag != _newest_final(minor):
        print(f"::notice::{tag} is not the newest {minor} release; the :{minor} alias is left alone.", file=sys.stderr)
        minor = ""
    return {"digest": expected, "commit": commit, "minor_alias": f"{image}:{minor}" if minor else ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("select")
    s.add_argument("--event", required=True)
    s.add_argument("--ref", required=True)
    s.add_argument("--sha", required=True)
    s.add_argument("--declared", required=True)
    s.add_argument("--image", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--tag", required=True)
    v.add_argument("--declared", required=True)
    v.add_argument("--image", required=True)
    v.add_argument("--main-ref", default="origin/main")
    i = sub.add_parser("inspect")
    i.add_argument("reference")
    args = parser.parse_args(argv)
    try:
        if args.command == "select":
            _emit(select(args.event, args.ref, args.sha, args.declared, args.image))
        elif args.command == "verify":
            _emit(verify(args.tag, args.declared, args.image, args.main_ref))
        else:
            print(json.dumps(inspect(args.reference), indent=2, sort_keys=True))
    except ReleaseError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
