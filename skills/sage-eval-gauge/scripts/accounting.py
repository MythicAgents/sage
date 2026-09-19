#!/usr/bin/env python3
"""Opt-in eval accounting: reconcile/verify/check-budget offline; collect reads LiteLLM."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "Payload_Type" / "sage"))
from ai.hillclimb.accounting_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
