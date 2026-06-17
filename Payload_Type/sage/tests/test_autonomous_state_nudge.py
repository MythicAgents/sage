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
import json
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

    def __init__(self, hops=None, objective="sage-engagement:test", footholds=None, graph_facts=None):
        self._engagement_hops = list(hops or [])
        self._engagement_footholds = list(footholds or [])
        self._engagement_graph_facts = list(graph_facts or [])
        self._objective = objective

    def _engagement_objective(self) -> str:
        return self._objective


class _ReconcilingStubMythicClient(_StubMythicClient):
    """Stub that supports access_reconciler.reconcile_access without issuing payload tasks."""

    def __init__(self, *args, callback_id="6", **kwargs):
        super().__init__(*args, **kwargs)
        self.client = object()
        self.callback_id = str(callback_id)
        self.callback_reads = 0

    async def get_all_active_callbacks(self):
        self.callback_reads += 1
        return json.dumps([{
            "id": int(self.callback_id),
            "display_id": int(self.callback_id),
            "agent": "apollo",
            "host": "CASTELBLACK",
            "user": "samwell.tarly",
            "integrity": 2,
        }])


class _GraphMustNotRun:
    async def astream(self, *args, **kwargs):
        raise AssertionError("graph.astream should not run after preflight objective completion")
        yield {}


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


def _achieved_effect(effect, task_id, technique="capability:proof", target="essos.local"):
    return engagement_state.Hop(
        id=f"{technique}:{target}:{effect}",
        technique=technique,
        target=target,
        effect=effect,
        status="achieved",
        evidence={"source": "test", "task_id": str(task_id), "provenance": "run"},
        preconditions=[],
        satisfied_effects=[effect],
        source="test",
        timestamp="2026-06-14T00:00:00Z",
    )


def _live_apollo_foothold(callback_id="6"):
    return engagement_state.Foothold(
        callback_id=str(callback_id),
        agent="apollo",
        host="castelblack",
        forest="north.sevenkingdoms.local",
        identity="NORTH\\samwell.tarly",
        integrity="medium",
        alive=True,
        source="test",
        timestamp="2026-06-14T00:00:00Z",
    )


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


# ---------------------------------------------------------------------------
# 5. Terminal objective completion: full autonomous solve stops on verified target proof
# ---------------------------------------------------------------------------


def test_objective_completion_report_streams_and_sets_handback(monkeypatch):
    Model = _load_model_class()
    package_mythic_tools = importlib.import_module("ai.langgraph.mythic_tools")
    monkeypatch.setattr(package_mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)

    m = Model.__new__(Model)
    m._autonomous_solve = True
    m._objective_completion_report_streamed = False
    m.state = {}
    m.mythic_client = _StubMythicClient(
        objective="obtain administrative control of essos.local",
        footholds=[_live_apollo_foothold("6")],
        hops=[
            _achieved_effect("da:essos.local", "229", technique="adcs-certificate-auth"),
            _achieved_effect("certificate-auth:administrator@essos.local", "229", technique="adcs-certificate-auth"),
            _achieved_effect(
                "kerberos-context:essos.local@callback:6",
                "229",
                technique="capability:ensure-kerberos-context",
                target="domain=essos.local;callback=6",
            ),
            _achieved_effect("krbtgt-hash:essos.local", "232", technique="dcsync"),
        ],
    )
    streamed = []

    async def _stream(text):
        streamed.append(text)

    m._stream_message_to_mythic = _stream

    assert asyncio.run(m._maybe_stream_objective_completion_stop()) is True
    assert m.state["recursion_handback"] is True
    assert len(streamed) == 1
    assert "Objective complete" in streamed[0]
    assert "`da:essos.local` task=229" in streamed[0]
    assert "`kerberos-context:essos.local@callback:6` task=229 cb=6" in streamed[0]
    assert "`krbtgt-hash:essos.local` task=232" in streamed[0]
    assert "Sage is stopping" in streamed[0]


def test_objective_completion_report_ignores_opaque_engagement_id(monkeypatch):
    Model = _load_model_class()
    package_mythic_tools = importlib.import_module("ai.langgraph.mythic_tools")
    monkeypatch.setattr(package_mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)

    m = Model.__new__(Model)
    m._autonomous_solve = True
    m.mythic_client = _StubMythicClient(
        objective="sage-engagement:d499206c-e493-45cf-a422-54ffa33fcece",
        footholds=[_live_apollo_foothold("6")],
        hops=[
            _achieved_effect("da:essos.local", "229", technique="adcs-certificate-auth"),
            _achieved_effect(
                "kerberos-context:essos.local@callback:6",
                "229",
                technique="capability:ensure-kerberos-context",
                target="domain=essos.local;callback=6",
            ),
        ],
    )

    assert m._objective_completion_report() is None


def test_objective_completion_preflight_refreshes_empty_foothold_cache(monkeypatch):
    Model = _load_model_class()
    package_mythic_tools = importlib.import_module("ai.langgraph.mythic_tools")
    package_access_reconciler = importlib.import_module("ai.langgraph.access_reconciler")
    monkeypatch.setattr(package_mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)

    async def _alive(client, callback_display_id):
        return {"alive": str(callback_display_id) == "6"}

    monkeypatch.setattr(package_access_reconciler, "assess_callback_liveness", _alive)

    m = Model.__new__(Model)
    m._autonomous_solve = True
    m._objective_completion_report_streamed = False
    m.state = {}
    m.mythic_client = _ReconcilingStubMythicClient(
        objective="obtain administrative control of essos.local",
        footholds=[],
        hops=[
            _achieved_effect("da:essos.local", "229", technique="adcs-certificate-auth"),
            _achieved_effect(
                "kerberos-context:essos.local@callback:6",
                "229",
                technique="capability:ensure-kerberos-context",
                target="domain=essos.local;callback=6",
            ),
            _achieved_effect("krbtgt-hash:essos.local", "232", technique="dcsync"),
        ],
    )
    streamed = []

    async def _stream(text):
        streamed.append(text)

    m._stream_message_to_mythic = _stream

    assert asyncio.run(m._maybe_stream_objective_completion_stop(refresh_footholds=True)) is True
    assert m.mythic_client.callback_reads == 1
    assert len(m.mythic_client._engagement_footholds) == 1
    assert streamed and "Objective complete" in streamed[0]


def test_invoke_preflight_stops_before_graph_astream(monkeypatch):
    Model = _load_model_class()
    package_mythic_tools = importlib.import_module("ai.langgraph.mythic_tools")
    package_access_reconciler = importlib.import_module("ai.langgraph.access_reconciler")
    monkeypatch.setattr(package_mythic_tools, "ENGAGEMENT_GATE_ENABLED", True)

    async def _alive(client, callback_display_id):
        return {"alive": str(callback_display_id) == "6"}

    monkeypatch.setattr(package_access_reconciler, "assess_callback_liveness", _alive)

    m = Model.__new__(Model)
    m.provider = "test"
    m.model = "test"
    m.mode = "auto"
    m.graph = _GraphMustNotRun()
    m.verbose = False
    m._autonomous_solve = False
    m._objective_completion_report_streamed = False
    m._message_seq = 1
    m._stop_requested = False
    m.agent_task_id = "agent-task"
    m.task_id = 272
    m.state = {
        "messages": [],
        "_message_seq": 1,
        "supervisor_messages": [],
        "generalist_messages": [],
        "mythic_operator_messages": [],
        "mythic_payload_messages": [],
        "mcp_manager_messages": [],
        "bloodhound_messages": [],
        "recursion_summary_requested": False,
        "recursion_handback": False,
    }
    m.mythic_client = _ReconcilingStubMythicClient(
        objective="obtain administrative control of essos.local",
        footholds=[],
        hops=[
            _achieved_effect("da:essos.local", "229", technique="adcs-certificate-auth"),
            _achieved_effect(
                "kerberos-context:essos.local@callback:6",
                "229",
                technique="capability:ensure-kerberos-context",
                target="domain=essos.local;callback=6",
            ),
            _achieved_effect("krbtgt-hash:essos.local", "232", technique="dcsync"),
        ],
    )
    streamed = []

    async def _stream(text):
        streamed.append(text)

    m._stream_message_to_mythic = _stream

    prompt = (
        "Continue the autonomous objective from the observed engagement state. If the objective is already "
        "satisfied, report the proof chain and stop."
    )
    result = asyncio.run(m.invoke(prompt, is_interactive=False))

    assert result == ""
    assert m.state["recursion_handback"] is True
    assert m.mythic_client.callback_reads == 1
    assert any("Continue the autonomous objective" in item for item in streamed)
    assert any("Objective complete" in item for item in streamed)


def test_objective_completion_preflight_does_not_intercept_unrelated_query():
    Model = _load_model_class()
    m = Model.__new__(Model)
    m._autonomous_solve = False

    assert m._objective_completion_preflight_allowed("list active callbacks") is False
    assert m._objective_completion_preflight_allowed("Continue the autonomous objective from the observed engagement state") is True
