"""Evaluation-plane storage for independent outcome observations (formerly ``ground_truth_probes``).

Independent evaluator observations never enter Sage's runtime ledger, its events, runtime snapshots or model
context. They live in an explicitly configured evaluation-side directory, one collision-safe JSON file per run
engagement id. Historical ledger JSON bundles that carried ``ground_truth_probes`` stay readable as historical
evaluation input through :func:`read_observations`; nothing here writes into Sage.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

OBSERVATIONS_ENV = "SAGE_EVAL_OBSERVATIONS_DIR"
_SAGE_ROOT = Path(__file__).resolve().parents[2]


def observations_dir() -> Path:
    """``SAGE_EVAL_OBSERVATIONS_DIR`` when set, else ``.hillclimb/evaluator-observations`` (evaluation output)."""
    configured = os.environ.get(OBSERVATIONS_ENV, "").strip()
    return Path(configured).expanduser() if configured else _SAGE_ROOT / ".hillclimb" / "evaluator-observations"


def observation_path(engagement_id: str, directory: str | os.PathLike | None = None) -> Path:
    """Collision-safe observation file for a run engagement id."""
    return _path(engagement_id, directory)


def _path(engagement_id: str, directory: str | os.PathLike | None) -> Path:
    key = str(engagement_id or "").strip()
    if not key:
        raise ValueError("an evaluator observation needs the run's engagement id")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:96]
    digest = hashlib.sha256(key.encode()).hexdigest()[:12]
    root = Path(directory) if directory is not None else observations_dir()
    return root / f"observations_{safe}.{digest}.json"


def write_observations(engagement_id: str, record: dict, directory: str | os.PathLike | None = None) -> Path:
    """Atomically write one run's independent observation record (the evaluation plane only)."""
    target = _path(engagement_id, directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"engagement_id": str(engagement_id), "ground_truth_probes": dict(record)}
    fd, temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return target


def read_observations(
    engagement_id: str,
    directory: str | os.PathLike | None = None,
    *,
    historical_bundle: str | os.PathLike | None = None,
) -> dict[str, Any] | None:
    """The run's observation record, else a historical bundle's ``ground_truth_probes``, else None."""
    try:
        data = json.loads(_path(engagement_id, directory).read_text(encoding="utf-8"))
        record = data.get("ground_truth_probes") if isinstance(data, dict) else None
        if isinstance(record, dict):
            return record
    except (OSError, ValueError):
        pass
    if historical_bundle is not None:
        try:
            data = json.loads(Path(historical_bundle).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        record = data.get("ground_truth_probes") if isinstance(data, dict) else None
        return record if isinstance(record, dict) else None
    return None


def observation_file_bytes(engagement_id: str, directory: str | os.PathLike | None = None) -> bytes:
    try:
        return _path(engagement_id, directory).read_bytes()
    except OSError:
        return b""
