"""Operation-named state ledger + Mythic credential-store tools.

Covers:
  * operation_context: operation_key build + resolve_operation(_key) via a mocked Mythic query.
  * engagement_ledger: the new `state_` filename prefix + agent/command no-drift.
  * MythicTools: lazy operation-key resolution (_ensure_engagement_key / _eng_key) + persist key,
    and the explicit SAGE_ENGAGEMENT_ID override.
  * read_credentials / add_credential tools (mocked mythic client) + GUARDED_TOOLS gating.
  * state.py: _resolve_engagement_id from taskData.Callback (import-guarded).

No live Mythic. The `mythic` lib functions are monkeypatched. Mirrors the repo's no-pytest-asyncio
convention (asyncio.run for the few async methods).
"""

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
    assert _run(oc.resolve_operation_key(None)) is None


FIXED_UUID = "3f2a8c1d-1111-2222-3333-444455556666"


def _fake_mythic(monkeypatch, *, ops=(), callback_op=None, marker_uuid=None, capture=None):
    """Route execute_custom_query by query (op-list / callback / engagement marker) and mock the marker
    write. `marker_uuid` present => the marker already exists; None => absent (forces a generate+persist)."""
    async def fake_q(client, query, variables=None):
        if "SageOpList" in query:
            return {"operation": list(ops)}
        if "SageOpFromCallback" in query:
            return {"callback": ([{"operation_id": callback_op[0],
                                   "operation": {"id": callback_op[0], "name": callback_op[1]}}]
                                 if callback_op else [])}
        if "SageEngagementMarker" in query:
            return {"operationeventlog": ([{"message": f"id: {marker_uuid}"}] if marker_uuid else [])}
        return {}

    async def fake_send(client, message, level="info", source=""):
        if capture is not None:
            capture.append((message, source))
        return {"status": "success"}
    monkeypatch.setattr(MM, "execute_custom_query", fake_q)
    monkeypatch.setattr(MM, "send_event_log_message", fake_send)


def test_resolve_operation_from_callback(monkeypatch):
    _fake_mythic(monkeypatch, ops=[], callback_op=(1, "Operation Chimera"), marker_uuid=FIXED_UUID)
    assert _run(oc.resolve_operation(object())) == (1, "Operation Chimera")
    assert _run(oc.resolve_operation_key(object())) == f"Operation_Chimera_1_{FIXED_UUID}"


def test_resolve_operation_prefers_single_scoped_op(monkeypatch):
    # A scoped token sees exactly one op -> use it directly (works even with zero callbacks).
    _fake_mythic(monkeypatch, ops=[{"id": 9, "name": "Solo Op"}], callback_op=None, marker_uuid=FIXED_UUID)
    assert _run(oc.resolve_operation_key(object())) == f"Solo_Op_9_{FIXED_UUID}"


def test_resolve_operation_multi_op_uses_callback(monkeypatch):
    # Ambiguous (admin sees >1 op) -> anchor on the most recent callback's own operation, not a guess.
    _fake_mythic(monkeypatch, ops=[{"id": 1, "name": "Op A"}, {"id": 2, "name": "Op B"}],
                 callback_op=(2, "Op B"), marker_uuid=FIXED_UUID)
    assert _run(oc.resolve_operation_key(object())) == f"Op_B_2_{FIXED_UUID}"


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


def test_resolve_key_falls_back_to_base_when_marker_unpersistable(monkeypatch):
    async def fake_q(client, query, variables=None):
        if "SageOpList" in query:
            return {"operation": [{"id": 1, "name": "Operation Chimera"}]}
        if "SageEngagementMarker" in query:
            return {"operationeventlog": []}
        return {}

    async def fake_send_fail(client, message, level="info", source=""):
        raise RuntimeError("mythic unreachable")
    monkeypatch.setattr(MM, "execute_custom_query", fake_q)
    monkeypatch.setattr(MM, "send_event_log_message", fake_send_fail)
    # Can't persist the marker -> non-durable base key (best effort), never a half-baked uuid.
    assert _run(oc.resolve_operation_key(object())) == "Operation_Chimera_1"


# ---------------------------------------------------------------------------
# engagement_ledger: new `state_` prefix + no-drift
# ---------------------------------------------------------------------------

def test_ledger_prefix_is_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    p = el.ledger_path("Operation_Chimera_1")
    assert Path(p).name == "state_Operation_Chimera_1.json"
    assert "engagement_" not in Path(p).name


def test_agent_and_ledger_resolve_same_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    assert mythic_tools._engagement_ledger_file("Operation_Chimera_1") == el.ledger_path("Operation_Chimera_1")


def test_list_engagements_scans_state_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    el.save({"hops": []}, "Operation_Chimera_1")
    (tmp_path / "engagement_old.json").write_text("{}")   # legacy file is NOT listed (clean break)
    assert el.list_engagements() == ["Operation_Chimera_1"]


def test_hostile_operation_name_no_path_traversal(monkeypatch, tmp_path):
    # An operator-controlled operation name must not escape the state dir (Advisor flag).
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    key = oc.operation_key("../../etc/evil", 1)
    p = Path(el.ledger_path(key))
    assert "/" not in p.name                                   # no separators survive into the filename
    assert str(p.resolve()).startswith(str(tmp_path.resolve()))  # stays inside the state dir


# ---------------------------------------------------------------------------
# MythicTools: operation-key resolution + persist
# ---------------------------------------------------------------------------

def test_ensure_engagement_key_resolves_operation(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "default")

    async def fake_key(client):
        return "Operation_Chimera_1"
    monkeypatch.setattr(oc, "resolve_operation_key", fake_key)

    mt = mythic_tools.MythicTools(agent_task_id="x")
    mt.client = object()
    assert mt._eng_key() == "default"          # before resolution
    _run(mt._ensure_engagement_key())
    assert mt._eng_key() == "Operation_Chimera_1"
    # A recorded hop persists under the operation-named file.
    mt._pending_engagement_hop = ("dcsync", "sevenkingdoms.local", "2026-06-08T00:00:00Z")
    mt._record_engagement_success("  Hash NTLM: 2b576acbe6bcfda7294d6bd18041b8fe")
    assert Path(mt._engagement_ledger_path()).name == "state_Operation_Chimera_1.json"
    assert Path(mt._engagement_ledger_path()).exists()


def test_default_ledger_not_loaded_before_operation_key(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "default")
    el.save({
        "hops": [_ledger_hop("collect-graph", "stale", "graph-built:stale")],
    }, "default")

    mt = mythic_tools.MythicTools(agent_task_id="x")

    assert mt._engagement_hops == []


def test_operation_key_load_replaces_stale_default_state(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "default")
    el.save({
        "hops": [_ledger_hop("collect-graph", "stale", "graph-built:stale")],
    }, "default")
    el.save({
        "hops": [_ledger_hop("capability:forge-golden-ticket", "domain=child.local;target_domain=parent.local", "da:parent.local")],
    }, "Operation_Chimera_1")

    async def fake_key(client):
        return "Operation_Chimera_1"
    monkeypatch.setattr(oc, "resolve_operation_key", fake_key)

    mt = mythic_tools.MythicTools(agent_task_id="x")
    mt.client = object()
    mt._engagement_hops = es.hops_from_dicts([
        _ledger_hop("collect-graph", "stale", "graph-built:stale"),
    ])

    _run(mt._ensure_engagement_key())

    effects = {getattr(h, "effect", "") for h in mt._engagement_hops}
    assert "da:parent.local" in effects
    assert "graph-built:stale" not in effects


def test_explicit_engagement_id_overrides_operation(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "manual-eng")

    async def boom(client):
        raise AssertionError("must not query Mythic when SAGE_ENGAGEMENT_ID is explicit")
    monkeypatch.setattr(oc, "resolve_operation_key", boom)

    mt = mythic_tools.MythicTools(agent_task_id="x")
    mt.client = object()
    _run(mt._ensure_engagement_key())
    assert mt._eng_key() == "manual-eng"


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


def test_ensure_engagement_key_idempotent(monkeypatch, tmp_path):
    # The lock + double-check means a second call (or a concurrent one) does NOT re-query or re-reload.
    monkeypatch.setenv("SAGE_ENGAGEMENT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(mythic_tools, "SAGE_ENGAGEMENT_ID", "default")
    calls = {"n": 0}

    async def fake_key(client):
        calls["n"] += 1
        return "Operation_Chimera_1"
    monkeypatch.setattr(oc, "resolve_operation_key", fake_key)

    mt = mythic_tools.MythicTools(agent_task_id="x")
    mt.client = object()

    async def run_twice():
        await mt._ensure_engagement_key()
        await mt._ensure_engagement_key()
    _run(run_twice())
    assert calls["n"] == 1 and mt._eng_key() == "Operation_Chimera_1"


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


# ---------------------------------------------------------------------------
# state.py: operation-key resolution from taskData (import-guarded)
# ---------------------------------------------------------------------------

class _FakeArgs:
    def __init__(self, eng=""):
        self._eng = eng
    def get_arg(self, name):
        return self._eng if name == "engagement" else ""


class _FakeCb:
    OperationID = 1
    OperationName = "Operation Chimera"


class _FakeTask:
    def __init__(self, eng=""):
        self.args = _FakeArgs(eng)
        self.Callback = _FakeCb()


def _load_state_module():
    sys.path.insert(0, str(LG.parent.parent))   # Payload_Type/sage, so `ai.langgraph` + `container.*` resolve
    try:
        import importlib
        return importlib.import_module("container.agent_functions.state")
    except Exception as e:  # pragma: no cover - environment-dependent
        pytest.skip(f"state.py not importable in this environment: {e}")


def test_state_resolves_operation_key_fallback(monkeypatch):
    # No Mythic client available in-test -> falls back to taskData.Callback op name+id (no durable uuid).
    state = _load_state_module()
    monkeypatch.delenv("SAGE_ENGAGEMENT_ID", raising=False)
    assert _run(state._resolve_engagement_id(_FakeTask())) == "Operation_Chimera_1"


def test_state_arg_overrides(monkeypatch):
    state = _load_state_module()
    assert _run(state._resolve_engagement_id(_FakeTask(eng="custom"))) == "custom"


def test_state_env_overrides(monkeypatch):
    state = _load_state_module()
    monkeypatch.setenv("SAGE_ENGAGEMENT_ID", "manual-eng")
    assert _run(state._resolve_engagement_id(_FakeTask())) == "manual-eng"


def test_state_reconcile_imports_credentials_without_returning_secret(monkeypatch):
    state = _load_state_module()
    secret = "a" * 64
    created = []

    async def fake_resolve_operation(client):
        return (1, "Operation Chimera")

    async def fake_query(client, query, variables=None):
        assert variables == {"op": 1}
        return {"credential": []}

    async def fake_create(client, credential, account="", realm="", comment="", credential_type=""):
        created.append({
            "credential": credential,
            "account": account,
            "realm": realm,
            "comment": comment,
            "credential_type": credential_type,
        })
        return {"status": "success", "id": 99}

    monkeypatch.setattr(state.operation_context, "resolve_operation", fake_resolve_operation)
    monkeypatch.setattr(state.mythic, "execute_custom_query", fake_query)
    monkeypatch.setattr(state.mythic, "create_credential", fake_create)

    material = {
        "account": "krbtgt",
        "realm": "sevenkingdoms.local",
        "secret_type": "aes256",
        "credential_type": "key",
        "credential": secret,
    }

    # Dry-run is the DEFAULT: task output is attacker-influenceable, so nothing is written to the Mythic
    # credential store without an explicit operator opt-in. Nothing created, nothing referenced, secret never leaked.
    refs_dry, notes_dry = _run(state._import_reconciled_credentials(object(), [material], 450))
    assert created == []
    assert refs_dry == []
    assert any("[dry-run]" in n for n in notes_dry)
    assert secret not in "\n".join(notes_dry)

    # apply=True is the explicit opt-in that actually writes the credential.
    refs, notes = _run(state._import_reconciled_credentials(object(), [material], 450, apply=True))

    assert created == [{
        "credential": secret,
        "account": "krbtgt",
        "realm": "sevenkingdoms.local",
        "comment": "Sage task-history reconcile from Mythic task 450: aes256",
        "credential_type": "key",
    }]
    assert refs == [{
        "id": 99,
        "account": "krbtgt",
        "realm": "sevenkingdoms.local",
        "secret_type": "aes256",
        "credential_type": "key",
        "status": "added",
    }]
    assert secret not in str(refs)
    assert secret not in "\n".join(notes)


def test_state_reconcile_reuses_existing_exact_credential(monkeypatch):
    state = _load_state_module()
    secret = "b" * 32
    created = []

    async def fake_resolve_operation(client):
        return (1, "Operation Chimera")

    async def fake_query(client, query, variables=None):
        return {"credential": [{
            "id": 7,
            "account": "krbtgt",
            "realm": "sevenkingdoms.local",
            "type": "hash",
            "credential_text": secret,
            "comment": "manual",
        }]}

    async def fake_create(*args, **kwargs):
        created.append(kwargs)
        return {"status": "success", "id": 100}

    monkeypatch.setattr(state.operation_context, "resolve_operation", fake_resolve_operation)
    monkeypatch.setattr(state.mythic, "execute_custom_query", fake_query)
    monkeypatch.setattr(state.mythic, "create_credential", fake_create)

    refs, notes = _run(state._import_reconciled_credentials(object(), [{
        "account": "krbtgt",
        "realm": "sevenkingdoms.local",
        "secret_type": "ntlm",
        "credential_type": "hash",
        "credential": secret.upper(),
    }], 450))

    assert created == []
    assert refs[0]["id"] == 7
    assert refs[0]["status"] == "existing"
    assert "reused" in "\n".join(notes)
