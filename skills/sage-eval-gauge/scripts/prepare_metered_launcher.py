#!/usr/bin/env python3
"""Build a temporary, source-bound launcher for one metered Python entry."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


class LauncherError(RuntimeError):
    pass


def _regular(path: Path, name: str, *, executable: bool = False) -> Path:
    if not path.is_absolute():
        raise LauncherError(f"{name} must be absolute")
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        raise LauncherError(f"{name} is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.R_OK) or (
        executable and not os.access(path, os.X_OK)
    ):
        raise LauncherError(f"{name} is invalid")
    return path


def _directory(path: Path, name: str) -> Path:
    if not path.is_absolute():
        raise LauncherError(f"{name} must be absolute")
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        raise LauncherError(f"{name} is unavailable") from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise LauncherError(f"{name} is invalid")
    return path


def _interpreter(path: Path) -> Path:
    if not path.is_absolute():
        raise LauncherError("Python interpreter must be absolute")
    try:
        metadata = path.stat()
    except OSError:
        raise LauncherError("Python interpreter is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.R_OK | os.X_OK):
        raise LauncherError("Python interpreter is invalid")
    return path


def _digest(value: str, name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise LauncherError(f"{name} is invalid")
    return value


def _build(args: argparse.Namespace) -> None:
    interpreter = _interpreter(args.python)
    source_root = _directory(args.source_root, "Sage source root")
    external_directory = _directory(args.external_directory, "external directory")
    entry = _regular(args.entry, "entry")
    if entry.parent != external_directory or entry.name != "main.py":
        raise LauncherError("entry must be the selected external directory's main.py")
    entry_sha256 = _digest(args.entry_sha256, "entry SHA-256")
    if hashlib.sha256(entry.read_bytes()).hexdigest() != entry_sha256:
        raise LauncherError("entry SHA-256 does not match")
    budget_config = _regular(args.budget_config, "budget config")
    budget_ledger = _regular(args.budget_ledger, "budget ledger")
    observation_directory = _directory(args.observation_directory, "observation directory")
    metered_entry = _regular(
        source_root / "skills" / "sage-eval-gauge" / "scripts" / "run_metered_python.py",
        "metered entry",
    )
    metered_sha256 = hashlib.sha256(metered_entry.read_bytes()).hexdigest()
    output = args.output
    if not output.is_absolute() or output.exists():
        raise LauncherError("output must be a new absolute temporary path")
    output_parent = _directory(output.parent, "output parent")
    temporary_root = Path(tempfile.gettempdir()).resolve()
    try:
        output_parent.resolve().relative_to(temporary_root)
    except ValueError:
        raise LauncherError("output must be a new absolute temporary path") from None

    command = [
        str(interpreter), str(metered_entry),
        "--source-root", str(source_root),
        "--entry", str(entry),
        "--entry-sha256", entry_sha256,
        "--budget-config", str(budget_config),
        "--budget-ledger", str(budget_ledger),
        "--episode-id", args.episode_id,
        "--observation-directory", str(observation_directory),
    ]
    for family in args.client_family or ():
        command.extend(["--client-family", family])
    expected = ["--directory", str(external_directory), "run", "main.py"]
    script = (
        "#!/usr/bin/env python3\n"
        "import hashlib,os,pathlib,sys\n"
        f"EXPECTED={json.dumps(expected, separators=(',', ':'))}\n"
        f"BINDINGS={json.dumps([[str(entry), entry_sha256], [str(metered_entry), metered_sha256]], separators=(',', ':'))}\n"
        f"COMMAND={json.dumps(command, separators=(',', ':'))}\n"
        "def fail():\n sys.stderr.write('metered launcher validation failed\\n');raise SystemExit(2)\n"
        "if sys.argv[1:]!=EXPECTED:fail()\n"
        "for value,digest in BINDINGS:\n"
        " try:\n  data=pathlib.Path(value).read_bytes()\n except OSError:fail()\n"
        " if hashlib.sha256(data).hexdigest()!=digest:fail()\n"
        "os.execv(COMMAND[0],COMMAND)\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        try:
            os.write(descriptor, script)
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise LauncherError("output could not be created") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--external-directory", required=True, type=Path)
    parser.add_argument("--entry", required=True, type=Path)
    parser.add_argument("--entry-sha256", required=True)
    parser.add_argument("--budget-config", required=True, type=Path)
    parser.add_argument("--budget-ledger", required=True, type=Path)
    parser.add_argument("--episode-id", required=True)
    parser.add_argument("--observation-directory", required=True, type=Path)
    parser.add_argument("--client-family", action="append", choices=("requests", "aiohttp"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        _build(args)
        return 0
    except LauncherError as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
