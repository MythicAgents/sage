"""A task-linked credential row proves its source task's effect only while its text is a value that task produced or
imported.

Mythic keeps a credential row's task link when an operator rewrites the row (`UpdateCredentialWebhook` never updates
`task_id`), so the link alone does not say where the current text came from. Store proof therefore re-checks content
at every use. The row's text must be one of three things: a string value of its task's `original_params` (an
import); a key or hash that the credential parser reads from its task's output; or, for other text, a whole token of
that output.

Every consumer of store proof runs through Sage's real credential readers: forge binding at grounding and after
Accept, the supervised step and operation-reconcile ledger writers, and the gate's corroboration probe. Only Mythic's
GraphQL transport is faked. It serves the recorded shapes (`original_params` JSON, `task.responses[].response_text`
base64) and returns exactly the fields a query selects. All values are synthetic. The ticket-import and dcsync-key
shapes follow archived Mythic records (ticket rows linked to their import task; dcsync output whose keys the parser
reads). No recorded task-linked hex or plaintext row exists, so those links are synthetic, and the whole-token
plaintext form is synthetic throughout.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tests.esl_fixtures import AGENT_PROCESSED, seed_ledger  # noqa: E402
from test_krbtgt_reference_resolution import _action, _ground_without_issue  # noqa: E402
from test_verified_capability_input_continuity import (  # noqa: E402
    _forge_from, _krbtgt_tools, _rubeus_issues, mythic_tools,
)

NOW = datetime.now(timezone.utc).isoformat()
REALM = "lab.local"
AES = "3a7c91e50b4d8f2617e9c35a8b0d4f62a1c3e5079b2d4f6810e2c4a6b8d0f213"
NTLM = "5e2b8d14c7a9f36b0e1d4c7a2f5b8e91"
SVC_NTLM = "7d1f3b5a9c2e4f6081a3c5e7f9b1d3a5"
ROTATED = "9c4e7a1f3b6d8025c7e9a1b3d5f709246a8c0e2f4b6d8a1c3e5f7092b4d6f81a"  # the probe's rotated key, no task's
PLAIN = "Winter-2026!q"
SPACED = "rain on the wall 26"
# Apollo's dcsync and mimikatz completion functions report a plaintext split from a `Password : <value>` line of the
# same task's output (CRLF lines); the value may hold spaces.
APOLLO_PASSWORD_OUTPUT = f"  Username : svc.backup\r\n  Domain   : LAB\r\n  Password : {SPACED}\r\n"
TICKET = "doIFujCCBbag" + "AwIBBaEDAgEWooIEvzCCBLthggS3MIIEs6ADAgEFoQ8bDUxBQi5MT0NBTKIiMCCgAwIBAqEZ" * 30
SECRETS = (AES, NTLM, SVC_NTLM, ROTATED, PLAIN, SPACED, TICKET)
CONTENT_REASON = "no proven gain (the entry's current text is not a value its producing task produced or imported)"

DUMP_TASK, CLEARTEXT_TASK, FAILED_DUMP_TASK, IMPORT_TASK = 21, 22, 23, 40


def _dump_output(aes_line: str | None = None) -> str:
    """Mimikatz `lsadump::dcsync` of krbtgt, in the shape of the recorded runs."""
    aes_line = aes_line if aes_line is not None else f"      aes256_hmac       (4096) : {AES}\n"
    return ("[DC] 'lab.local' will be the domain\n[DC] 'dc01.lab.local' will be the DC server\n"
            "[DC] 'krbtgt' will be the user account\n\nObject RDN           : krbtgt\n\n** SAM ACCOUNT **\n\n"
            "SAM Username         : krbtgt\nCredentials:\n"
            f"  Hash NTLM: {NTLM}\n\n* Primary:Kerberos-Newer-Keys *\n    Default Salt : LAB.LOCALkrbtgt\n"
            f"    Default Iterations : 4096\n    Credentials\n{aes_line}")


WRAPPED_AES = f"      aes256_hmac       (4096) : {AES[:40]}\n      {AES[40:]}\n"


def _cleartext_output(password_line: str = PLAIN) -> str:
    """A dcsync of an account stored with reversible encryption (synthetic: no recording exists)."""
    return ("[DC] 'lab.local' will be the domain\nObject RDN           : svc.backup\n\n** SAM ACCOUNT **\n\n"
            f"SAM Username         : svc.backup\nCredentials:\n  Hash NTLM: {SVC_NTLM}\n\n"
            f"* Primary:CLEARTEXT *\n    {password_line}\n")


def _task(display_id: int, command: str, params: dict, output: str, *, status: str = "success") -> dict:
    """Mythic's task record as Hasura holds it, with its response rows (`response_text` is base64)."""
    return {"display_id": display_id, "id": 1000 + display_id, "operation_id": 1, "status": status,
            "completed": True, **AGENT_PROCESSED, "command_name": command, "original_params": json.dumps(params),
            "callback": {"display_id": 13},
            "responses": [{"id": 9000 + display_id, "response_text": base64.b64encode(output.encode()).decode()}]}


def _dump_task(output: str | None = None, *, status: str = "success") -> dict:
    return _task(DUMP_TASK, "dcsync", {"domain": REALM, "user": "krbtgt"},
                 _dump_output() if output is None else output, status=status)


def _cleartext_task(output: str | None = None) -> dict:
    return _task(CLEARTEXT_TASK, "dcsync", {"domain": REALM, "user": "svc.backup"},
                 _cleartext_output() if output is None else output)


def _import_task(ticket_param: str = TICKET) -> dict:
    return _task(IMPORT_TASK, "ticket_cache_add", {"base64ticket": ticket_param, "luid": ""},
                 "[*] Imported the ticket into the current logon session.\n")


def _failed_dump_task() -> dict:
    return _task(FAILED_DUMP_TASK, "execute_pe",
                 {"pe_name": "mimikatz.exe", "pe_arguments": '"lsadump::dcsync /domain:lab.local /user:krbtgt" exit'},
                 "ERROR kuhl_m_lsadump_dcsync ; GetNCChanges: 0x000020f7 (8439)\n")


def _row(row_id: int, account: str, kind: str, text: str, task: dict | None, realm: str = REALM) -> dict:
    return {"id": row_id, "operation_id": 1, "deleted": False, "account": account, "realm": realm, "type": kind,
            "credential_text": text, "comment": "", "timestamp": NOW, "task": task}


def _key_row(text: str = AES, *, kind: str = "key", task: dict | None = None, row_id: int = 500) -> dict:
    return _row(row_id, "krbtgt", kind, text, task or _dump_task())


def _hash_row(text: str = NTLM, *, kind: str = "hash", row_id: int = 501) -> dict:
    return _row(row_id, "krbtgt", kind, text, _dump_task())


def _ticket_row(text: str = TICKET, *, task: dict | None = None) -> dict:
    return _row(600, "Administrator@LAB.LOCAL", "ticket", text, task or _import_task(), realm="LAB.LOCAL")


def _plain_row(text: str = PLAIN, *, task: dict | None = None) -> dict:
    return _row(700, "svc.backup", "plaintext", text, task or _cleartext_task())


def _one_char(value: str) -> str:
    return value[:-1] + ("0" if value[-1] != "0" else "1")


# Each accepted form and its equivalents, as the row Mythic would serve.
ACCEPTED = {
    "A-import": lambda: _ticket_row(),
    "A-row-whitespace": lambda: _ticket_row(f"  {TICKET}\n"),
    "A-param-whitespace": lambda: _ticket_row(task=_import_task(f"{TICKET}\n")),
    "B-aes-key": lambda: _key_row(),
    "B-ntlm-hash": lambda: _hash_row(),
    "B-uppercase": lambda: _key_row(AES.upper()),
    "B-wrapped-key": lambda: _key_row(task=_dump_task(_dump_output(WRAPPED_AES))),
    "C-token": lambda: _plain_row(),
    "C-quoted-token": lambda: _plain_row(task=_cleartext_task(_cleartext_output(f'"{PLAIN}"'))),
    "C-value-with-spaces": lambda: _plain_row(SPACED, task=_cleartext_task(APOLLO_PASSWORD_OUTPUT)),
}

# Each rejected near-match, and the probe's edit, as the row Mythic serves after the edit.
NEAR_MATCHES = {
    "probe-rotation": lambda: _hash_row(ROTATED, kind="key"),
    "one-hex-char": lambda: _key_row(_one_char(AES)),
    "half-of-key": lambda: _key_row(AES[:32], kind="hash"),
    "ticket-case": lambda: _ticket_row(TICKET.swapcase()),
    "ticket-truncated": lambda: _ticket_row(TICKET[:-12]),
    "param-substring": lambda: _row(800, "krbtgt", "plaintext", "krbtgt", _failed_dump_task()),
    "inside-longer-token": lambda: _plain_row(PLAIN[:-2]),
    "spaced-value-truncated": lambda: _plain_row(SPACED[:-1], task=_cleartext_task(APOLLO_PASSWORD_OUTPUT)),
}
HEX_NEAR_MATCHES = ["probe-rotation", "one-hex-char", "half-of-key"]
TOKEN_NEAR_MATCHES = [*HEX_NEAR_MATCHES, "inside-longer-token", "spaced-value-truncated"]

# Edits applied after Accept to the pinned row 501 (rc4, task 21).
EDITS_AFTER_ACCEPT = {
    "probe-rotation": {"credential_text": ROTATED, "type": "key"},
    "one-hex-char": {"credential_text": _one_char(NTLM)},
    "half-of-key": {"credential_text": AES[:32]},
}


# --- Mythic's GraphQL transport --------------------------------------------------------------------------------------

def _close(text: str, start: int, opening: str, closing: str) -> int:
    """Index just past the bracket that closes the one at `start`."""
    depth = 0
    for index in range(start, len(text)):
        depth += {opening: 1, closing: -1}.get(text[index], 0)
        if depth == 0:
            return index + 1
    raise AssertionError(f"unbalanced {opening}{closing} in query")


def _selection(query: str, head: str) -> dict:
    """The selection set of the field `head`: field names only, arguments removed, nesting kept."""
    start = query.index(head) + len(head)
    if query[start] == "(":
        start = _close(query, start, "(", ")")
    open_brace = query.index("{", start)
    body = query[open_brace + 1:_close(query, open_brace, "{", "}") - 1]
    while "(" in body:
        at = body.index("(")
        body = body[:at] + body[_close(body, at, "(", ")"):]
    spec: dict = {}
    stack, last = [spec], ""
    for token in re.findall(r"[{}]|\w+", body):
        if token == "{":
            stack[-1][last] = {}
            stack.append(stack[-1][last])
        elif token == "}":
            stack.pop()
        else:
            stack[-1][token], last = None, token
    return spec


def _project(value, spec: dict | None):
    """What Hasura returns for `value`: only the selected fields, through objects and arrays."""
    if spec is None:
        return value
    if isinstance(value, list):
        return [_project(item, spec) for item in value]
    if not isinstance(value, dict):
        return value
    return {name: _project(value.get(name), nested) for name, nested in spec.items()}


def _client(monkeypatch, tmp_path, rows: list[dict]):
    """The production client reading `rows` through Sage's own credential readers; only GraphQL is faked."""
    mt, store = _krbtgt_tools(monkeypatch, tmp_path)
    seed_ledger(mt, hops=[])
    del mt._fetch_credentials_cached  # the fixture's stand-in; Sage's reader from here on
    store["rows"] = rows
    store["queries"] = []
    for row in rows:
        task = row.get("task") or {}
        for response in task.get("responses") or []:
            store["task_outputs"][task["display_id"]] = base64.b64decode(response["response_text"]).decode()
    original = mythic_tools.mythic.execute_custom_query

    async def execute_custom_query(client, query, variables=None, *args, **kwargs):
        variables = dict(variables or {})
        name = re.match(r"\s*query (\w+)", query).group(1)
        store["queries"].append((name, query))
        live = [row for row in store["rows"] if not row.get("deleted")
                and ("op" not in variables or row["operation_id"] == variables["op"])]
        if name == "SageOpList":
            return {"operation": [{"id": 1, "name": "fixture operation"}]}
        if name in {"SageReadCredentials", "SageTaskCredentials", "SageCitedCredential"}:
            if name == "SageTaskCredentials":
                live = [row for row in live if (row.get("task") or {}).get("display_id") in variables["ids"]]
            elif name == "SageCitedCredential":
                live = [row for row in store["rows"] if row["id"] == variables["id"] and not row.get("deleted")]
            spec = _selection(query, "credential")
            return {"credential": [_project(row, spec) for row in sorted(live, key=lambda r: r["id"],
                                                                          reverse=name == "SageReadCredentials")]}
        if name == "SageCitedTask":
            spec = _selection(query, "task")
            tasks = {row["task"]["display_id"]: row["task"] for row in store["rows"] if row.get("task")}
            return {"task": [_project(tasks[variables["id"]], spec)] if variables["id"] in tasks else []}
        if name == "SageCreditTasks":
            return {"window": [], "on_callback": []}
        return await original(client, query, variables, *args, **kwargs)

    async def no_callbacks(self):
        return []

    monkeypatch.setattr(mythic_tools.mythic, "execute_custom_query", execute_custom_query)
    monkeypatch.setattr(type(mt), "_step_callback_rows", no_callbacks)
    return mt, store


def _read(mt) -> list[dict]:
    return asyncio.run(mt._fetch_credentials_cached(NOW, force=True))


def _expire_cache(mt) -> None:
    """The short credential cache has expired: the next read sees Mythic's current rows."""
    mt._cred_cache_ts = None


def _edit(store, row_id: int, **fields) -> None:
    """An operator rewrites a row in Mythic: text and type change, the task link stays."""
    row = next(row for row in store["rows"] if row["id"] == row_id)
    row.update(fields)


def _no_secret(value) -> None:
    text = json.dumps(value, default=str)
    for secret in SECRETS:
        assert secret not in text and secret.upper() not in text


def _hop_for(mt, credential_id: int):
    hops = [hop for hop in mt._ledger_view().hops
            if str((getattr(hop, "evidence", {}) or {}).get("credential_id")) == str(credential_id)]
    assert len(hops) <= 1, hops
    return hops[0] if hops else None


def _step(mt, row: dict) -> dict:
    references = {"tasks": [], "graph_reads": [],
                  "credentials": [{"credential_id": row["id"], "source_task_id": row["task"]["display_id"]}]}
    return asyncio.run(mt._record_step_ledger(references, []))


def _reconcile(mt, *cited: str) -> list[str]:
    return asyncio.run(mt._reconcile_operation(list(cited)))


def _facts(mt) -> dict:
    return {fact.predicate: fact.proof_envelope for fact in asyncio.run(mt._corroboration_facts(NOW))}


# --- the proof owner, through the real reader ------------------------------------------------------------------------

@pytest.mark.parametrize("form", ACCEPTED)
def test_an_accepted_form_keeps_store_proof(monkeypatch, tmp_path, form):
    mt, _ = _client(monkeypatch, tmp_path, [ACCEPTED[form]()])

    [row] = _read(mt)

    assert mt._credential_store_proof_envelope(row, NOW)


@pytest.mark.parametrize("near_match", NEAR_MATCHES)
def test_a_near_match_loses_store_proof(monkeypatch, tmp_path, near_match):
    mt, _ = _client(monkeypatch, tmp_path, [NEAR_MATCHES[near_match]()])

    [row] = _read(mt)

    assert mt._credential_store_proof_envelope(row, NOW) == {}


@pytest.mark.parametrize("row", [
    _row(900, "krbtgt", "key", AES, None),                                  # typed into Mythic: no task link
    _key_row(task=_dump_task(status="error")),                              # its task did not succeed
], ids=["no-link", "task-not-successful"])
def test_rows_outside_the_content_check_keep_todays_answer(monkeypatch, tmp_path, row):
    mt, _ = _client(monkeypatch, tmp_path, [row])

    [read] = _read(mt)

    assert mt._credential_store_proof_envelope(read, NOW) == {}


def test_both_readers_carry_the_task_output_with_the_row(monkeypatch, tmp_path):
    from ai.langgraph import state_reconcile

    mt, store = _client(monkeypatch, tmp_path, [_key_row()])

    [cached] = _read(mt)
    [by_task] = asyncio.run(state_reconcile.fetch_task_credentials(mt.client, 1, [DUMP_TASK]))
    cited, reason = asyncio.run(state_reconcile.fetch_cited_credential(mt.client, 1, 500))

    assert reason == ""
    expected = [{"response_text": item["response_text"]} for item in store["rows"][0]["task"]["responses"]]
    for row in (cached, by_task, cited):
        assert row["task"]["responses"] == expected, row["task"]  # the response text only, from this exact task


# --- forge binding: at grounding and after Accept --------------------------------------------------------------------

@pytest.mark.parametrize("form", ["B-aes-key", "B-uppercase", "B-wrapped-key"])
@pytest.mark.parametrize("pin", [{}, {"credential_id": 500}], ids=["unpinned", "by-id"])
def test_an_accepted_key_row_binds_and_forges(monkeypatch, tmp_path, form, pin):
    mt, store = _client(monkeypatch, tmp_path, [ACCEPTED[form](), _hash_row()])
    key = store["rows"][0]["credential_text"]

    [call] = _ground_without_issue(mt, dict(pin), _action("forge-golden-ticket")).tool_calls

    inputs = call["args"]["inputs"]
    assert str(inputs["credential_id"]) == "500" and str(inputs["credential_proof_task_id"]) == str(DUMP_TASK)
    _no_secret(call)
    _expire_cache(mt)
    result, calls = _forge_from(mt, inputs, call["args"]["action"])
    [forge] = _rubeus_issues(calls)
    assert f"/aes256:{key}" in forge, forge
    _no_secret(result)


def test_a_pinned_hash_row_binds_and_forges(monkeypatch, tmp_path):
    mt, _ = _client(monkeypatch, tmp_path, [_key_row(), _hash_row()])

    [call] = _ground_without_issue(mt, {"credential_id": 501}, _action("forge-golden-ticket")).tool_calls

    assert call["args"]["inputs"]["key_type"] == "rc4"
    _expire_cache(mt)
    _, calls = _forge_from(mt, call["args"]["inputs"], call["args"]["action"])
    [forge] = _rubeus_issues(calls)
    assert f"/rc4:{NTLM} " in forge, forge


@pytest.mark.parametrize("near_match", HEX_NEAR_MATCHES)
@pytest.mark.parametrize("pin", [{}, "row"], ids=["unpinned", "by-id"])
def test_an_edit_before_grounding_offers_no_card(monkeypatch, tmp_path, near_match, pin):
    row = NEAR_MATCHES[near_match]()
    mt, store = _client(monkeypatch, tmp_path, [row])
    inputs = {"credential_id": row["id"]} if pin == "row" else {}

    message = _ground_without_issue(mt, inputs, _action("forge-golden-ticket"))

    assert message.tool_calls == []
    assert "No forge was issued." in str(message.content), message.content
    _no_secret(message.content)


@pytest.mark.parametrize("edit", EDITS_AFTER_ACCEPT)
def test_an_edit_after_accept_issues_no_forge(monkeypatch, tmp_path, edit):
    mt, store = _client(monkeypatch, tmp_path, [_key_row(), _hash_row()])
    [call] = _ground_without_issue(mt, {"credential_id": 501}, _action("forge-golden-ticket")).tool_calls
    approved = call["args"]["inputs"]
    assert str(approved["credential_id"]) == "501" and approved["key_type"] == "rc4"

    _edit(store, 501, **EDITS_AFTER_ACCEPT[edit])
    _expire_cache(mt)
    result, calls = _forge_from(mt, approved, call["args"]["action"])

    assert calls.get("issued", []) == [], calls
    assert result["issued"] == [] and "no longer resolves" in result["reason"], result
    _no_secret(result)


# --- the ledger: supervised step and operation reconcile -------------------------------------------------------------

@pytest.mark.parametrize("form", ACCEPTED)
def test_an_accepted_form_is_achieved_from_the_supervised_step(monkeypatch, tmp_path, form):
    row = ACCEPTED[form]()
    mt, _ = _client(monkeypatch, tmp_path, [row])

    summary = _step(mt, row)

    hop = _hop_for(mt, row["id"])
    assert hop is not None and hop.status == "achieved", (summary, hop)
    if form.startswith("A-"):
        assert hop.effect.startswith("cred-material:ticket:"), hop.effect


@pytest.mark.parametrize("form", ACCEPTED)
def test_an_accepted_form_is_achieved_from_a_cited_reconcile(monkeypatch, tmp_path, form):
    row = ACCEPTED[form]()
    mt, _ = _client(monkeypatch, tmp_path, [row])

    lines = _reconcile(mt, f"cred:{row['id']}")

    hop = _hop_for(mt, row["id"])
    assert hop is not None and hop.status == "achieved", lines
    assert any(f"cred:{row['id']}: added row #" in line for line in lines), lines


def test_an_imported_ticket_is_achieved_from_its_tasks_reconcile(monkeypatch, tmp_path):
    mt, store = _client(monkeypatch, tmp_path, [_ticket_row()])

    lines = _reconcile(mt, str(IMPORT_TASK))

    hop = _hop_for(mt, 600)
    assert hop is not None and hop.status == "achieved" and hop.effect.startswith("cred-material:ticket:"), lines
    assert "SageTaskCredentials" in [name for name, _query in store["queries"]]


@pytest.mark.parametrize("near_match", NEAR_MATCHES)
def test_a_near_match_lands_pending_from_the_step_and_adds_no_reconcile_row(monkeypatch, tmp_path, near_match):
    row = NEAR_MATCHES[near_match]()
    mt, _ = _client(monkeypatch, tmp_path, [row])

    lines = _reconcile(mt, f"cred:{row['id']}")

    assert _hop_for(mt, row["id"]) is None
    assert f"- cred:{row['id']}: not added, {CONTENT_REASON}." in lines, lines

    summary = _step(mt, row)

    [recorded] = summary["recorded"]  # a pending row keeps no proof evidence, so it is found by its effect
    hop = next(hop for hop in mt._ledger_view().hops if hop.effect == recorded["effect"])
    assert recorded["status"] == hop.status == "pending", (summary, hop)


@pytest.mark.parametrize("edit", ["probe-rotation", "ticket-case"])
def test_an_achieved_row_is_never_downgraded_by_a_later_edit(monkeypatch, tmp_path, edit):
    row = _hash_row() if edit == "probe-rotation" else _ticket_row()
    mt, store = _client(monkeypatch, tmp_path, [row])
    _step(mt, row)
    assert _hop_for(mt, row["id"]).status == "achieved"

    if edit == "probe-rotation":
        _edit(store, row["id"], credential_text=ROTATED, type="key")
    else:
        _edit(store, row["id"], credential_text=TICKET.swapcase())
    _expire_cache(mt)
    _step(mt, row)
    lines = _reconcile(mt, f"cred:{row['id']}")

    assert _hop_for(mt, row["id"]).status == "achieved"
    assert f"- cred:{row['id']}: not added, {CONTENT_REASON}." in lines, lines


# --- corroboration --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("form, predicate", [
    ("B-aes-key", "krbtgt-hash:lab.local"),
    ("B-ntlm-hash", "creds:krbtgt@lab.local"),
    ("B-wrapped-key", "krbtgt-hash:lab.local"),
    ("C-token", "creds:svc.backup@lab.local"),
    ("C-quoted-token", "creds:svc.backup@lab.local"),
    ("C-value-with-spaces", "creds:svc.backup@lab.local"),
])
def test_an_accepted_form_corroborates_with_proof(monkeypatch, tmp_path, form, predicate):
    mt, _ = _client(monkeypatch, tmp_path, [ACCEPTED[form]()])

    assert _facts(mt).get(predicate), _facts(mt)


@pytest.mark.parametrize("near_match", TOKEN_NEAR_MATCHES)
def test_a_near_match_corroborates_without_proof(monkeypatch, tmp_path, near_match):
    mt, _ = _client(monkeypatch, tmp_path, [NEAR_MATCHES[near_match]()])

    facts = _facts(mt)

    assert facts and all(proof == {} for proof in facts.values()), facts
