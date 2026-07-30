#!/usr/bin/env python3
"""Read-only BloodHound MCP readiness probe for Sage reset workflows."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
READINESS_CONTRACT_PATH = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "readiness_contract.py"


def _load_readiness_contract():
    spec = importlib.util.spec_from_file_location("sage_readiness_contract", READINESS_CONTRACT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load readiness contract helper from {READINESS_CONTRACT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def collect_status(directory: str | Path | None = None) -> dict[str, Any]:
    contract = _load_readiness_contract()
    return await contract.probe_bloodhound_mcp_tools(directory)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bloodhound-mcp-dir",
        default=None,
        help="Optional BloodHound MCP checkout path. Defaults to SAGE_BLOODHOUND_MCP_DIR or the workspace checkout.",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    status = await collect_status(args.bloodhound_mcp_dir)
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0 if status.get("ready") else 1


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
