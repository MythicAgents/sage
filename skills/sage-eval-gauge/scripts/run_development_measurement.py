#!/usr/bin/env python3
"""Local-only entrypoint for development measurement and source composition."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "Payload_Type" / "sage"))
from ai.hillclimb.development_measurement import main

if __name__ == "__main__":
    raise SystemExit(main())
