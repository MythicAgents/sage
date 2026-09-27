#!/usr/bin/env python3
"""Run one explicitly pinned Python script with Sage's opt-in passive meters."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import runpy
import stat
import sys


class MeteredEntryError(RuntimeError):
    pass


def _absolute_regular(path: Path, name: str, *, must_exist: bool = True) -> Path:
    if not path.is_absolute():
        raise MeteredEntryError(f"{name} must be absolute")
    if not must_exist and not path.exists():
        return path
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise MeteredEntryError(f"{name} is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.R_OK):
        raise MeteredEntryError(f"{name} must be a readable regular file")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--entry", required=True, type=Path)
    parser.add_argument("--entry-sha256", required=True)
    parser.add_argument("--budget-config", required=True, type=Path)
    parser.add_argument("--budget-ledger", required=True, type=Path)
    parser.add_argument("--episode-id")
    parser.add_argument("--observation-directory", type=Path)
    parser.add_argument("--client-family", action="append", choices=("requests", "aiohttp"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        source_root = args.source_root
        if not source_root.is_absolute() or not source_root.is_dir() or not os.access(source_root, os.R_OK):
            raise MeteredEntryError("source root must be an absolute readable Sage root")
        runtime_root = source_root / "Payload_Type" / "sage"
        _absolute_regular(runtime_root / "ai" / "resource_clients.py", "Sage meter module")
        entry = _absolute_regular(args.entry, "entry")
        digest = args.entry_sha256
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise MeteredEntryError("entry SHA-256 is invalid")
        if hashlib.sha256(entry.read_bytes()).hexdigest() != digest:
            raise MeteredEntryError("entry SHA-256 does not match")
        budget_config = _absolute_regular(args.budget_config, "budget config")
        budget_ledger = args.budget_ledger
        if not budget_ledger.is_absolute():
            raise MeteredEntryError("budget ledger must be absolute")
        if budget_ledger.exists():
            _absolute_regular(budget_ledger, "budget ledger")
        if args.observation_directory is not None and not args.observation_directory.is_absolute():
            raise MeteredEntryError("observation directory must be absolute")

        sys.path.insert(0, str(runtime_root))
        try:
            from ai import observation_channel, observation_process, resource_budget, resource_clients
        except ModuleNotFoundError as exc:
            dependency = exc.name if isinstance(exc.name, str) and exc.name else "unknown"
            raise MeteredEntryError(f"required dependency unavailable: {dependency}") from None
        try:
            status = resource_budget.ResourceLedger(
                budget_config, budget_ledger, episode_id=args.episode_id
            ).status()
        except resource_budget.ResourceBudgetError:
            raise MeteredEntryError("budget configuration or ledger is invalid") from None
        if args.episode_id is not None:
            episode = status["episodes"].get(args.episode_id)
            if episode is None or episode["closed_epoch"] is not None:
                raise MeteredEntryError("episode scope is unavailable")
        os.environ[resource_budget._CONFIG_ENV] = str(budget_config)
        os.environ[resource_budget._LEDGER_ENV] = str(budget_ledger)
        if args.episode_id is not None:
            os.environ[resource_budget._EPISODE_ENV] = args.episode_id
        if args.observation_directory is not None:
            os.environ[observation_channel.ENV_NAME] = str(args.observation_directory)
        try:
            resource_budget.configured_ledger()
            if args.observation_directory is not None:
                observation_process.install()
            resource_clients.install(
                clients=tuple(args.client_family or ("requests", "aiohttp")),
            )
        except resource_budget.ResourceBudgetError:
            raise MeteredEntryError("budget configuration or ledger is invalid") from None
        arguments = list(args.arguments)
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        sys.path.insert(0, str(entry.parent))
        sys.argv = [str(entry), *arguments]
        try:
            runpy.run_path(str(entry), run_name="__main__")
        except SystemExit:
            raise
        except BaseException:
            raise MeteredEntryError("metered entry failed") from None
        return 0
    except MeteredEntryError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
