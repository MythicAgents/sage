"""A forge's parent replication grant and ExtraSIDs check use the domain the ticket is issued in.

A `forge-golden-ticket` request can name its domains two ways: the requested target in `domain` with the forge
(child) domain in `source_domain`, or the forge domain in `domain` with the target in `target_domain`. The forge
plan, the krbtgt key binding and the domain-SID lookup already resolve the issuing domain the same way for both.
The post-import parent replication grant and the ExtraSIDs source check must agree with them: both spellings issue
the same tasks, grant the parent after a successful ticket import (so the engagement precheck lets the parent
DCSync through), and refuse caller-supplied ExtraSIDs that no directory source backs.

The grant stays same-forest only (the parent must be a DNS suffix of the child): a failed import, a cross-forest
target, a same-domain forge and an `ensure-kerberos-context` composition grant nothing.

The requested target resolves the same way for every spelling, including a target given only in the action itself
or alongside an explicit `domain=<child>` action target. The input augment stores it, so its parent-target inference
fills a target only when the request names none, and the approval card, every executor pass, the plan and the
grant all read the target the request named.

Fixtures run the production executor, grounding, augment, binder, builder, adapter and engagement precheck; only
Mythic's task API seam is inert, and every domain, SID and key is synthetic.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import access_reconciler  # noqa: E402
import capabilities  # noqa: E402
import engagement_state  # noqa: E402
from tests.esl_fixtures import seed_ledger  # noqa: E402
from test_engagement_wiring import _split_issue  # noqa: E402
from test_verified_capability_input_continuity import (  # noqa: E402
    DCSYNC_OK, KRBTGT_KEY, _ground_forge, _krbtgt_tools, _rubeus_issues,
)

CHILD, PARENT, OTHER = "north.lab.local", "lab.local", "essos.local"
CHILD_SID, PARENT_SID, OTHER_SID = "S-1-5-21-1-2-3", "S-1-5-21-9-9-9", "S-1-5-21-7-7-7"
CHILD_KEY = "c" * 64
FORGE = {"capability": "forge-golden-ticket", "callback_id": "13"}

# The requested target in `domain`, the forge domain in `source_domain`.
SHAPE_A = {"domain": PARENT, "source_domain": CHILD}
# The forge domain in `domain`, the requested target in `target_domain`.
SHAPE_B = {"domain": CHILD, "target_domain": PARENT}
SHAPES = {"source_domain": SHAPE_A, "target_domain": SHAPE_B}
LANES = ["supervised", "auto"]
IMPORT_DENIED = "[!] Error importing ticket: access is denied"
# The ticket-import steps (current session, or a separate logon context); each produces `kerberos_ticket_imported`.
IMPORTS = {"ticket_cache_add", "ticket_store_add"}


def _tools(monkeypatch, tmp_path, *, directory_sids: bool = True):
    """A client with the child krbtgt proved by the production DCSync executor (task 24)."""
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    sids = {CHILD: CHILD_SID, PARENT: PARENT_SID, OTHER: OTHER_SID}
    mt._resolve_domain_sid = lambda domain: asyncio.sleep(0, result=sids.get(str(domain).casefold(), ""))
    output = DCSYNC_OK.replace("'lab.local'", f"'{CHILD}'").replace(KRBTGT_KEY, CHILD_KEY)
    store["task_outputs"][24] = output
    with _split_issue(lambda: output, {}, display_id=lambda: 24):
        proved = json.loads(asyncio.run(mt.execute_capability(
            {"capability": "dcsync-krbtgt", "domain": CHILD, "callback_id": "13"}, {"timeout": 5})))
    assert f"krbtgt-hash:{CHILD}" in proved.get("recorded_effects", []), proved
    if not directory_sids:
        mt._resolve_domain_sid = lambda domain: asyncio.sleep(0, result="")
    return mt


def _execute(mt, request: dict, *, lane: str, action: dict | None = None, import_output: str = "ok"):
    """Run one capability through the executor. Supervised grounds the card first and executes what it shows."""
    action = dict(action or FORGE)
    inputs = dict(request)
    if lane == "supervised":
        [call] = _ground_forge(mt, inputs, action).tool_calls
        action, inputs = call["args"]["action"], call["args"]["inputs"]
    calls: dict = {}

    def output():
        issued = calls.get("issued") or [{}]
        return import_output if issued[-1].get("command_name") in IMPORTS else "ok"

    with _split_issue(output, calls, display_id=lambda: 30 + len(calls.get("issued", []))):
        result = json.loads(asyncio.run(mt.execute_capability(action, {**inputs, "timeout": 5})))
    return result, calls


def _issued(calls: dict) -> list[tuple[str, str]]:
    return [(item["command_name"], json.dumps(item["parameters"], sort_keys=True)) for item in calls.get("issued", [])]


def _imported(calls: dict) -> bool:
    return any(item["command_name"] in IMPORTS for item in calls.get("issued", []))


def _per_spelling(monkeypatch, tmp_path, run) -> dict:
    """Run `run(shape, tmp)` once per spelling, each on its own client, engagement ledger and state directory."""
    outcomes = {}
    for name, shape in SHAPES.items():
        spelling_tmp = tmp_path / name
        spelling_tmp.mkdir()
        monkeypatch.setenv("SAGE_DATABASE_PATH", str(spelling_tmp / "sage.db"))
        outcomes[name] = run(dict(shape), spelling_tmp)
    return outcomes


def _parent_dcsync_blocked(mt, monkeypatch, domain: str = PARENT) -> bool:
    """Ask the real engagement precheck whether a DCSync against `domain` is blocked for missing rights."""
    mt.client = object()

    async def _none(*_args, **_kwargs):
        return None

    async def _empty(*_args, **_kwargs):
        return []

    mt._ensure_engagement_key = _none
    mt._corroboration_facts = _empty
    mt._refresh_graph_facts_if_stale = _none
    monkeypatch.setattr(access_reconciler, "reconcile_access", _empty)
    # A populated graph, so the absence of the replication right is evidence and the precheck may block.
    seed_ledger(mt, graph_facts=[engagement_state.GraphFact(
        "generic-write:gpo:unrelated-policy", "bloodhound:cypher", "2026-06-12T00:00:00Z", 600)])
    mt._engagement_footholds = []
    mt._dcsync_precheck_blocks = {}
    params = {"domain": domain, "user": f"{domain.split('.')[0].upper()}\\krbtgt", "dc": f"dc01.{domain}"}
    return asyncio.run(mt._engagement_issue_hook("dcsync", params, 2)) is not None


def _forge_parent(monkeypatch, tmp_path, request: dict, *, lane: str, import_output: str = "ok"):
    mt = _tools(monkeypatch, tmp_path)
    result, calls = _execute(mt, request, lane=lane, import_output=import_output)
    grants = set(mt._cross_domain_replication_rights)
    blocked = _parent_dcsync_blocked(mt, monkeypatch)
    return result, calls, grants, blocked


@pytest.mark.parametrize("lane", LANES)
def test_both_spellings_forge_the_same_ticket_and_grant_the_parent(monkeypatch, tmp_path, lane):
    extra = {"extra_sids": [f"{PARENT_SID}-519"]}
    outcomes = _per_spelling(monkeypatch, tmp_path, lambda shape, tmp: _forge_parent(
        monkeypatch, tmp, {**shape, **extra}, lane=lane))

    for name, (result, calls, grants, blocked) in outcomes.items():
        forge = _rubeus_issues(calls)
        assert len(forge) == 1, (name, result)
        assert f"/domain:{CHILD}" in forge[0] and f"/sids:{PARENT_SID}-519" in forge[0], (name, forge)
        assert _imported(calls), (name, _issued(calls))
        assert grants == {PARENT}, (name, grants, result)
        assert blocked is False, name
    assert _issued(outcomes["source_domain"][1]) == _issued(outcomes["target_domain"][1])


@pytest.mark.parametrize("shape", list(SHAPES))
def test_a_failed_ticket_import_grants_nothing(monkeypatch, tmp_path, shape):
    request = {**SHAPES[shape], "extra_sids": [f"{PARENT_SID}-519"]}
    result, calls, grants, blocked = _forge_parent(monkeypatch, tmp_path, request, lane="auto",
                                                   import_output=IMPORT_DENIED)
    assert _imported(calls), result
    assert grants == set(), result
    assert blocked is True


@pytest.mark.parametrize("request_", [
    {"domain": OTHER, "source_domain": CHILD},
    {"domain": CHILD, "target_domain": OTHER},
], ids=list(SHAPES))
def test_a_cross_forest_forge_grants_nothing(monkeypatch, tmp_path, request_):
    mt = _tools(monkeypatch, tmp_path)
    result, calls = _execute(mt, {**request_, "extra_sids": [f"{OTHER_SID}-519"]}, lane="auto")
    assert mt._cross_domain_replication_rights == set(), result
    assert _parent_dcsync_blocked(mt, monkeypatch, OTHER) is True


@pytest.mark.parametrize("request_", [
    {"domain": CHILD, "source_domain": CHILD},
    {"domain": CHILD},
], ids=["source_domain", "domain-only"])
def test_a_same_domain_forge_grants_nothing(monkeypatch, tmp_path, request_):
    mt = _tools(monkeypatch, tmp_path)
    result, calls = _execute(mt, request_, lane="auto")
    forge = _rubeus_issues(calls)
    assert len(forge) == 1 and f"/domain:{CHILD}" in forge[0], result
    assert _imported(calls), result
    assert mt._cross_domain_replication_rights == set(), result


def test_an_ensure_kerberos_context_composition_grants_nothing(monkeypatch, tmp_path):
    mt = _tools(monkeypatch, tmp_path)
    action = {"capability": "ensure-kerberos-context", "callback_id": "13"}
    result, calls = _execute(mt, dict(SHAPE_A), lane="auto", action=action)
    forge = _rubeus_issues(calls)
    assert len(forge) == 1 and f"/domain:{CHILD}" in forge[0], result
    assert _imported(calls), result
    assert mt._cross_domain_replication_rights == set(), result


def test_without_a_directory_sid_both_spellings_refuse_caller_extra_sids(monkeypatch, tmp_path):
    def refuse(shape, tmp):
        mt = _tools(monkeypatch, tmp, directory_sids=False)
        request = {**shape, "domain_sid": CHILD_SID, "extra_sids": [f"{PARENT_SID}-519"]}
        result, calls = _execute(mt, request, lane="auto")
        return result, calls, set(mt._cross_domain_replication_rights)

    outcomes = _per_spelling(monkeypatch, tmp_path, refuse)
    for name, (result, calls, grants) in outcomes.items():
        assert result.get("verdict") == "failed", (name, result)
        assert "missing_extra_sids_source" in result.get("missing", []), (name, result)
        # The build refuses before the forge: only the current-context preflight probes ran.
        assert _rubeus_issues(calls) == [] and not _imported(calls), (name, _issued(calls))
        assert grants == set(), name
    assert _issued(outcomes["source_domain"][1]) == _issued(outcomes["target_domain"][1])


# --- Every spelling of the requested target behaves like `target_domain` in the inputs ---------------------------

TARGET_SIDS = {PARENT: PARENT_SID, OTHER: OTHER_SID}
FORESTS = {"same-forest": PARENT, "cross-forest": OTHER}


def _spellings(target: str) -> dict[str, tuple[dict, dict]]:
    """(action, inputs) for each way a request can name the forge domain and the requested target."""
    return {
        "B": (FORGE, {"domain": CHILD, "target_domain": target}),
        "A": (FORGE, {"domain": target, "source_domain": CHILD}),
        # The forge domain as the action target, the requested target only in the action's own fields.
        "C": ({**FORGE, "target": f"domain={CHILD}", "domain": CHILD, "target_domain": target}, {}),
        # Spelling A with an explicit forge-domain action target.
        "A-prime": ({**FORGE, "target": f"domain={CHILD}"}, {"domain": target, "source_domain": CHILD}),
    }


def _card_operations(mt, args: dict) -> list[str]:
    """The step operations the approval card's plan preview lists for these grounded arguments."""
    from test_step_bounds import model_module

    text = model_module.Model._capability_card_plan_text(SimpleNamespace(mythic_client=mt), args)
    assert "Planned inner tasks" in text, text
    return re.findall(r"^\s+\d+\. (\S+) — ", text, flags=re.MULTILINE)


def _case(monkeypatch, tmp_path, action: dict, request: dict, *, lane: str, precheck_domain: str):
    """Run one request through its lane on its own client, engagement ledger and state directory."""
    tmp_path.mkdir()
    monkeypatch.setenv("SAGE_DATABASE_PATH", str(tmp_path / "sage.db"))
    mt = _tools(monkeypatch, tmp_path)
    action, inputs, grounded, card = dict(action), dict(request), None, None
    if lane == "supervised":
        [call] = _ground_forge(mt, inputs, action).tool_calls
        action, inputs = call["args"]["action"], call["args"]["inputs"]
        grounded, card = dict(inputs), _card_operations(mt, call["args"])
    # The executor runs exactly the grounded call (Supervised after Accept), or the request itself (Auto).
    result, calls = _execute(mt, inputs, lane="auto", action=action)
    return SimpleNamespace(
        result=result, calls=calls, forge=_rubeus_issues(calls), grounded=grounded, card=card,
        grants=set(mt._cross_domain_replication_rights),
        blocked=_parent_dcsync_blocked(mt, monkeypatch, precheck_domain),
    )


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("caller_sids", [False, True], ids=["directory-sids", "caller-extra-sids"])
@pytest.mark.parametrize("forest", list(FORESTS))
def test_every_spelling_forges_and_grants_like_the_target_domain_spelling(monkeypatch, tmp_path, forest,
                                                                          caller_sids, lane):
    target = FORESTS[forest]
    target_sid = f"{TARGET_SIDS[target]}-519"
    extra = {"extra_sids": [target_sid]} if caller_sids else {}
    outcomes = {
        name: _case(monkeypatch, tmp_path / name, action, {**request, **extra}, lane=lane, precheck_domain=target)
        for name, (action, request) in _spellings(target).items()
    }

    control = outcomes["B"]
    for name, case in outcomes.items():
        assert len(case.forge) == 1, (name, case.result)
        assert f"/domain:{CHILD}" in case.forge[0] and f"/sids:{target_sid}" in case.forge[0], (name, case.forge)
        assert _issued(case.calls) == _issued(control.calls), (name, _issued(case.calls))
        if lane == "supervised":
            assert case.grounded.get("target_domain") == target, (name, case.grounded)
            assert case.card == control.card, (name, case.card, control.card)
        if forest == "same-forest":
            assert case.grants == {PARENT}, (name, case.grants, case.result)
            assert case.blocked is False, name
        else:
            assert case.grants == set(), (name, case.grants, case.result)
            assert case.blocked is True, name


@pytest.mark.parametrize("lane", LANES)
def test_a_request_naming_no_target_infers_the_parent_only_from_caller_extra_sids(monkeypatch, tmp_path, lane):
    run = lambda name, request: _case(monkeypatch, tmp_path / name, FORGE, request, lane=lane,  # noqa: E731
                                      precheck_domain=PARENT)
    control = run("B", {"domain": CHILD, "target_domain": PARENT})
    inferred = run("inferred", {"domain": CHILD, "extra_sids": [f"{PARENT_SID}-519"]})
    bare = run("bare", {"domain": CHILD})

    assert len(inferred.forge) == 1 and f"/sids:{PARENT_SID}-519" in inferred.forge[0], inferred.result
    assert _issued(inferred.calls) == _issued(control.calls)
    assert inferred.grants == {PARENT}
    assert len(bare.forge) == 1 and "/sids:" not in bare.forge[0], bare.result
    assert bare.grants == set()
    if lane == "supervised":
        assert inferred.grounded.get("target_domain") == PARENT and inferred.card == control.card
        assert "target_domain" not in bare.grounded and bare.card != control.card


def test_the_build_tool_plans_an_action_only_target_like_the_target_domain_spelling(monkeypatch, tmp_path):
    def build(name, action, inputs):
        (tmp_path / name).mkdir()
        monkeypatch.setenv("SAGE_DATABASE_PATH", str(tmp_path / name / "sage.db"))
        mt = _tools(monkeypatch, tmp_path / name)
        built = json.loads(asyncio.run(mt.build_capability_commands(dict(action), dict(inputs))))
        assert built.get("ok"), built
        return [(item.get("command"), json.dumps(item.get("parameters"), sort_keys=True))
                for item in built.get("commands", [])]

    spellings = _spellings(PARENT)
    control = build("B", *spellings["B"])
    assert any(f"/sids:{PARENT_SID}-519" in parameters for _, parameters in control), control
    for name in ("A", "C", "A-prime"):
        assert build(name, *spellings[name]) == control, name


def _action(target: str = "", **intent) -> "capabilities.CapabilityAction":
    return capabilities.CapabilityAction(name="forge-golden-ticket", target=target, intent=dict(intent))


@pytest.mark.parametrize(("action", "inputs", "expected"), [
    (_action(), {"domain": CHILD, "target_domain": PARENT}, PARENT),
    (_action(), {"domain": PARENT, "source_domain": CHILD}, PARENT),
    (_action(f"domain={CHILD}", domain=CHILD, target_domain=PARENT), {}, PARENT),
    (_action(f"domain={CHILD}"), {"domain": PARENT, "source_domain": CHILD}, PARENT),
    (_action(f"domain={CHILD};target_domain={PARENT}"), {}, PARENT),
    (_action(domain=PARENT, source_domain=CHILD), {}, PARENT),
    (_action(), {"domain": CHILD, "effect_domain": OTHER}, OTHER),
    (_action(domain=CHILD, effect_domain=OTHER), {}, OTHER),
    (_action(), {"domain": "LAB.Local", "source_domain": "North.Lab.Local"}, PARENT),
    (_action(), {"domain": CHILD, "source_domain": CHILD}, ""),
    (_action(), {"domain": CHILD, "target_domain": CHILD}, ""),
    (_action(f"domain={CHILD}"), {"target_domain": CHILD}, ""),
    (_action(), {"domain": CHILD}, ""),
    (_action(f"domain={CHILD}"), {"domain": PARENT}, ""),
], ids=[
    "target_domain", "domain-with-source_domain", "action-only-target", "explicit-target-with-source_domain",
    "action-target-string", "intent-domain-with-source_domain", "effect_domain", "intent-effect_domain",
    "case-folded", "equal-source", "equal-target", "equal-action-target", "domain-alone",
    "domain-without-stated-source",
])
def test_the_forge_target_resolver(action, inputs, expected):
    assert capabilities.forge_target_domain(action, inputs) == expected
