#!/usr/bin/env python3
"""Inspect an operator-provided artifact in a pinned, isolated Docker image.

This is an operator CLI, not a Sage model tool or target callback command.
Collected output is untrusted data; a zero collector exit does not prove that
the inspected tool's operation succeeded.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "Payload_Type" / "sage"))

from ai.artifact_inspection import (  # noqa: E402
    InspectionError,
    Limits,
    WINE_LIMITS,
    inspect_artifact,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--sha256", required=True, help="Expected artifact SHA-256")
    parser.add_argument("--image-id", required=True, help="Immutable Docker sha256: image ID")
    parser.add_argument("--profile", required=True, choices=("static-v1", "certify-help-v1"))
    parser.add_argument("--recipe", required=True, help="Reviewed recipe for the selected profile")
    parser.add_argument("--timeout", type=int, help="Override the profile's bounded wall timeout")
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    parser.add_argument("--cache-dir", type=Path, default=cache_root / "sage" / "artifact-inspection")
    parser.add_argument("--output", type=Path, help="Write JSON to a new file; refuse to overwrite")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.output is not None and args.output.exists():
        print("output file already exists", file=sys.stderr)
        return 2
    limits = WINE_LIMITS if args.profile == "certify-help-v1" else Limits()
    if args.timeout is not None:
        limits = replace(limits, wall_seconds=args.timeout)
    try:
        result = inspect_artifact(
            args.artifact,
            expected_sha256=args.sha256,
            profile=args.profile,
            recipe=args.recipe,
            image_id=args.image_id,
            cache_dir=args.cache_dir,
            limits=limits,
        )
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
        else:
            sys.stdout.write(encoded)
    except (InspectionError, OSError) as exc:
        print(json.dumps({"status": "unavailable", "error": str(exc)}), file=sys.stderr)
        return 2
    # A captured parser error is an observation, not a collector failure. The
    # workload's own exit code and both output streams remain in the result.
    return 0 if result["status"] in {"completed", "nonzero_exit"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
