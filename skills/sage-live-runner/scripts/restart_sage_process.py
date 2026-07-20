#!/usr/bin/env python3
"""Delegate local Sage restarts to the canonical tmux-aware launcher."""

from __future__ import annotations

import subprocess
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_RESTART = REPO_ROOT / "skills" / "sage-goad-reset" / "scripts" / "sage_restart.sh"


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    completed = subprocess.run(
        ["/bin/bash", str(CANONICAL_RESTART), *args],
        check=False,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
