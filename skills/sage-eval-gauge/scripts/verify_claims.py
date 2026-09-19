#!/usr/bin/env python3
"""Re-derive source-bound gauge claims offline, without environment discovery."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "Payload_Type/sage"))
from ai.hillclimb.reproducible_claims import REDUCERS, evaluator_bindings, verify_claims


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument("--inventory", action="store_true")
    args = parser.parse_args(argv)
    if args.inventory:
        print(json.dumps({"reducers": REDUCERS, "evaluator_bindings": evaluator_bindings()}, sort_keys=True))
        return 0
    if args.manifest is None:
        parser.error("a retained claim manifest is required")
    try:
        report = verify_claims(args.manifest)
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        # Input observations can contain secrets; never print their repr or the
        # lower-level exception message from a parser on an untrusted document.
        print(json.dumps({"verified": False, "error": "claim reproduction failed; check labels, source/evaluator bindings, dependencies and asserted output",
                          **({"verifier_cost": exc.verifier_cost} if hasattr(exc, "verifier_cost") else {})}))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
