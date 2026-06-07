"""Per-turn engagement-state injection tests (Bug 1 fix).

Verify the per-turn fix for the mis-wired continue-loop-only injection:

- Model._render_engagement_state_for_injection() is a cheap, synchronous, in-memory render that
  gates correctly (None unless autonomous + gate-on + observed state present) and emits the
  `=== ENGAGEMENT STATE` block otherwise.
- _EngagementStateMiddleware._augment appends the rendered block as a HumanMessage via
  request.override (EPHEMERAL — the original request is untouched, so it never accumulates).
- awrap_model_call passes the augmented request to the handler.

Mirrors test_autonomous_state_nudge.py: Model.__new__ + stub client, asyncio.run for async.
"""

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import engagement_state  # noqa: E402


def _load_model_module():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        return importlib.import_module("ai.langgraph.model")
    except Exception as e:  # pragma: no cover - environment guard
        pytest.skip(f"model.py runtime unavailable: {e}")


def _set_gate(monkeypatch, enabled: bool):
    """Set ENGAGEMENT_GATE_ENABLED on every loaded mythic_tools module object.

    model.py resolves mythic_tools via a package-qualified import (ai.langgraph.mythic_tools),
    which is a distinct module object from a bare top-level `import mythic_tools`. Set both so the
    render helper sees the intended value regardless of which object it bound."""
    import mythic_tools as _top  # noqa
    for mod in (_top, sys.modules.get("ai.langgraph.mythic_tools")):
        if mod is not None:
            monkeypatch.setattr(mod, "ENGAGEMENT_GATE_ENABLED", enabled)


class _StubClient:
    def __init__(self, hops=None, footholds=None):
        self._engagement_hops = list(hops or [])
        self._engagement_footholds = list(footholds or [])

    def _engagement_objective(self):
        return "sage-engagement:test"


def _achieved_hop():
    state = engagement_state.record_hop_result(
        engagement_state.EngagementState(objective="t"),
        "gpo-abuse",
        "winterfell.north.sevenkingdoms.local",
        "achieved",
        {"source": "test", "scheduled_task_present": True},
        "2026-06-07T00:00:00Z",
    )
    return state.hops


# ---------------------------------------------------------------------------
# 1. render helper gating
# ---------------------------------------------------------------------------


def test_render_none_when_not_autonomous(monkeypatch):
    mod = _load_model_module()
    _set_gate(monkeypatch, True)
    m = mod.Model.__new__(mod.Model)
    m._autonomous_solve = False
    m.mythic_client = _StubClient(hops=_achieved_hop())
    assert m._render_engagement_state_for_injection() is None


def test_render_none_when_gate_off(monkeypatch):
    mod = _load_model_module()
    _set_gate(monkeypatch, False)
    m = mod.Model.__new__(mod.Model)
    m._autonomous_solve = True
    m.mythic_client = _StubClient(hops=_achieved_hop())
    assert m._render_engagement_state_for_injection() is None


def test_render_none_when_no_observed_state(monkeypatch):
    mod = _load_model_module()
    _set_gate(monkeypatch, True)
    m = mod.Model.__new__(mod.Model)
    m._autonomous_solve = True
    m.mythic_client = _StubClient(hops=[], footholds=[])
    assert m._render_engagement_state_for_injection() is None


def test_render_present_when_autonomous_and_gate_on(monkeypatch):
    mod = _load_model_module()
    _set_gate(monkeypatch, True)
    m = mod.Model.__new__(mod.Model)
    m._autonomous_solve = True
    m.mythic_client = _StubClient(hops=_achieved_hop())
    out = m._render_engagement_state_for_injection()
    assert out is not None
    assert "=== ENGAGEMENT STATE" in out
    assert "gpo-abuse" in out


# ---------------------------------------------------------------------------
# 2. middleware augmentation (ephemeral)
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, messages):
        self.messages = list(messages)

    def override(self, **kw):
        return _FakeRequest(kw.get("messages", self.messages))


class _StubModelForMw:
    def __init__(self, rendered):
        self._rendered = rendered

    def _render_engagement_state_for_injection(self):
        return self._rendered


def test_middleware_appends_block_ephemerally():
    mod = _load_model_module()
    mw = mod._EngagementStateMiddleware(
        _StubModelForMw("=== ENGAGEMENT STATE ===\nAchieved hops:\n- gpo-abuse")
    )
    orig = [HumanMessage(content="hello")]
    req = _FakeRequest(orig)
    new_req = mw._augment(req)
    # Original request is untouched — injection is per-call, not accumulated.
    assert len(req.messages) == 1
    # New request carries the block as the most-recent (most salient) message.
    assert len(new_req.messages) == 2
    assert "=== ENGAGEMENT STATE" in new_req.messages[-1].content


def test_middleware_noop_when_render_none():
    mod = _load_model_module()
    mw = mod._EngagementStateMiddleware(_StubModelForMw(None))
    req = _FakeRequest([HumanMessage(content="hi")])
    assert mw._augment(req) is req  # same object, unchanged


def test_awrap_model_call_passes_augmented_request():
    mod = _load_model_module()
    mw = mod._EngagementStateMiddleware(
        _StubModelForMw("=== ENGAGEMENT STATE ===\n- x")
    )
    seen = {}

    async def handler(request):
        seen["messages"] = request.messages
        return "RESP"

    res = asyncio.run(mw.awrap_model_call(_FakeRequest([HumanMessage(content="go")]), handler))
    assert res == "RESP"
    assert "=== ENGAGEMENT STATE" in seen["messages"][-1].content
