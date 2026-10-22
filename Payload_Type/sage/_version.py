"""Deployed-version signal for Sage.

Sage ships as two layers that drift independently: the source is bind-mounted into the container and
read live, while the Python dependencies are baked into the image. Mythic copies the source without
usable git metadata, so there is no commit to read from a deployed tree, and ``git rev-parse`` inside
the installed service directory misleadingly resolves to the *Mythic* checkout instead.

This module gives an honest, dependency-free version signal that ``main.py`` logs at startup:

* ``agent_version`` / ``mythic_version`` -- read from the project's existing ``agent_capabilities.json``
                           (the single source of truth Mythic already uses), not a second hardcoded copy.
* ``build_commit()``    -- the commit the *image* was built from, read from the ``SAGE_GIT_COMMIT``
                           env baked by the Dockerfile. CI passes the real SHA; a plain ``mythic-cli``
                           build leaves it ``unknown``.
* ``source_fingerprint()`` -- a deterministic content hash of the *live* runtime source tree, so two
                           operators can tell whether they run byte-identical code and a stale
                           source / image mismatch is visible in the logs.

Only the standard library is used, and every helper fails soft: a version banner must never be able to
stop the service from starting.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

__all__ = [
    "__version__",
    "agent_version",
    "mythic_version",
    "build_commit",
    "source_fingerprint",
    "version_info",
    "startup_banner",
]

# The canonical version lives in the repo-root agent_capabilities.json (Mythic reads it too); this
# module does not keep a second copy. That file is NOT copied into the deployed service dir and is not
# in the Docker build context, so the running container cannot read it. Two reads cover both cases: a
# dev checkout has the file a couple levels up (found by search), and the built image carries the value
# in SAGE_AGENT_VERSION, baked from the same file by CI. The chat container registers this value with
# Mythic as its semver; tests/test_version.py guards that.
_CAPABILITIES_FILE = "agent_capabilities.json"

# Env vars the Dockerfile bakes (ARG -> ENV). Absent/empty -> "unknown".
_AGENT_VERSION_ENV = "SAGE_AGENT_VERSION"
_BUILD_COMMIT_ENV = "SAGE_GIT_COMMIT"

# Directory names skipped at any depth. Any dot-prefixed dir (.phoenix, .git, .sage_engagement*,
# .sage_history, .hillclimb, ...) is skipped too — those are runtime/VCS state, not shipped code.
_SKIP_DIR_NAMES = frozenset({"__pycache__", "node_modules"})

# Relative sub-trees that are development/eval-only and never part of the runtime surface (runtime must
# not import them; see AGENTS.md). Excluding them keeps the fingerprint to the deployed service code.
_SKIP_RELATIVE_PREFIXES = ("tests", "evals", os.path.join("ai", "hillclimb"), os.path.join("ai", "trajectory"))


def _service_root() -> Path:
    return Path(__file__).resolve().parent


def _capabilities() -> dict:
    """Parse the repo-root agent_capabilities.json by searching up from this module.

    A dev checkout has it a couple levels above the service dir; the deployed container does not ship
    it at all (hence the SAGE_AGENT_VERSION env fallback in agent_version). Returns {} if not found.
    """
    here = _service_root()
    for base in (here, *here.parents[:4]):
        candidate = base / _CAPABILITIES_FILE
        try:
            if candidate.is_file():
                return json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def agent_version() -> str:
    """Sage's release version.

    A built image carries it in SAGE_AGENT_VERSION (baked by CI from agent_capabilities.json); that
    env wins because it is what the artifact was stamped as. A dev checkout reads it straight from the
    repo-root agent_capabilities.json. ``"unknown"`` when neither is available (a plain local build).
    """
    env = os.environ.get(_AGENT_VERSION_ENV, "").strip()
    if env:
        return env
    value = str(_capabilities().get("agent_version", "")).strip()
    return value or "unknown"


def mythic_version() -> str:
    """The targeted Mythic framework version from agent_capabilities.json, or ``"unknown"``.

    Dev/test helper (the file is not in the container); not logged at runtime.
    """
    value = str(_capabilities().get("mythic_version", "")).strip()
    return value or "unknown"


# Module-level convenience so ``from _version import __version__`` works; evaluated once at import.
__version__ = agent_version()


def build_commit() -> str:
    """Return the image build commit from the environment, or ``"unknown"``."""
    value = os.environ.get(_BUILD_COMMIT_ENV, "").strip()
    return value or "unknown"


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name in _SKIP_DIR_NAMES


def source_fingerprint(root: Path | str | None = None) -> str:
    """Return a stable 12-hex fingerprint of the runtime ``*.py`` tree under ``root``.

    Deterministic across machines and installs: it hashes a sorted manifest of
    ``(posix-relative-path, sha256-of-bytes)`` for each shipped ``*.py`` file, so file mtimes,
    scan order, and absolute paths do not affect it. Volatile and dev/eval sub-trees are excluded.
    Returns ``"unknown"`` if the tree cannot be read — never raises.
    """
    try:
        base = Path(root).resolve() if root is not None else _service_root()
        entries: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            # Prune skipped directories in place so os.walk does not descend into them.
            dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
            rel_dir = os.path.relpath(dirpath, base)
            rel_dir = "" if rel_dir == "." else rel_dir
            if rel_dir and rel_dir.startswith(_SKIP_RELATIVE_PREFIXES):
                dirnames[:] = []  # do not descend further into a dev/eval sub-tree
                continue
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                rel_path = os.path.join(rel_dir, filename) if rel_dir else filename
                try:
                    data = Path(dirpath, filename).read_bytes()
                except OSError:
                    continue
                digest = hashlib.sha256(data).hexdigest()
                entries.append(f"{Path(rel_path).as_posix()}\x00{digest}")
        manifest = "\n".join(sorted(entries)).encode("utf-8")
        return hashlib.sha256(manifest).hexdigest()[:12]
    except Exception:
        return "unknown"


def _short_commit(commit: str) -> str:
    # Shorten a full 40-hex SHA for the banner; leave sentinels like "unknown" alone.
    if len(commit) >= 12 and all(c in "0123456789abcdefABCDEF" for c in commit):
        return commit[:12]
    return commit


def version_info() -> dict[str, str]:
    return {
        "build_commit": build_commit(),
        "source_fp": source_fingerprint(),
        "agent_version": agent_version(),
    }


def startup_banner() -> str:
    # Lead with the auto-derived facts that never rot (image build commit, live source fingerprint);
    # the hand-maintained agent_version follows as a best-effort label. CI's release gate keeps that
    # label honest against the tag, but commit + fingerprint are the trustworthy "which code" answer.
    info = version_info()
    return (
        f"Sage build_commit={_short_commit(info['build_commit'])} source_fp={info['source_fp']} "
        f"version={info['agent_version']}"
    )
