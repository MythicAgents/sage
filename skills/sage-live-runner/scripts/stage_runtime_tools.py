#!/usr/bin/env python3
"""Copy and verify an explicit operator tools drop zone for an isolated Sage runtime.

Offline only: never downloads, uploads, or executes tools. Existing different
destination bytes and unexpected entries fail closed without being overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def inventory(sage_root: Path) -> dict[str, dict]:
    if not (sage_root / "ai/langgraph/ttp_library.py").is_file():
        raise ValueError(f"Not a Sage runtime root: {sage_root}")
    folder = sage_root / "tools"
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(f"Expected a real tools directory: {folder}")
    result = {}
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Unsupported tools entry: {path.name}")
        if path.name == ".keep":
            continue
        content = path.read_bytes()
        if not content:
            raise ValueError(f"Empty tool: {path.name}")
        result[path.name] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    return result


def stage(source: Path, destination: Path, *, required: list[str], apply: bool = False) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if source == destination:
        raise ValueError("Source and destination must be different runtime roots")
    expected, before = inventory(source), inventory(destination)
    for name in required:
        if not name or Path(name).name != name or name in {".", "..", ".keep"}:
            raise ValueError("Required tools must be plain filenames")
        if name not in expected:
            raise ValueError(f"Required source tool missing: {name}")
    if not expected:
        raise ValueError("Source tools inventory is empty")
    unexpected = sorted(set(before) - set(expected))
    changed = sorted(name for name in before if name in expected and before[name] != expected[name])
    if unexpected or changed:
        raise ValueError(f"Destination conflicts: unexpected={unexpected}, different={changed}")
    missing = sorted(set(expected) - set(before))
    if apply:
        for name in missing:
            content = (source / "tools" / name).read_bytes()
            if hashlib.sha256(content).hexdigest() != expected[name]["sha256"]:
                raise ValueError(f"Source changed during staging: {name}")
            with (destination / "tools" / name).open("xb") as output:
                output.write(content)
            (destination / "tools" / name).chmod(0o600)
    actual = inventory(destination)
    if inventory(source) != expected:
        raise ValueError("Source tools inventory changed during verification")
    return {"ready": actual == expected, "applied": apply,
            "source_root": str(source), "destination_root": str(destination),
            "required": required, "copied": missing if apply else [],
            "missing": sorted(set(expected) - set(actual)), "inventory": actual,
            "source_inventory": expected}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sage-root", type=Path, required=True)
    parser.add_argument("--destination-sage-root", type=Path, required=True)
    parser.add_argument("--required", action="append", default=[])
    parser.add_argument("--apply", action="store_true", help="Copy missing local files; default only checks")
    args = parser.parse_args()
    try:
        result = stage(args.source_sage_root, args.destination_sage_root,
                       required=args.required, apply=args.apply)
    except (ValueError, OSError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
