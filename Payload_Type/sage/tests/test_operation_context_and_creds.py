"""Operation-named state ledger + Mythic credential-store tools.

Covers:
  * operation_context: operation_key build + resolve_operation(_key) via a mocked Mythic query.
  * engagement_ledger: the new `state_` filename prefix + agent/command no-drift.
  * MythicTools: lazy operation-key resolution (_ensure_engagement_key / _eng_key) + persist key,
    and the explicit SAGE_ENGAGEMENT_ID override.
  * read_credentials / add_credential tools (mocked mythic client) + GUARDED_TOOLS gating.

No live Mythic. The `mythic` lib functions are monkeypatched. Mirrors the repo's no-pytest-asyncio
convention (asyncio.run for the few async methods).
"""

from tests.esl_fixtures import seed_ledger  # noqa: E402
import asyncio
import io
import sys
import zipfile
from pathlib import Path

import pytest

LG = Path(__file__).resolve().parents[1] / "ai" / "langgraph"
sys.path.insert(0, str(LG))
import engagement_ledger as el  # noqa: E402
import engagement_state as es  # noqa: E402
import operation_context as oc  # noqa: E402
import mythic_tools  # noqa: E402
from mythic import mythic as MM  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def _ledger_hop(technique: str, target: str, effect: str, status: str = "achieved") -> dict:
    return {
        "id": f"{technique}:{target}",
        "technique": technique,
        "target": target,
        "effect": effect,
        "status": status,
        "evidence": {"source": "test", "provenance": "run"},
        "preconditions": [],
        "satisfied_effects": [effect],
        "source": "test",
        "timestamp": "2026-06-11T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# operation_context
# ---------------------------------------------------------------------------

def test_operation_key_sanitizes_and_orders():
    assert oc.operation_key("Operation Chimera", 1) == "Operation_Chimera_1"
    assert oc.operation_key("weird/name:x", 7) == "weird_name_x_7"
    assert oc.operation_key("", 3) == "operation_3"


def test_resolve_operation_none_client():
    assert _run(oc.resolve_operation(None)) is None
    assert _run(oc.resolve_operation_by_id(None, 1)) is None


FIXED_UUID = "3f2a8c1d-1111-2222-3333-444455556666"


def _fake_mythic(monkeypatch, *, ops=(), callback_op=None, marker_uuid=None, capture=None, by_id=None):
    """Route execute_custom_query by query and keep written markers: a persisted marker is read back, as Mythic
    does. `marker_uuid` present => the marker already exists; None => absent (forces a generate+persist)."""
    markers = [marker_uuid] if marker_uuid else []
    names = dict(by_id or {})
    for op in ops:
        names.setdefault(int(op["id"]), op["name"])

    async def fake_q(client, query, variables=None):
        if "SageOpList" in query:
            return {"operation": list(ops)}
        if "SageOperationById" in query:
            op = int((variables or {}).get("op"))
            return {"operation_by_pk": {"id": op, "name": names[op]} if op in names else None}
        if "SageOpFromCallback" in query:
            return {"callback": ([{"operation_id": callback_op[0],
                                   "operation": {"id": callback_op[0], "name": callback_op[1]}}]
                                 if callback_op else [])}
        if "SageEngagementMarker" in query:
            return {"operationeventlog": [{"message": f"id: {m}"} for m in markers[:1]]}
        return {}

    async def fake_send(client, message, level="info", source=""):
        if capture is not None:
            capture.append((message, source))
        found = oc._UUID_RE.search(message)
        if found:
            markers.append(found.group(0))
        return {"status": "success"}
    monkeypatch.setattr(MM, "execute_custom_query", fake_q)
    monkeypatch.setattr(MM, "send_event_log_message", fake_send)
    return markers


def test_resolve_operation_from_callback(monkeypatch):
    _fake_mythic(monkeypatch, ops=[], callback_op=(1, "Operation Chimera"), marker_uuid=FIXED_UUID)
    assert _run(oc.resolve_operation(object())) == (1, "Operation Chimera")
    assert _run(oc.resolve_operation(object())) == (1, "Operation Chimera")


def test_resolve_operation_prefers_single_scoped_op(monkeypatch):
    # A scoped token sees exactly one op -> use it directly (works even with zero callbacks).
    _fake_mythic(monkeypatch, ops=[{"id": 9, "name": "Solo Op"}], callback_op=None, marker_uuid=FIXED_UUID)
    assert _run(oc.resolve_operation(object())) == (9, "Solo Op")


def test_resolve_operation_multi_op_uses_callback(monkeypatch):
    # Ambiguous (admin sees >1 op) -> anchor on the most recent callback's own operation, not a guess.
    _fake_mythic(monkeypatch, ops=[{"id": 1, "name": "Op A"}, {"id": 2, "name": "Op B"}],
                 callback_op=(2, "Op B"), marker_uuid=FIXED_UUID)
    assert _run(oc.resolve_operation(object())) == (2, "Op B")


def test_operation_key_int_str_id_identical():
    # Agent path yields an int id (GraphQL); the `state` path may yield str — keys must match.
    assert oc.operation_key("Operation Chimera", 1) == oc.operation_key("Operation Chimera", "1")


# --- durable UUID marker (reset-safe) ------------------------------------------------------------------

def test_marker_present_is_reused_without_writing(monkeypatch):
    cap = []
    _fake_mythic(monkeypatch, ops=[{"id": 1, "name": "Operation Chimera"}], marker_uuid=FIXED_UUID, capture=cap)
    assert _run(oc.get_or_create_engagement_uuid(object(), 1)) == FIXED_UUID
    assert cap == []                                   # existing marker => no new write


def test_marker_absent_generates_and_persists(monkeypatch):
    cap = []
    _fake_mythic(monkeypatch, ops=[{"id": 1, "name": "Operation Chimera"}], marker_uuid=None, capture=cap)
    u = _run(oc.get_or_create_engagement_uuid(object(), 1))
    assert oc._UUID_RE.fullmatch(u)                     # a real uuid4 was minted
    assert len(cap) == 1 and cap[0][1] == "sage_engagement_id" and u in cap[0][0]  # persisted as the marker


def test_fresh_instance_gets_new_uuid_so_old_ledger_is_not_reused(monkeypatch):
    # Two "instances": first has no marker (mints U1), second (post-reset) also has no marker (mints U2).
    a = _run(_mint_with(monkeypatch))
    b = _run(_mint_with(monkeypatch))
    assert a != b                                       # a DB reset => different uuid => different ledger file


async def _mint_with(monkeypatch):
    _fake_mythic(monkeypatch, ops=[{"id": 1, "name": "Operation Chimera"}], marker_uuid=None)
    return await oc.get_or_create_engagement_uuid(object(), 1)


def test_marker_that_cannot_be_read_back_leaves_the_ledger_unbound(monkeypatch):
    async def fake_q(client, query, variables=None):
        if "SageEngagementMarker" in query:
            return {"operationeventlog": []}
        return {}

    async def fake_send(client, message, level="info", source=""):
        return {"status": "success"}  # claims success, but the marker never becomes readable
    monkeypatch.setattr(MM, "execute_custom_query", fake_q)
    monkeypatch.setattr(MM, "send_event_log_message", fake_send)
    assert _run(oc.get_or_create_engagement_uuid(object(), 1)) is None


def test_concurrent_first_sessions_converge_on_the_earliest_marker(monkeypatch):
    markers = _fake_mythic(monkeypatch, ops=[{"id": 1, "name": "Operation Chimera"}], marker_uuid=None)
    first = _run(oc.get_or_create_engagement_uuid(object(), 1))
    markers.insert(0, "11111111-2222-4333-8444-555555555555")  # another session's marker landed earlier
    markers.append("99999999-2222-4333-8444-555555555555")
    assert first and _run(oc.get_or_create_engagement_uuid(object(), 1)) == "11111111-2222-4333-8444-555555555555"


# ---------------------------------------------------------------------------
# engagement_ledger: new `state_` prefix + no-drift
# ---------------------------------------------------------------------------

def test_ledger_prefix_is_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    p = el.ledger_path("Operation_Chimera_1")
    assert Path(p).name == "state_Operation_Chimera_1.json"
    assert "engagement_" not in Path(p).name


def test_hostile_operation_name_no_path_traversal(monkeypatch, tmp_path):
    # An operator-controlled operation name must not escape the state dir.
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    key = oc.operation_key("../../etc/evil", 1)
    p = Path(el.ledger_path(key))
    assert "/" not in p.name                                   # no separators survive into the filename
    assert str(p.resolve()).startswith(str(tmp_path.resolve()))  # stays inside the state dir


# ---------------------------------------------------------------------------
# MythicTools: operation-key resolution + persist
# ---------------------------------------------------------------------------

def _operation_session(monkeypatch, op_id=1, name="Operation Chimera", marker=FIXED_UUID):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "default")
    _fake_mythic(monkeypatch, marker_uuid=marker, by_id={op_id: name})
    mt = mythic_tools.MythicTools(operation_id=op_id, channel_id=3)
    mt.client = object()
    return mt


def test_ensure_engagement_key_binds_the_sessions_own_operation(monkeypatch):
    mt = _operation_session(monkeypatch)
    assert mt._eng_key() == ""                      # unbound: no default key
    _run(mt._ensure_engagement_key())
    assert mt._eng_key() == f"Operation_Chimera_1_{FIXED_UUID}"
    mt._pending_engagement_hop = ("dcsync", "sevenkingdoms.local", "2026-06-08T00:00:00Z")
    mt._record_engagement_success("  Hash NTLM: 2b576acbe6bcfda7294d6bd18041b8fe")
    assert [row["technique"] for row in mt._ledger_snapshot().display_hops()] == ["dcsync"]


def test_unbound_session_is_explicitly_unavailable_not_default(monkeypatch):
    import engagement_store

    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "default")
    mt = mythic_tools.MythicTools(agent_task_id="x")
    _run(mt._ensure_engagement_key())               # no operation id and no namespace
    assert mt._eng_key() == "" and mt._engagement_key is None
    with pytest.raises(engagement_store.LedgerUnavailable):
        mt._engagement_hops


def test_unreadable_operation_leaves_the_ledger_unavailable(monkeypatch):
    import engagement_store

    mt = _operation_session(monkeypatch, op_id=7)
    _fake_mythic(monkeypatch, marker_uuid=FIXED_UUID, by_id={})   # operation 7 not readable
    _run(mt._ensure_engagement_key())
    with pytest.raises(engagement_store.LedgerUnavailable, match="operation 7"):
        mt._engagement_hops


def test_reused_numeric_id_with_a_new_marker_is_a_separate_ledger(monkeypatch):
    old = _operation_session(monkeypatch)
    _run(old._ensure_engagement_key())
    seed_ledger(old, hops=es.hops_from_dicts([_ledger_hop("collect-graph", "stale", "graph-built:stale")]))
    after_reset = _operation_session(monkeypatch, marker="aaaaaaaa-1111-4222-8333-444444444444")
    _run(after_reset._ensure_engagement_key())
    assert after_reset._eng_key().endswith("aaaaaaaa-1111-4222-8333-444444444444")
    assert after_reset._engagement_hops == []


def test_operation_rename_keeps_the_ledger_and_its_proof_key(monkeypatch):
    first = _operation_session(monkeypatch)
    _run(first._ensure_engagement_key())
    seed_ledger(first, hops=es.hops_from_dicts([_ledger_hop("dcsync", "lab.local", "krbtgt-hash:lab.local")]))
    renamed = _operation_session(monkeypatch, name="Renamed Operation")
    _run(renamed._ensure_engagement_key())
    assert renamed._eng_key() == f"Operation_Chimera_1_{FIXED_UUID}"   # first canonical key retained
    assert [hop.status for hop in renamed._engagement_hops] == ["achieved"]  # proof still matches the key


def test_other_operation_cannot_read_this_operations_rows(monkeypatch):
    one = _operation_session(monkeypatch, op_id=1)
    _run(one._ensure_engagement_key())
    seed_ledger(one, hops=es.hops_from_dicts([_ledger_hop("dcsync", "lab.local", "krbtgt-hash:lab.local")]))
    two = _operation_session(monkeypatch, op_id=2, name="Other", marker="bbbbbbbb-1111-4222-8333-444444444444")
    _run(two._ensure_engagement_key())
    assert two._engagement_hops == []


def test_explicit_namespace_binds_only_sessions_without_an_operation(monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "manual-eng")
    offline = mythic_tools.MythicTools(agent_task_id="x")
    assert offline._eng_key() == "manual-eng"        # bound at construction, no Mythic query

    _fake_mythic(monkeypatch, marker_uuid=FIXED_UUID, by_id={1: "Operation Chimera"})
    chat = mythic_tools.MythicTools(operation_id=1, channel_id=3)
    chat.client = object()
    _run(chat._ensure_engagement_key())
    assert chat._eng_key() == f"Operation_Chimera_1_{FIXED_UUID}"   # a native-chat session keeps its operation


def test_engagement_objective_env_override(monkeypatch):
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_OBJECTIVE", "obtain administrative control of essos.local")

    mt = mythic_tools.MythicTools(agent_task_id="x")

    assert mt._engagement_objective() == "obtain administrative control of essos.local"


def test_bloodhound_collection_zip_shape_validation():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("computers.json", "[]")

    assert mythic_tools._looks_like_bloodhound_collection_zip(buf.getvalue()) is True
    assert mythic_tools._looks_like_bloodhound_collection_zip(b"SharpHound help text") is False


def test_ensure_engagement_key_idempotent(monkeypatch):
    mt = _operation_session(monkeypatch)
    calls = {"n": 0}
    real = oc.resolve_operation_by_id

    async def counting(client, op_id):
        calls["n"] += 1
        return await real(client, op_id)
    monkeypatch.setattr(oc, "resolve_operation_by_id", counting)

    async def run_twice():
        await mt._ensure_engagement_key()
        await mt._ensure_engagement_key()
    _run(run_twice())
    assert calls["n"] == 1 and mt._eng_key() == f"Operation_Chimera_1_{FIXED_UUID}"


# ---------------------------------------------------------------------------
# Credential tools
# ---------------------------------------------------------------------------

@pytest.fixture
def creds_tool(monkeypatch):
    async def fake_op(client):
        return (1, "Operation Chimera")
    monkeypatch.setattr(oc, "resolve_operation", fake_op)
    mt = mythic_tools.MythicTools(agent_task_id="x")
    mt.client = object()
    return mt


def test_read_credentials_formats_and_filters(creds_tool, monkeypatch):
    async def fake_q(client, query, variables=None):
        assert variables == {"op": 1}            # scoped to the resolved operation
        return {"credential": [
            {"account": "cersei.lannister", "realm": "sevenkingdoms.local", "type": "hash",
             "credential_text": "2b57...", "comment": "dcsync"},
            {"account": "jon.snow", "realm": "north.sevenkingdoms.local", "type": "plaintext",
             "credential_text": "pw", "comment": ""},
        ]}
    monkeypatch.setattr(MM, "execute_custom_query", fake_q)
    out = _run(creds_tool.read_credentials())
    assert "cersei.lannister" in out and "jon.snow" in out
    # realm filter narrows to the child domain only
    out2 = _run(creds_tool.read_credentials(realm="north."))
    assert "jon.snow" in out2 and "cersei.lannister" not in out2


def test_read_credentials_empty(creds_tool, monkeypatch):
    async def fake_q(client, query, variables=None):
        return {"credential": []}
    monkeypatch.setattr(MM, "execute_custom_query", fake_q)
    assert "No credentials" in _run(creds_tool.read_credentials())


def test_add_credential_success(creds_tool, monkeypatch):
    seen = {}
    async def fake_create(client, credential, account="", realm="", comment="", credential_type=""):
        seen.update(credential=credential, account=account, realm=realm, credential_type=credential_type)
        return {"status": "success", "id": 7}
    monkeypatch.setattr(MM, "create_credential", fake_create)
    out = _run(creds_tool.add_credential("S3cret", account="dcsync-user", realm="essos.local", credential_type="hash"))
    assert "id=7" in out and "essos.local" in out
    assert seen == {"credential": "S3cret", "account": "dcsync-user", "realm": "essos.local", "credential_type": "hash"}


def test_add_credential_requires_value(creds_tool):
    assert "non-empty" in _run(creds_tool.add_credential("   "))


def test_add_credential_is_guarded():
    assert "add_credential" in mythic_tools.GUARDED_TOOLS
    assert "read_credentials" not in mythic_tools.GUARDED_TOOLS   # read-only stays free
