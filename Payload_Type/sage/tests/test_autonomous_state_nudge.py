"""Unit tests for the engagement-state-aware autonomous-continue nudge.

These tests verify the additive, flag-gated, fail-open nudge injection in
``ai/langgraph/model.py``:

- ``Model._build_current_engagement_state`` builds the current EngagementState the
  same way the gate does, best-effort, returning ``None`` on any error (never raises).
- ``Model._autonomous_nudge_content`` prepends the rendered state + directive when a
  rendered block is supplied, and is a byte-for-byte no-op when it is not.

No live Mythic is required — the MythicTools instance is stubbed and the async
reconciler is monkeypatched. Async helpers are driven with ``asyncio.run`` because
this repo does not configure pytest-asyncio.
"""

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import access_reconciler  # noqa: E402
import engagement_state  # noqa: E402


def _load_model_class():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        mod = importlib.import_module("ai.langgraph.model")
    except Exception as e:  # pragma: no cover - environment guard
        pytest.skip(f"model.py runtime unavailable: {e}")
    return mod.Model


class _StubMythicClient:
    """Minimal MythicTools-like stub exposing what the helper reads."""

    def __init__(self, hops=None, objective="sage-engagement:test"):
        self._engagement_hops = list(hops or [])
        self._objective = objective

    def _engagement_objective(self) -> str:
        return self._objective


def _achieved_gpo_abuse_hop():
    """Return a single achieved gpo-abuse hop via the engagement_state API."""
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="test"),
        "gpo-abuse",
        "WINTERFELL.north.local",
        "achieved",
        {"source": "test", "scheduled_task_present": True},
        "2026-06-06T12:00:00Z",
    )
    return state.hops


_DIRECTIVE_MARKER = "do NOT re-issue"


# ---------------------------------------------------------------------------
# 1. _build_current_engagement_state returns EngagementState | None, never raises
# ---------------------------------------------------------------------------


def test_build_state_returns_engagement_state(monkeypatch):
    Model = _load_model_class()
    m = Model.__new__(Model)
    m.mythic_client = _StubMythicClient(hops=_achieved_gpo_abuse_hop())

    async def _fake_reconcile(client, now):
        return []

    monkeypatch.setattr(access_reconciler, "reconcile_access", _fake_reconcile)

    state = asyncio.run(m._build_current_engagement_state())
    # model.py resolves engagement_state via package-qualified import (ai.langgraph.*),
    # which is a distinct module object from the top-level import used by this test, so
    # cross-module isinstance() would fail spuriously. Assert by class name + shape instead.
    assert state is not None
    assert type(state).__name__ == "EngagementState"
    # The achieved hop survived into the built state.
    assert any(
        str(getattr(h, "technique", "")) == "gpo-abuse" for h in state.hops
    )


def test_build_state_returns_none_when_no_client():
    Model = _load_model_class()
    m = Model.__new__(Model)
    m.mythic_client = None
    state = asyncio.run(m._build_current_engagement_state())
    assert state is None


def test_build_state_fails_open_to_empty_footholds(monkeypatch):
    """A raising reconciler must not abort state-building — footholds fall open to []."""
    Model = _load_model_class()
    m = Model.__new__(Model)
    m.mythic_client = _StubMythicClient(hops=_achieved_gpo_abuse_hop())

    async def _boom(client, now):
        raise RuntimeError("reconcile exploded")

    monkeypatch.setattr(access_reconciler, "reconcile_access", _boom)

    state = asyncio.run(m._build_current_engagement_state())
    # Cross-module isinstance would spuriously fail (see note above) — assert by name + shape.
    assert state is not None
    assert type(state).__name__ == "EngagementState"
    assert state.footholds == []
    # Hops are still present even though foothold reconciliation failed.
    assert any(
        str(getattr(h, "technique", "")) == "gpo-abuse" for h in state.hops
    )


# ---------------------------------------------------------------------------
# 2. Flag-ON: composed nudge contains the achieved hop + the directive
# ---------------------------------------------------------------------------


def test_nudge_on_contains_achieved_hop_and_directive():
    Model = _load_model_class()
    m = Model.__new__(Model)

    state = engagement_state.EngagementState(
        objective="test",
        footholds=[],
        hops=_achieved_gpo_abuse_hop(),
    )
    rendered = engagement_state.render_engagement_state(state)

    base = "[autonomous-continue] base nudge text"
    out = m._autonomous_nudge_content(base, rendered)

    # The achieved gpo-abuse hop is rendered into the block.
    assert "Achieved hops:" in out
    assert "gpo-abuse" in out
    # The directive is present.
    assert _DIRECTIVE_MARKER in out
    # The original base nudge is preserved at the end.
    assert out.endswith(base)
    # State + directive precede the base text.
    assert out.index("gpo-abuse") < out.index(_DIRECTIVE_MARKER) < out.index(base)


def test_build_then_render_then_compose_end_to_end(monkeypatch):
    """Exercise the real path: build state -> render -> compose nudge."""
    Model = _load_model_class()
    m = Model.__new__(Model)
    m.mythic_client = _StubMythicClient(hops=_achieved_gpo_abuse_hop())

    async def _fake_reconcile(client, now):
        return []

    monkeypatch.setattr(access_reconciler, "reconcile_access", _fake_reconcile)

    state = asyncio.run(m._build_current_engagement_state())
    rendered = engagement_state.render_engagement_state(state)
    base = "[autonomous-continue] base nudge text"
    out = m._autonomous_nudge_content(base, rendered)

    assert "gpo-abuse" in out
    assert _DIRECTIVE_MARKER in out
    assert out.endswith(base)


# ---------------------------------------------------------------------------
# 3. Flag-OFF semantics: rendered_state falsy -> base nudge byte-for-byte
# ---------------------------------------------------------------------------


def test_nudge_off_is_byte_for_byte_base():
    Model = _load_model_class()
    m = Model.__new__(Model)
    base = (
        "[autonomous-continue] You ended your turn without reaching the objective and without an explicit "
        "handback. Do not stop silently."
    )
    out = m._autonomous_nudge_content(base, None)
    assert out == base
    # No state header and no directive leaked in.
    assert "ENGAGEMENT STATE" not in out
    assert _DIRECTIVE_MARKER not in out


def test_nudge_empty_string_state_is_base():
    Model = _load_model_class()
    m = Model.__new__(Model)
    base = "[autonomous-continue] base"
    assert m._autonomous_nudge_content(base, "") == base


# ---------------------------------------------------------------------------
# 4. Fail-open: render failure / reconcile raise -> plain base, no exception
# ---------------------------------------------------------------------------


def test_nudge_falls_back_to_base_when_render_unavailable():
    """A None rendered_state (simulating a render/build failure) -> plain base nudge."""
    Model = _load_model_class()
    m = Model.__new__(Model)
    base = "[autonomous-continue] base nudge"
    out = m._autonomous_nudge_content(base, None)
    assert out == base


def test_build_state_does_not_raise_on_broken_client(monkeypatch):
    """A client whose attribute access raises must yield None, not propagate."""
    Model = _load_model_class()
    m = Model.__new__(Model)

    class _BrokenClient:
        @property
        def _engagement_hops(self):
            raise RuntimeError("hops exploded")

        def _engagement_objective(self):
            return "x"

    m.mythic_client = _BrokenClient()

    async def _fake_reconcile(client, now):
        return []

    monkeypatch.setattr(access_reconciler, "reconcile_access", _fake_reconcile)

    state = asyncio.run(m._build_current_engagement_state())
    assert state is None
