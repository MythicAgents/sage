from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "ludus.py"
SPEC = importlib.util.spec_from_file_location("ludus", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_with_range_id_preserves_existing_query_and_does_not_duplicate():
    assert (
        MODULE._with_range_id("/api/v2/range/logs?tail=60", "SAGEPOLICY20260712")
        == "/api/v2/range/logs?tail=60&rangeID=SAGEPOLICY20260712"
    )
    assert (
        MODULE._with_range_id("/api/v2/range?rangeID=existing", "SAGEPOLICY20260712")
        == "/api/v2/range?rangeID=existing"
    )


def test_parser_accepts_range_id_before_or_after_command():
    assert MODULE.build_parser().parse_args(["--range-id", "before", "status"]).range_id == "before"
    assert MODULE.build_parser().parse_args(["status", "--range-id", "after"]).range_id == "after"
