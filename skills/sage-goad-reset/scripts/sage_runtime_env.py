#!/usr/bin/env python3
"""Resolve a Sage runtime setting the way the running Sage resolves it.

Order: the process environment, then `Payload_Type/sage/.env.local`, then `Payload_Type/sage/.env`.
An empty value sets nothing, wherever it appears. The file order and the empty rule are
`dotenv_bootstrap`'s — the module Sage itself loads at startup — imported here, not restated, so
operator tooling and the runtime cannot drift apart.

Why tooling needs this at all: `SAGE_BLOODHOUND_MCP_DIR` lives in the tracked `.env` for an
operator who configures Sage there, and in the gitignored `.env.local` for a developer. Tools used
to read only the process environment and then fall back to a sibling `bloodhound_mcp` checkout,
which AGENTS.md forbids: a guessed path binds BloodHound to a checkout nobody chose. Unresolved is
now a refusal that names the variable and the files searched.

One deliberate difference from the runtime: an EMPTY process-environment value counts as unset
here. `SAGE_BLOODHOUND_MCP_DIR="$SAGE_BLOODHOUND_MCP_DIR"` with the variable unexported produced
exactly that on 2026-10-31, and it silently replaced the configured directory.

  get NAME              print the value, or exit 2 naming NAME and the files searched
  mcp-run SCRIPT [ARG]  run SCRIPT inside the BloodHound MCP checkout's own environment:
                        `uv --directory <dir> run python <SCRIPT> [ARG...]`, with the resolved
                        SAGE_BLOODHOUND_MCP_DIR exported to it
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[3]
#: Where Sage's own env files live. Read at call time so tests can point it elsewhere.
RUNTIME_DIR = REPO_ROOT / "Payload_Type" / "sage"
BLOODHOUND_MCP_DIR = "SAGE_BLOODHOUND_MCP_DIR"


def _dotenv_bootstrap() -> ModuleType:
    """Load the runtime's loader by path, without putting the Sage package on sys.path.

    Callers include scripts that run inside the BloodHound MCP's own virtualenv; adding Sage's
    package root there would let its top-level modules shadow the MCP's.
    """
    cached = sys.modules.get("sage_dotenv_bootstrap")
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "sage_dotenv_bootstrap", REPO_ROOT / "Payload_Type" / "sage" / "dotenv_bootstrap.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Payload_Type/sage/dotenv_bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["sage_dotenv_bootstrap"] = module
    return module


def searched_files(runtime_dir: str | Path | None = None) -> list[Path]:
    directory = Path(runtime_dir or RUNTIME_DIR)
    return [directory / name for name in _dotenv_bootstrap().DOTENV_FILENAMES]


def resolve(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
    runtime_dir: str | Path | None = None,
) -> str | None:
    """The value Sage would see for ``name``, or None. Never mutates ``environ``."""
    loader = _dotenv_bootstrap()
    values = {key: value for key, value in (os.environ if environ is None else environ).items() if value}
    for path in loader.dotenv_paths(str(Path(runtime_dir or RUNTIME_DIR))):
        loader.apply_dotenv(loader.dotenv_values(path), values)
    return values.get(name) or None


def unresolved_message(name: str, runtime_dir: str | Path | None = None) -> str:
    files = ", then ".join(str(path) for path in searched_files(runtime_dir))
    return (
        f"{name} is not set. Searched the process environment, then {files}. "
        f"Set it in one of those files, or export it."
    )


def require(name: str, *, runtime_dir: str | Path | None = None) -> str:
    """The resolved value, or SystemExit naming the variable. For scripts that cannot continue."""
    value = resolve(name, runtime_dir=runtime_dir)
    if not value:
        raise SystemExit(f"ERR: {unresolved_message(name, runtime_dir)}")
    return value


def _cmd_get(args: argparse.Namespace) -> int:
    value = resolve(args.name, runtime_dir=args.runtime_dir)
    if not value:
        print(f"ERR: {unresolved_message(args.name, args.runtime_dir)}", file=sys.stderr)
        return 2
    print(value)
    return 0


def _cmd_mcp_run(args: argparse.Namespace) -> int:
    directory = resolve(BLOODHOUND_MCP_DIR, runtime_dir=args.runtime_dir)
    if not directory:
        print(f"ERR: {unresolved_message(BLOODHOUND_MCP_DIR, args.runtime_dir)}", file=sys.stderr)
        return 2
    mcp_dir = Path(directory).expanduser()
    if not mcp_dir.is_dir():
        print(f"ERR: {BLOODHOUND_MCP_DIR}={mcp_dir} does not exist.", file=sys.stderr)
        return 2
    # `uv --directory` switches the working directory before Python starts, so a relative script
    # path would be resolved against the MCP checkout instead of where the operator typed it.
    script = str(Path(args.script).expanduser().resolve())
    if shutil.which("uv") is None:
        print("ERR: `uv` is not on PATH; it runs the script inside the BloodHound MCP checkout.", file=sys.stderr)
        return 2
    env = {**os.environ, BLOODHOUND_MCP_DIR: str(mcp_dir)}
    command = ["uv", "--directory", str(mcp_dir), "run", "python", script, *args.args]
    os.execvpe(command[0], command, env)
    return 127  # pragma: no cover - exec does not return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--runtime-dir",
        default=None,
        help="directory holding Sage's .env.local/.env (default: Payload_Type/sage in this repo)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("get", help="print a resolved Sage runtime setting")
    get.add_argument("name")
    get.set_defaults(func=_cmd_get)
    run = sub.add_parser("mcp-run", help="run a script inside the BloodHound MCP checkout's environment")
    run.add_argument("script")
    run.add_argument("args", nargs=argparse.REMAINDER)
    run.set_defaults(func=_cmd_mcp_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
