"""CLI for Sage trajectory corpus and replay tooling."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .corpus import build_manifest
from .exporter import export_transitions
from .replay import replay_score
from .schema import load_jsonl, write_jsonl


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m Payload_Type.sage.ai.trajectory")
    sub = parser.add_subparsers(dest="command", required=True)

    manifest = sub.add_parser("manifest", help="Build a read-only artifact manifest")
    manifest.add_argument("--corpus-root", action="append", default=[], help="Root/file to scan; repeatable")
    manifest.add_argument("--output", required=True, help="Manifest JSON output path")
    manifest.add_argument("--include-hash", action="store_true", help="Hash artifacts while scanning")

    export = sub.add_parser("export", help="Export normalized transition JSONL records")
    export.add_argument("--corpus-root", action="append", default=[], help="Root/file to scan; repeatable")
    export.add_argument("--output", required=True, help="Transition JSONL output path")
    export.add_argument("--append", action="store_true", help="Append to the output JSONL")

    replay = sub.add_parser("replay", help="Score repair-policy replay accuracy")
    replay.add_argument("--train", required=True, help="Training transition JSONL")
    replay.add_argument("--eval", required=True, help="Evaluation transition JSONL")
    replay.add_argument("--output", help="Optional JSON output path")

    args = parser.parse_args()
    if args.command == "manifest":
        roots = args.corpus_root or ["."]
        artifacts = build_manifest(roots, include_hash=args.include_hash)
        payload = [artifact.__dict__ for artifact in artifacts]
        Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"manifest artifacts={len(payload)} output={args.output}")
        return 0
    if args.command == "export":
        roots = args.corpus_root or ["."]
        records = export_transitions(roots)
        write_jsonl(args.output, records, append=args.append)
        print(f"transition records={len(records)} output={args.output}")
        return 0
    if args.command == "replay":
        result = replay_score(load_jsonl(args.train), load_jsonl(args.eval))
        payload = {
            "total": result.total,
            "exact_repair_matches": result.exact_repair_matches,
            "label_matches": result.label_matches,
            "exact_repair_rate": result.exact_repair_rate,
            "label_match_rate": result.label_match_rate,
        }
        if args.output:
            Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
