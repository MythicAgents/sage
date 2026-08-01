"""Model-visible schema output stays inside the budget that actually exists (ISC-7.1, ISC-9.1, ISC-9.3).

Two different ceilings, and conflating them is the mistake this file exists to prevent.

`model.py:967` defines `_compact_tool_result_str(trigger=4000, ceiling=16000)`: 4,000 is where a
tool result gets COMPACTED, 16,000 is where it gets hard-TRUNCATED. But all three schema tools sit
in `_COMPACTION_PROTECTED_TOOLS`, so neither applies to them by the normal path — that exemption is
itself the ISC-72 fix, added after a 75,650-char payload was truncated to 16,000 and the model
started guessing parameters.

So the operative limits are:

* **ISC-7.1** — the internal, no-example projection stays under 4,000. That is a self-imposed
  discipline, and it is cheap to hold, so it is held.
* **ISC-9.3** — the model-facing projection WITH examples stays far below the 16,000 truncation
  ceiling, which is the only number that has ever caused harm here. Enforcing 4,000 on this path
  would cost the per-group worked example (the `@cred:` / `@link:` forms restored upstream in rc7)
  on two commands, to avoid a truncation that cannot occur.

Fixtures are the real upstream payloads for Apollo's two widest commands, captured 2026-08-01 from
live Mythic. Synthetic worst-cases would prove nothing about the actual command surface.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph import command_parameter_schema as schema  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "data" / "apollo_widest_command_schemas.json"

#: From `model.py:967`. Named here so a change there is visibly a change to this contract.
COMPACTION_TRIGGER = 4000
TRUNCATION_CEILING = 16000


@pytest.fixture(scope="module")
def raw_schemas() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert set(data) == {"sc", "pth"}, "fixture must carry the two widest Apollo commands"
    assert sorted(data["sc"]) == ["Create", "Delete", "Query", "Start", "Stop"]
    assert sorted(data["pth"]) == ["AES128", "AES256", "Credential", "NTLM"]
    return data


def _rendered(raw, *, include_example, fields=schema.MODEL_VISIBLE_FIELDS, drop_empty=True):
    return json.dumps(
        schema._normalize(
            raw, include_example=include_example, fields=fields, drop_empty=drop_empty
        ),
        sort_keys=True,
    )


@pytest.mark.parametrize("command", ["sc", "pth"])
def test_internal_projection_stays_under_the_compaction_trigger(raw_schemas, command):
    """ISC-7.1 — the no-example path is the tight one, and holds with room to spare."""
    size = len(
        _rendered(
            raw_schemas[command],
            include_example=False,
            fields=schema.INTERNAL_SCHEMA_FIELDS,
            drop_empty=False,
        )
    )
    assert size < COMPACTION_TRIGGER, (
        f"{command} internal projection is {size} chars, over the {COMPACTION_TRIGGER} trigger"
    )


@pytest.mark.parametrize("command", ["sc", "pth"])
def test_model_projection_with_examples_stays_far_below_the_truncation_ceiling(
    raw_schemas, command
):
    """ISC-9.3 as reworded — the ceiling that has actually caused harm is 16,000, not 4,000.

    Asserted at half the ceiling rather than at it, so this fails while there is still headroom to
    react rather than at the moment output starts being lost.
    """
    size = len(_rendered(raw_schemas[command], include_example=True))
    assert size < TRUNCATION_CEILING // 2, (
        f"{command} model projection is {size} chars; the truncation ceiling is "
        f"{TRUNCATION_CEILING} and this asserts half of it as an early-warning margin"
    )


@pytest.mark.parametrize("command", ["sc", "pth"])
def test_every_parameter_group_carries_its_example(raw_schemas, command):
    """ISC-9.1 — the example is per-group, and a group without one is the case the model has to
    guess through. `sc` has five groups, which is why it is the widest command."""
    rendered = schema._normalize(raw_schemas[command], include_example=True)
    assert rendered, "expected at least one parameter group"
    for group_name, group in rendered.items():
        assert group.get("example"), f"{command} group '{group_name}' lost its example call"


def test_examples_are_what_make_the_difference_worth_deciding():
    """Pins the trade-off the ISC-9.3 decision was made on, so a future reader sees the numbers
    rather than the conclusion. If dropping examples ever stops mattering, this goes red and the
    decision can be revisited on evidence."""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))["sc"]
    with_examples = len(_rendered(raw, include_example=True))
    without = len(_rendered(raw, include_example=False))

    assert with_examples > COMPACTION_TRIGGER, (
        "sc with examples was measured at 4,925 chars — above the 4,000 trigger, which is why "
        "enforcing 4,000 here would have cost the examples"
    )
    assert without < COMPACTION_TRIGGER, (
        "sc without examples was measured at 2,834 chars, which is why ISC-7.1 is holdable"
    )
