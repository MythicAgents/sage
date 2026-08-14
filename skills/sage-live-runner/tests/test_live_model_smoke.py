from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "live_model_smoke.py"
SPEC = importlib.util.spec_from_file_location("live_model_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)

DOTENV_SCRIPT = Path(__file__).resolve().parents[3] / "Payload_Type" / "sage" / "dotenv_bootstrap.py"
DOTENV_SPEC = importlib.util.spec_from_file_location("live_model_smoke_test_dotenv", DOTENV_SCRIPT)
assert DOTENV_SPEC is not None and DOTENV_SPEC.loader is not None
canonical_dotenv = importlib.util.module_from_spec(DOTENV_SPEC)
DOTENV_SPEC.loader.exec_module(canonical_dotenv)


@pytest.fixture(autouse=True)
def _block_maintainer_runtime_dotenv(monkeypatch):
    monkeypatch.setattr(
        smoke,
        "dotenv_bootstrap",
        SimpleNamespace(load_sage_dotenv=lambda _directory: []),
        raising=False,
    )


def _args(**overrides):
    values = {"live_test": True, "timeout": 3, "nonce": "SAGE-EXACT-NONCE"}
    values.update(overrides)
    return argparse.Namespace(**values)


def _transcript(request_id: int, channel_id: int, answer: str, prompt: str):
    transitions = [
        {"content": "request contract installed", "event_id": f"{request_id}-install", "kind": "control_transition", "phase": "request_installed"},
        {"content": "complete", "event_id": f"{request_id}-terminal", "kind": "control_transition", "phase": "request_terminal"},
    ]
    return {
        "request": {"id": request_id, "channel_id": channel_id, "request_message_id": request_id * 10, "status": "completed", "error": ""},
        "messages": [
            {"id": request_id * 10, "channel_id": channel_id, "chat_request_id": None, "message": prompt, "author_type": "operator", "metadata": {}},
            {"id": request_id * 10 + 1, "channel_id": channel_id, "chat_request_id": request_id, "message": answer, "author_type": "ai", "metadata": {"control_transitions": transitions}},
        ],
    }


THREAD = "7:generation:0123456789abcdef0123456789abcdef"
IDENTITY = {"provider": "openai", "model": "real-model", "route": "direct"}


def _authority(channel_id: int, request_id: object) -> str:
    return f'[turn-authority] contract {json.dumps({"request_id": f"chat:{channel_id}:request:{request_id}"})}'


def _span(
    trace_id: str,
    *,
    authorities: tuple[str, ...] = (),
    channel_id: int = 7,
    request_id: object = 11,
    thread_id: str | None = THREAD,
    provider: str | None = "openai",
    model: str | None = "real-model",
    prompt: int = 3,
    completion: int = 2,
    status: str = "OK",
    span_kind: str = "LLM",
    attributes: object | None = None,
) -> dict[str, object]:
    metadata = {"thread_id": thread_id, "ls_provider": provider, "ls_model_name": model}
    input_value = " ".join(authorities + (_authority(channel_id, request_id),))
    return {
        "trace_id": trace_id,
        "attributes": json.dumps({"metadata": metadata, "input": {"value": input_value}}) if attributes is None else attributes,
        "prompt": prompt,
        "completion": completion,
        "status": status,
        "span_kind": span_kind,
    }


def _phoenix_db(tmp_path: Path, spans: list[dict[str, object]]) -> Path:
    path = tmp_path / "phoenix.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE traces(trace_id TEXT);"
            "CREATE TABLE spans(id INTEGER PRIMARY KEY, trace_rowid INTEGER, span_id TEXT, name TEXT, "
            "span_kind TEXT, attributes TEXT, llm_token_count_prompt INTEGER, "
            "llm_token_count_completion INTEGER, status_code TEXT);"
        )
        trace_rowids: dict[str, int] = {}
        for index, span in enumerate(spans, 1):
            trace_id = str(span["trace_id"])
            if trace_id not in trace_rowids:
                conn.execute("INSERT INTO traces(trace_id) VALUES (?)", (trace_id,))
                trace_rowids[trace_id] = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO spans(trace_rowid, span_id, name, span_kind, attributes, llm_token_count_prompt, llm_token_count_completion, status_code) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (trace_rowids[trace_id], f"span-{index}", "ChatOpenAI", span["span_kind"], span["attributes"], span["prompt"], span["completion"], span["status"]),
            )
    return path


def _validated_evidence(tmp_path: Path, spans: list[dict[str, object]]):
    evidence = smoke.phoenix_request_evidence(
        _phoenix_db(tmp_path, spans),
        0,
        channel_id=7,
        request_ids=[11, 12],
        identity=IDENTITY,
    )
    return smoke.validate_phoenix_evidence(evidence, request_ids=[11, 12], identity=IDENTITY)


def test_missing_ack_refuses_before_login(monkeypatch):
    monkeypatch.setattr(smoke, "dotenv_bootstrap", SimpleNamespace(load_sage_dotenv=lambda _directory: pytest.fail("runtime dotenv loaded")))
    monkeypatch.setattr(smoke, "configured_identity", lambda: pytest.fail("identity constructed"))
    monkeypatch.setattr(smoke.native_chat, "login", lambda **_kw: pytest.fail("login called"))
    with pytest.raises(RuntimeError, match="--live-test"):
        asyncio.run(smoke.run_smoke(_args(live_test=False)))


def test_live_ack_loads_canonical_runtime_dotenv_before_identity(monkeypatch):
    events = []
    monkeypatch.setattr(smoke, "dotenv_bootstrap", SimpleNamespace(load_sage_dotenv=lambda directory: events.append(("load", Path(directory)))))
    monkeypatch.setattr(smoke, "configured_identity", lambda: events.append(("identity", None)) or {"provider": "fake", "model": "real", "route": ""})
    monkeypatch.setattr(smoke.native_chat, "login", lambda **_kw: pytest.fail("login called"))
    with pytest.raises(RuntimeError, match="provider/model"):
        asyncio.run(smoke.run_smoke(_args()))
    assert events == [("load", SCRIPT.parents[3] / "Payload_Type" / "sage"), ("identity", None)]


def test_canonical_runtime_dotenv_precedence_existing_and_empty_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("SAGE_SMOKE_EXISTING", "process")
    monkeypatch.delenv("SAGE_SMOKE_ORDER", raising=False)
    monkeypatch.delenv("SAGE_SMOKE_EMPTY", raising=False)
    monkeypatch.delenv("SAGE_SMOKE_SECOND", raising=False)
    (tmp_path / ".env.local").write_text("SAGE_SMOKE_ORDER=local\nSAGE_SMOKE_EXISTING=local\nSAGE_SMOKE_EMPTY=\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SAGE_SMOKE_ORDER=tracked\nSAGE_SMOKE_EXISTING=tracked\nSAGE_SMOKE_SECOND=tracked\n", encoding="utf-8")

    applied = canonical_dotenv.load_sage_dotenv(str(tmp_path))

    assert __import__("os").environ["SAGE_SMOKE_ORDER"] == "local"
    assert __import__("os").environ["SAGE_SMOKE_EXISTING"] == "process"
    assert __import__("os").environ["SAGE_SMOKE_SECOND"] == "tracked"
    assert "SAGE_SMOKE_EMPTY" not in __import__("os").environ
    assert applied == ["SAGE_SMOKE_ORDER", "SAGE_SMOKE_SECOND"]


@pytest.mark.parametrize("value", ["", "fake", "null", "mock", "test-provider"])
def test_provider_or_model_sentinel_refuses_before_channel(monkeypatch, value):
    monkeypatch.setattr(smoke, "configured_identity", lambda: {"provider": value, "model": "real", "route": "direct"})
    monkeypatch.setattr(smoke.native_chat, "login", lambda **_kw: pytest.fail("login called"))
    with pytest.raises(RuntimeError, match="provider/model"):
        asyncio.run(smoke.run_smoke(_args()))


def test_model_sentinel_is_rejected_independently(monkeypatch):
    monkeypatch.setattr(smoke, "configured_identity", lambda: {"provider": "openai", "model": "mock-model", "route": "direct"})
    monkeypatch.setattr(smoke.native_chat, "login", lambda **_kw: pytest.fail("login called"))
    with pytest.raises(RuntimeError, match="provider/model"):
        asyncio.run(smoke.run_smoke(_args()))


def test_two_turn_same_channel_success_and_exact_nonce(monkeypatch, tmp_path):
    channel = {"chat_channel_id": 7, "chat_runtime_identity": {"provider": "openai", "model": "real-model", "route": "direct"}}
    snapshots = [_transcript(11, 7, "I will remember it.", "remember"), _transcript(12, 7, "SAGE-EXACT-NONCE", "recall")]
    secret_marker = "unit-test-secret-marker"
    monkeypatch.setattr(smoke, "dotenv_bootstrap", SimpleNamespace(load_sage_dotenv=lambda _directory: monkeypatch.setenv("API_KEY", secret_marker) or ["API_KEY"]))
    monkeypatch.setattr(smoke, "configured_identity", lambda: channel["chat_runtime_identity"])
    monkeypatch.setattr(smoke.native_chat, "login", lambda **_kw: asyncio.sleep(0, result=object()))
    seen_metadata = {}
    async def fake_channel(*_a, **kw):
        seen_metadata.update(kw["metadata"]["config"])
        return channel
    monkeypatch.setattr(smoke.native_chat, "create_locked_channel", fake_channel)
    request_rows = iter([{"chat_request_id": 11}, {"chat_request_id": 12}])
    monkeypatch.setattr(smoke.native_chat, "create_message", lambda *_a, **_kw: asyncio.sleep(0, result=next(request_rows)))
    calls = iter(snapshots)
    monkeypatch.setattr(smoke.native_chat, "wait_for_request", lambda *_a, **_kw: asyncio.sleep(0, result=next(calls)))
    monkeypatch.setattr(smoke, "task_state", lambda *_a: asyncio.sleep(0, result={"count": 0, "max_id": None}))
    monkeypatch.setattr(smoke.phoenix_reader, "max_trace_rowid", lambda _db: 10)
    monkeypatch.setattr(smoke, "wait_for_phoenix_evidence", lambda *_a, **_kw: asyncio.sleep(0, result={"model_calls": 2, "tokens": 9, "error_count": 0, "thread_id": THREAD, "trace_ids": ["a", "b"]}))
    monkeypatch.setattr(smoke, "retain_artifacts", lambda *_a, **_kw: {"manifested": 3})

    report = asyncio.run(smoke.run_smoke(_args()))
    assert report["status"] == "passed"
    assert report["chat_channel_id"] == 7
    assert secret_marker not in json.dumps(report)
    assert seen_metadata["mode"] == "supervised"
    assert seen_metadata["autonomous_solve"] is False


@pytest.mark.parametrize("failure", ["pending", "task", "nonce", "wrong-channel", "duplicate", "incomplete", "empty", "identity"])
def test_fail_closed_cases(monkeypatch, failure):
    channel_id = 7
    snapshots = [_transcript(11, channel_id, "ok", "remember"), _transcript(12, channel_id, "SAGE-EXACT-NONCE", "recall")]
    if failure == "pending":
        snapshots[0]["messages"][1]["metadata"] = {"special_type": "input_requested", "input_requested": {"status": "pending"}}
    if failure == "nonce":
        snapshots[1]["messages"][1]["message"] = "SAGE-EXACT-NONC"
    if failure == "wrong-channel":
        snapshots[1] = _transcript(12, 8, "SAGE-EXACT-NONCE", "recall")
    if failure == "incomplete":
        snapshots[0]["request"]["status"] = "streaming"
    if failure == "empty":
        snapshots[0]["messages"][1]["message"] = ""
    monkeypatch.setattr(smoke, "configured_identity", lambda: {"provider": "openai", "model": "real", "route": "direct"})
    monkeypatch.setattr(smoke.native_chat, "login", lambda **_kw: asyncio.sleep(0, result=object()))
    monkeypatch.setattr(smoke.native_chat, "create_locked_channel", lambda *_a, **_kw: asyncio.sleep(0, result={"chat_channel_id": channel_id, "chat_runtime_identity": smoke.configured_identity()}))
    reqs = iter([{"chat_request_id": 11}, {"chat_request_id": 11 if failure == "duplicate" else 12}])
    monkeypatch.setattr(smoke.native_chat, "create_message", lambda *_a, **_kw: asyncio.sleep(0, result=next(reqs)))
    snaps = iter(snapshots)
    monkeypatch.setattr(smoke.native_chat, "wait_for_request", lambda *_a, **_kw: asyncio.sleep(0, result=next(snaps)))
    states = iter([{"count": 0, "max_id": None}, {"count": 1 if failure == "task" else 0, "max_id": None}, {"count": 0, "max_id": None}])
    monkeypatch.setattr(smoke, "task_state", lambda *_a: asyncio.sleep(0, result=next(states)))
    monkeypatch.setattr(smoke.phoenix_reader, "max_trace_rowid", lambda _db: 10)
    monkeypatch.setattr(smoke, "wait_for_phoenix_evidence", lambda *_a, **_kw: asyncio.sleep(0, result={"model_calls": 2, "tokens": 9, "error_count": 0, "thread_id": THREAD, "trace_ids": ["a", "b"]}))
    if failure == "identity":
        monkeypatch.setattr(smoke.native_chat, "create_locked_channel", lambda *_a, **_kw: asyncio.sleep(0, result={"chat_channel_id": channel_id, "chat_runtime_identity": {"provider": "openai", "model": "other", "route": "direct"}}))

    with pytest.raises(RuntimeError):
        asyncio.run(smoke.run_smoke(_args()))


def test_phoenix_evidence_waits_for_delayed_stable_per_request(monkeypatch):
    turn_one_only = {
        "model_calls": 3, "tokens": 12, "error_count": 0,
        "per_request": {
            "11": {"model_calls": 3, "tokens": 12, "zero_token_calls": 0, "providers": ["openai"], "models": ["real-model"], "thread_ids": [THREAD], "trace_ids": ["a"]},
            "12": {"model_calls": 0, "tokens": 0, "zero_token_calls": 0, "providers": [], "models": [], "thread_ids": [], "trace_ids": []},
        },
    }
    settled = {
        "model_calls": 2, "tokens": 9, "error_count": 0,
        "per_request": {
            "11": {"model_calls": 1, "tokens": 4, "zero_token_calls": 0, "providers": ["openai"], "models": ["real-model"], "thread_ids": [THREAD], "trace_ids": ["a"]},
            "12": {"model_calls": 1, "tokens": 5, "zero_token_calls": 0, "providers": ["openai"], "models": ["real-model"], "thread_ids": [THREAD], "trace_ids": ["b"]},
        },
    }
    rows = iter([
        turn_one_only, turn_one_only, turn_one_only,
        settled, settled, settled,
    ])
    monkeypatch.setattr(smoke, "phoenix_request_evidence", lambda *_a, **_kw: next(rows))
    monkeypatch.setattr(smoke.asyncio, "sleep", lambda _s: _noop())
    assert asyncio.run(smoke.wait_for_phoenix_evidence(Path("unused"), 1, channel_id=7, request_ids=[11, 12], identity=IDENTITY, timeout_seconds=1, poll_seconds=0))["model_calls"] == 2


def test_phoenix_evidence_settle_timeout(monkeypatch):
    monkeypatch.setattr(smoke, "phoenix_request_evidence", lambda *_a, **_kw: {"model_calls": 1, "tokens": 4, "error_count": 0, "per_request": {"11": {"model_calls": 1, "tokens": 4}, "12": {"model_calls": 0, "tokens": 0}}})
    with pytest.raises(RuntimeError, match="did not settle"):
        asyncio.run(smoke.wait_for_phoenix_evidence(Path("unused"), 1, channel_id=7, request_ids=[11, 12], identity=IDENTITY, timeout_seconds=0, poll_seconds=0))


def test_provider_evidence_accepts_last_current_authority_extra_calls_and_unrelated(tmp_path):
    spans = [
        _span("turn-1a", request_id=11),
        _span("turn-1b", request_id=11),
        _span("turn-2", request_id=12, authorities=(_authority(7, 11),)),
        _span("unrelated", channel_id=999, request_id=12, provider="fake", model="fake"),
    ]
    evidence = _validated_evidence(tmp_path, spans)
    assert evidence["model_calls"] == 3
    assert evidence["tokens"] > 0
    assert evidence["trace_ids"] == ["turn-1a", "turn-1b", "turn-2"]


@pytest.mark.parametrize(
    "case",
    [
        "unrelated-only", "wrong-request", "suffix-request", "wrong-channel", "one-sided",
        "shared-trace", "bad-thread", "missing-attributes", "malformed-attributes",
        "missing-input", "prior-last", "malformed-last", "zero-tokens", "error",
    ],
)
def test_provider_evidence_structural_failure_matrix(tmp_path, case):
    first = _span("turn-1", request_id=11)
    second = _span("turn-2", request_id=12)
    if case == "unrelated-only":
        first, second = _span("a", channel_id=99, request_id=11), _span("b", channel_id=99, request_id=12)
    elif case == "wrong-request": second = _span("turn-2", request_id=13)
    elif case == "suffix-request": second = _span("turn-2", request_id="12suffix")
    elif case == "wrong-channel": second = _span("turn-2", channel_id=8, request_id=12)
    elif case == "one-sided": second = _span("turn-1b", request_id=11)
    elif case == "shared-trace": second = _span("turn-1", request_id=12)
    elif case == "bad-thread": second = _span("turn-2", request_id=12, thread_id="7:generation:not-this-channel-generation")
    elif case == "missing-attributes": second = _span("turn-2", request_id=12, attributes="")
    elif case == "malformed-attributes": second = _span("turn-2", request_id=12, attributes="{")
    elif case == "missing-input":
        second = _span("turn-2", request_id=12, attributes=json.dumps({"metadata": {"thread_id": THREAD, "ls_provider": "openai", "ls_model_name": "real-model"}}))
    elif case == "prior-last": second = _span("turn-2", request_id=11, authorities=(_authority(7, 12),))
    elif case == "malformed-last": second = _span("turn-2", attributes=json.dumps({"metadata": {"thread_id": THREAD, "ls_provider": "openai", "ls_model_name": "real-model"}, "input": {"value": _authority(7, 12) + " [turn-authority] {bad"}}))
    elif case == "zero-tokens": second = _span("turn-2", request_id=12, prompt=0, completion=0)
    elif case == "error":
        second = _span("turn-2", request_id=12)
        error = _span("turn-2", request_id=12, span_kind="CHAIN", status="ERROR")
    spans = [first, second] + ([error] if case == "error" else [])
    with pytest.raises(RuntimeError):
        _validated_evidence(tmp_path, spans)


@pytest.mark.parametrize("field", ["provider", "model"])
@pytest.mark.parametrize("value", ["wrong", "fake", "null", "mock", "test", ""])
def test_provider_evidence_observed_identity_failure_matrix(tmp_path, field, value):
    changed = {field: value}
    with pytest.raises(RuntimeError):
        _validated_evidence(tmp_path, [_span("turn-1", request_id=11), _span("turn-2", request_id=12, **changed)])


@pytest.mark.parametrize("missing", ["thread_id", "ls_provider", "ls_model_name"])
def test_provider_evidence_missing_metadata_fails(tmp_path, missing):
    metadata = {"thread_id": THREAD, "ls_provider": "openai", "ls_model_name": "real-model"}
    metadata.pop(missing)
    attributes = json.dumps({"metadata": metadata, "input": {"value": _authority(7, 12)}})
    with pytest.raises(RuntimeError):
        _validated_evidence(tmp_path, [_span("turn-1", request_id=11), _span("turn-2", request_id=12, attributes=attributes)])


@pytest.mark.parametrize("wrapper", ["{}", " {} ", "prose {} done", "({})", "**{}**", "```\n{}\n```"])
def test_nonce_accepts_one_exact_delimited_token(wrapper):
    nonce = "SAGE-EXACT-NONCE"
    assert smoke.has_delimited_token(wrapper.format(nonce), nonce)


@pytest.mark.parametrize("answer", [
    "SAGE-EXACT-NONCE-later", "pre-SAGE-EXACT-NONCE", "SAGE-EXACT-NONCEX",
    "XSAGE-EXACT-NONCE", "SAGE-EXACT-NONCE_1", "_SAGE-EXACT-NONCE",
    "sage-exact-nonce", "SAGE-EXACT-NONC",
])
def test_nonce_rejects_collision_case_and_truncation_matrix(answer):
    assert not smoke.has_delimited_token(answer, "SAGE-EXACT-NONCE")


@pytest.mark.parametrize("author_type,metadata", [
    ("system", {}),
    ("operator", {}),
    ("ai", {"special_type": "tool_use", "tool_use": {"status": "completed"}}),
    ("ai", {"special_type": "input_requested", "input_requested": {"status": "pending"}}),
    ("ai", {"special_type": "subagent"}),
    ("ai", {"delegation_id": "generalist:request", "delegation_name": "Generalist"}),
])
def test_assistant_text_rejects_system_operator_and_card_only(author_type, metadata):
    snapshot = _transcript(11, 7, "", "prompt")
    snapshot["messages"][1].update(message="not an ordinary assistant answer", author_type=author_type, metadata=metadata)
    assert smoke._assistant_text(snapshot) == ""


async def _noop():
    return None
