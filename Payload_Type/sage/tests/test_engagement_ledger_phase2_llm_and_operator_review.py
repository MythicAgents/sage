"""Phase 2 group C: the narrow LLM fallback reviewer and the operator's cited `/state set … achieved` promotion.

A completion-function-error task whose output no deterministic locator reads (here a free-form secret) stays pending
under group B. Group C lets two reviewers cite where the secret is: a narrow model reviewer, called only when the
deterministic review found nothing to read, and the operator. Neither supplies a value. Sage re-reads the cited span from
Mythic's task output, validates it by credential type, binds it to the task's target from the output itself, and admits
material only through the same review-backed proof form. The model call runs isolated from the graph's callbacks and
with OpenInference tracing suppressed, so the output it reads and the citation it returns reach no span or chat stream.

Only Mythic's transport and the model provider are stubbed. The account, realm and secret are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from ai.langgraph import credential_review, credential_review_llm, engagement_state, state_reconcile, task_evidence
from tests.esl_fixtures import stub_mythic_operation
from tests.test_engagement_ledger_phase2_unified_proof import (  # noqa: F401 - fixtures are used by name
    ACCOUNT,
    CALLBACK,
    COMPLETION_ERROR,
    DCSYNC_OUTPUT,
    DCSYNC_PARAMS,
    REALM,
    SUCCESS,
    TASK,
    WORKING,
    _events,
    _observations,
    _reader,
    _typed_rows,
    session,
)

SECRET = "Sentinel-Plaintext-7f3c9"
PLAINTEXT = f"cred-material:plaintext:{ACCOUNT}@{REALM}"
OBJECTIVE = f"Obtain the credentials for {ACCOUNT}@{REALM}"
# No deterministic locator reads this output: the secret is free-form text on a line the output binds to the account.
FREEFORM_OUTPUT = (
    "[*] Reading the account's attributes\n"
    f"sAMAccountName : {ACCOUNT}\n"
    f"description    : temporary password {SECRET}\n"
    "password       : (null)\n"
    "[*] done\n"
)
GOOD_CITATION = {"found": True, "type": "plaintext", "account": ACCOUNT, "realm": REALM, "line": 3,
                 "after": "description : temporary password"}
_COMPLETION_ERROR_ROW = {k: v for k, v in COMPLETION_ERROR.items() if k not in {"callback", "responses"}}
_SUCCESS_ROW = {k: v for k, v in SUCCESS.items() if k not in {"callback", "responses"}}


class _ReviewModel(BaseChatModel):
    """A provider stand-in: records what the reviewer sent and replies with fixed text."""

    replies: list = Field(default_factory=list)
    seen: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "credential-review-fixture"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append("\n".join(str(message.content) for message in messages))
        reply = self.replies[min(len(self.seen), len(self.replies)) - 1]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])


def _reviewer(*replies) -> tuple[credential_review_llm.CredentialReviewer, _ReviewModel]:
    model = _ReviewModel(replies=[r if isinstance(r, str) else json.dumps(r) for r in replies])
    return credential_review_llm.CredentialReviewer(model, model_id="review-fixture-model"), model


def _history_task(display_id: int = TASK, params: dict | None = None, row: dict | None = None) -> dict:
    return {"display_id": display_id, "id": display_id, "operation_id": 1, "command_name": "dcsync",
            "original_params": json.dumps(params or DCSYNC_PARAMS), "operator": {"username": "operator"},
            "callback": {"display_id": CALLBACK, "host": "WS01"}, **(row or _COMPLETION_ERROR_ROW)}


def _operation(monkeypatch, output: str = FREEFORM_OUTPUT, tasks: list | None = None, outputs: dict | None = None):
    records = stub_mythic_operation(monkeypatch)
    records["tasks"] = tasks or [_history_task()]
    records["outputs"] = outputs or {TASK: output}
    return records


def _reviews(row) -> list[dict]:
    return [review for contribution in row.payload["contributions"]
            for review in (contribution.get("reviews") or {}).values()]


def _state(tools) -> engagement_state.EngagementState:
    return engagement_state.EngagementState(
        objective=OBJECTIVE, hops=list(tools._ledger_view().hops), engagement_id=tools._eng_key(), runtime_scope=True,
    )


def _database_text(tools) -> str:
    """Every stored ledger row and event, as text, for secret-sentinel checks."""
    connection = sqlite3.connect(tools._ledger_store().path)
    try:
        tables = [name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return "\n".join(str(row) for table in tables for row in connection.execute(f"SELECT * FROM {table}"))
    finally:
        connection.close()


# --- the LLM fallback through task-history reconciliation -------------------------------------------------------

def test_a_free_form_secret_gains_material_through_one_cited_llm_review(session, monkeypatch):
    tools = session()
    _operation(monkeypatch)
    reviewer, model = _reviewer(GOOD_CITATION)
    tools.set_credential_reviewer(reviewer)

    notes = asyncio.run(tools._reconcile_operation([str(TASK)]))  # /state reconcile 18
    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "achieved" and row.payload["satisfied_effects"] == [PLAINTEXT]
    assert _observations(row)["18"]["review"][0] == "admitted"
    [review] = _reviews(row)
    assert (review["verdict"], review["reviewer_kind"]) == (credential_review.VALID, credential_review.REVIEWER_LLM)
    assert review["model"] == "review-fixture-model"
    assert review["prompt_version"] == credential_review.LLM_PROMPT_VERSION
    assert (review["candidate"]["type"], review["candidate"]["account"], review["candidate"]["realm"]) == (
        "plaintext", ACCOUNT, REALM)
    start, end = review["location"]["start"], review["location"]["end"]
    assert FREEFORM_OUTPUT[start:end] == SECRET  # the citation re-reads to the secret in Mythic's output
    assert len(model.seen) == 1
    # The secret reaches only the reviewer's transient prompt: no stored row or event, no reply line.
    assert SECRET not in _database_text(tools) and SECRET not in "\n".join(notes)

    state = _state(_reader())
    assert engagement_state.objective_effects_complete(state)
    assert not state.satisfies_predicate(WORKING)  # material only

    events = _events(tools)
    reloaded = session("reloaded", request=2)
    again, _model = _reviewer(GOOD_CITATION)
    reloaded.set_credential_reviewer(again)
    asyncio.run(reloaded._reconcile_operation([str(TASK)]))  # a cited replay reuses the stored review
    assert _model.seen == [] and _events(tools) == events


def test_without_the_fallback_the_free_form_secret_stays_pending(session, monkeypatch):
    """The guard above can fail: the deterministic review alone finds nothing to read in this output."""
    tools = session()
    _operation(monkeypatch)
    notes = asyncio.run(tools._reconcile_operation([str(TASK)]))
    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "pending" and _reviews(row) == []
    assert any("credential review" in note and "no model reviewer" in note for note in notes)


@pytest.mark.parametrize("output", [
    DCSYNC_OUTPUT,                                                                    # deterministic valid
    DCSYNC_OUTPUT.replace(f"SAM Username         : {ACCOUNT}", "SAM Username         : other.user"),  # rejected
])
def test_a_decisive_deterministic_review_never_calls_the_model(session, monkeypatch, output):
    tools = session()
    _operation(monkeypatch, output)
    reviewer, model = _reviewer(GOOD_CITATION)
    tools.set_credential_reviewer(reviewer)
    asyncio.run(tools._reconcile_operation([str(TASK)]))
    [row] = _typed_rows(_reader())
    assert model.seen == []
    assert {review["reviewer_kind"] for review in _reviews(row)} == {credential_review.REVIEWER_DETERMINISTIC}


def test_an_output_that_never_names_the_account_is_not_sent_to_the_model(session, monkeypatch):
    tools = session()
    _operation(monkeypatch, FREEFORM_OUTPUT.replace(ACCOUNT, "other.user"))
    reviewer, model = _reviewer(GOOD_CITATION)
    tools.set_credential_reviewer(reviewer)
    asyncio.run(tools._reconcile_operation([str(TASK)]))
    [row] = _typed_rows(_reader())
    assert model.seen == [] and row.payload["status"] == "pending" and _reviews(row) == []


def test_the_model_sees_only_a_bounded_excerpt_around_the_account(session, monkeypatch):
    tools = session()
    filler = "".join(f"filler line {index}\n" for index in range(200))
    output = filler + FREEFORM_OUTPUT
    _operation(monkeypatch, output)
    reviewer, model = _reviewer(dict(GOOD_CITATION, line=203))
    tools.set_credential_reviewer(reviewer)
    asyncio.run(tools._reconcile_operation([str(TASK)]))
    [prompt] = model.seen
    assert "filler line 0\n" not in prompt and "filler line 150" not in prompt
    assert f"202| sAMAccountName : {ACCOUNT}" in prompt
    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "achieved"


@pytest.mark.parametrize(("reply", "reason"), [
    (dict(GOOD_CITATION, line=40), "outside"),
    (dict(GOOD_CITATION, after="no such label"), "not on the cited line"),
    (dict(GOOD_CITATION, account="other.user"), "row's account"),
    (dict(GOOD_CITATION, line=4, after="password :"), "placeholder"),
    (dict(GOOD_CITATION, line=2, after="sAMAccountName :"), "account name"),
    (dict(GOOD_CITATION, type="ticket"), "unsupported"),
    (dict(GOOD_CITATION, note=f"the value is {SECRET}"), "contained the candidate value"),
    ({"found": False}, "located no candidate"),
    ("I cannot help with that.", "not a citation"),
])
def test_an_unusable_model_citation_is_stored_rejected_and_never_regraded(session, monkeypatch, reply, reason):
    tools = session()
    _operation(monkeypatch)
    reviewer, model = _reviewer(reply)
    tools.set_credential_reviewer(reviewer)
    asyncio.run(tools._reconcile_operation([str(TASK)]))
    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "pending"
    [review] = _reviews(row)
    assert review["verdict"] == credential_review.REJECTED and reason in review["reason"]
    assert SECRET not in _database_text(tools)
    asyncio.run(tools._reconcile_operation([str(TASK)]))
    assert len(model.seen) == 1  # the stored rejection is reused: no second model call


def test_a_model_error_or_timeout_stores_nothing_and_says_why(session, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    tools = session()
    _operation(monkeypatch)

    class Failing(_ReviewModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError(f"provider echoed {SECRET}")

    tools.set_credential_reviewer(credential_review_llm.CredentialReviewer(Failing(), model_id="failing"))
    notes = asyncio.run(tools._reconcile_operation([str(TASK)]))
    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "pending" and _reviews(row) == []
    assert any("RuntimeError" in note for note in notes) and SECRET not in "\n".join(notes)
    assert SECRET not in caplog.text  # the provider's message is never logged


def test_the_session_reviewer_is_the_base_chat_model_unbound_to_tools():
    from ai.langgraph.model import Model

    base = _ReviewModel()
    reviewer = Model._credential_reviewer(SimpleNamespace(llm=base, provider="openai", model="m-1"))
    assert isinstance(reviewer, credential_review_llm.CredentialReviewer)
    assert reviewer._model is base and reviewer.model_id == "openai:m-1"
    assert Model._credential_reviewer(SimpleNamespace(llm=None, provider="openai", model="m-1")) is None


def test_the_reviewer_refuses_when_environment_tracing_could_capture_the_call(session, monkeypatch):
    tools = session()
    _operation(monkeypatch)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    reviewer, model = _reviewer(GOOD_CITATION)
    tools.set_credential_reviewer(reviewer)
    notes = asyncio.run(tools._reconcile_operation([str(TASK)]))
    [row] = _typed_rows(_reader())
    assert model.seen == [] and row.payload["status"] == "pending" and _reviews(row) == []
    assert any("LangSmith" in note for note in notes)


# --- the review call reaches no span and no chat stream ---------------------------------------------------------

def _messages() -> list:
    return credential_review.llm_messages(f"3| description    : temporary password {SECRET}", ACCOUNT, REALM)


def test_the_review_call_exports_no_span_while_a_direct_call_would(monkeypatch):
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        reviewer, model = _reviewer(GOOD_CITATION)
        asyncio.run(reviewer.locate(_messages()))
        assert len(model.seen) == 1 and exporter.get_finished_spans() == ()

        # Without the reviewer's isolation the same call is traced with the output in it.
        asyncio.run(model.ainvoke([HumanMessage(content=message[1]) for message in _messages()]))
        spans = exporter.get_finished_spans()
        assert spans and any(SECRET in json.dumps(dict(span.attributes or {}), default=str) for span in spans)
    finally:
        instrumentor.uninstrument()


def test_the_review_call_inside_a_graph_node_never_reaches_the_message_stream():
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict

    class State(TypedDict):
        done: bool

    marker = "REPLY-MARKER-0c1d"

    def stream(node) -> str:
        graph = StateGraph(State)
        graph.add_node("review", node)
        graph.add_edge(START, "review")
        graph.add_edge("review", END)

        async def run():
            chunks = []
            async for chunk, _meta in graph.compile().astream({"done": False}, stream_mode="messages"):
                chunks.append(str(getattr(chunk, "content", chunk)))
            return "\n".join(chunks)
        return asyncio.run(run())

    reviewer, model = _reviewer(dict(GOOD_CITATION, after=marker))

    async def isolated(state):
        await reviewer.locate(_messages())
        return {"done": True}

    async def direct(state):
        await model.ainvoke([HumanMessage(content="locate")])
        return {"done": True}

    assert marker not in stream(isolated)
    assert marker in stream(direct)  # the guard can fail: a plain call in a node is streamed to chat


# --- the operator's cited promotion ---------------------------------------------------------------------------

def _slash(tools, arg: str) -> str:
    from sage_chat import slash
    from sage_chat.headless import build_chat_request

    model = SimpleNamespace(mythic_client=tools, mode="supervised", provider="t", model="t", state={})
    return asyncio.run(slash._handle_state(model, build_chat_request("x", channel_id=6, operation_id=0), arg))


def _notice(reply: str) -> str:
    """The command's own reply line, before the rendered ledger."""
    return reply.split("\n\n", 1)[0]


def _pending_row(session, monkeypatch, **operation):
    tools = session()
    records = _operation(monkeypatch, **operation)
    asyncio.run(tools._reconcile_operation([str(TASK)]))  # no reviewer in this session: the row stays pending
    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "pending"
    return tools, row, records


def _promotion(number: int, **fields) -> str:
    values = {"task": TASK, "type": "plaintext", "account": f"{ACCOUNT}@{REALM}", "line": 3,
              "after": "description : temporary password", **fields}
    after = values.pop("after")
    return (f"set {number} achieved " + " ".join(f"{key}={value}" for key, value in values.items())
            + f" after={after}")


def test_the_operator_promotes_material_by_citing_where_the_secret_is(session, monkeypatch):
    tools, row, _records = _pending_row(session, monkeypatch)
    reply = _slash(tools, _promotion(row.number))
    assert "promoted" in reply.lower() and SECRET not in reply

    [promoted] = _typed_rows(_reader())
    assert promoted.number == row.number
    assert promoted.payload["status"] == "achieved" and promoted.payload["satisfied_effects"] == [PLAINTEXT]
    assert _observations(promoted)["18"]["operator"][0] == "admitted"
    [review] = _reviews(promoted)
    assert (review["verdict"], review["reviewer_kind"]) == (credential_review.VALID, credential_review.REVIEWER_OPERATOR)
    assert "identity-unavailable" in review["reviewer_id"]
    events = tools._ledger_store().history(tools._require_ledger_binding())
    assert events[-1]["actor_kind"] == "operator"
    assert events[-1]["actor_name"] == "operator (identity unavailable in chat request)"
    assert SECRET not in _database_text(tools)
    state = _state(_reader())
    assert engagement_state.objective_effects_complete(state) and not state.satisfies_predicate(WORKING)


@pytest.mark.parametrize(("fields", "reason"), [
    ({"line": 40}, "outside"),
    ({"after": "no such label"}, "not on the cited line"),
    ({"line": 4, "after": "password :"}, "placeholder"),
    ({"account": f"other.user@{REALM}"}, "the cited account is not row #1's account"),
    ({"type": "ticket"}, "unsupported"),
    ({"task": 19}, "no such task"),
])
def test_an_invalid_operator_citation_changes_nothing_and_says_why(session, monkeypatch, fields, reason):
    tools, row, _records = _pending_row(session, monkeypatch)
    events = _events(tools)
    notice = _notice(_slash(tools, _promotion(row.number, **fields)))
    assert "not promoted" in notice and reason in notice
    assert _events(tools) == events and _typed_rows(_reader())[0].payload["status"] == "pending"


_CITED = f"type=plaintext account={ACCOUNT}@{REALM} line=3"


@pytest.mark.parametrize(("arg", "reason"), [
    ("set 1 achieved", "needs `task=`"),                                         # no citation
    ("set 1 pending task=18", "Only `achieved`"),                                # only `achieved` exists
    (f"set 1 achieved task=18 {_CITED}", "needs `task=`"),                       # no location text
    (f"set 1 achieved task=18 {_CITED} value={SECRET} after=x", "Only one each"),  # no value field
    (f"set 1 achieved task=18 task=19 {_CITED} after=x", "Only one each"),       # one task only
])
def test_a_malformed_promotion_is_refused_before_anything_is_read(session, monkeypatch, arg, reason):
    tools, _row, records = _pending_row(session, monkeypatch)
    queries, events = records["queries"], _events(tools)
    reply = _slash(tools, arg)
    assert reason in _notice(reply) and "`/state set" in reply and SECRET not in reply
    assert records["queries"] == queries and _events(tools) == events


@pytest.mark.parametrize(("output", "reason"), [
    (DCSYNC_OUTPUT, "only a completion-function error"),   # terminal success: the working path, never promoted
    (FREEFORM_OUTPUT, "proves no account secret"),         # terminal success without a credited secret
])
def test_the_operator_cannot_promote_a_task_outside_the_completion_error_class(session, monkeypatch, output, reason):
    tasks = [_history_task(), _history_task(19, row=dict(_SUCCESS_ROW))]
    tools, row, _records = _pending_row(session, monkeypatch, tasks=tasks,
                                        outputs={TASK: FREEFORM_OUTPUT, 19: output})
    events = _events(tools)
    notice = _notice(_slash(tools, _promotion(row.number, task=19)))
    assert "not promoted" in notice and reason in notice
    assert _events(tools) == events


def test_the_operator_cannot_cite_a_task_for_another_effect(session, monkeypatch):
    other = dict(DCSYNC_PARAMS, User="north\\other.user")
    tasks = [_history_task(), _history_task(19, params=other)]
    tools, row, _records = _pending_row(session, monkeypatch, tasks=tasks,
                                        outputs={TASK: FREEFORM_OUTPUT, 19: FREEFORM_OUTPUT})
    notice = _notice(_slash(tools, _promotion(row.number, task=19)))
    assert "not promoted" in notice and "task 19's effect is not row #1's effect" in notice


def test_an_achieved_row_is_not_promoted_again(session, monkeypatch):
    tools = session()
    _operation(monkeypatch)
    reviewer, _model = _reviewer(GOOD_CITATION)
    tools.set_credential_reviewer(reviewer)
    asyncio.run(tools._reconcile_operation([str(TASK)]))
    [row] = _typed_rows(_reader())
    events = _events(tools)
    notice = _notice(_slash(tools, _promotion(row.number)))
    assert "not promoted" in notice and "already achieved" in notice and _events(tools) == events


# --- the shared citation check ------------------------------------------------------------------------------------

def _cite(output: str = FREEFORM_OUTPUT, forbidden: str = "", **fields) -> credential_review.ReviewOutcome:
    citation = credential_review.Citation(**{
        "credential_type": "plaintext", "account": ACCOUNT, "realm": REALM, "line": 3,
        "after": "description : temporary password", **fields})
    return credential_review.review_citation(
        output, citation, task_id=TASK, canonical_effect=f"account-secret:{ACCOUNT}@{REALM}", account=ACCOUNT,
        realm=REALM, reviewer_kind=credential_review.REVIEWER_OPERATOR, reviewer_id="operator:test",
        forbidden_text=forbidden,
    )


@pytest.mark.parametrize(("output", "verdict"), [
    (FREEFORM_OUTPUT, credential_review.VALID),
    (FREEFORM_OUTPUT.replace(f"sAMAccountName : {ACCOUNT}", f"sAMAccountName : north\\{ACCOUNT}"),
     credential_review.VALID),
    (FREEFORM_OUTPUT.replace(f"sAMAccountName : {ACCOUNT}", f"sAMAccountName : south\\{ACCOUNT}"),
     credential_review.REJECTED),
    ("Domain : SOUTH\n" + FREEFORM_OUTPUT, credential_review.REJECTED),
    (f"sAMAccountName : {ACCOUNT}\n" + "".join(f"x{index}\n" for index in range(6))
     + f"description    : temporary password {SECRET}\n", credential_review.REJECTED),  # account named too far above
])
def test_a_citation_binds_to_the_target_from_the_output_itself(output, verdict):
    line = next(index for index, text in enumerate(output.split("\n"), 1) if "temporary password" in text)
    assert _cite(output, line=line).verdict == verdict


def test_a_hex_citation_uses_the_type_validator():
    output = f"SAM Username : {ACCOUNT}\nNT digest => {'0123456789abcdef' * 2}\n"
    ok = _cite(output, credential_type="ntlm", line=2, after="NT digest =>")
    assert ok.verdict == credential_review.VALID and ok.material_kind == "hash"
    short = _cite(output.replace("0123456789abcdef" * 2, "0123456789abcdef"), credential_type="ntlm", line=2,
                  after="NT digest =>")
    assert short.verdict == credential_review.REJECTED


def test_a_citation_whose_value_appears_in_the_claimant_text_is_rejected():
    outcome = _cite(forbidden=f"after=temporary password {SECRET}")
    assert outcome.verdict == credential_review.REJECTED and "contained the candidate value" in outcome.record["reason"]
    assert SECRET not in json.dumps(outcome.record)


def test_a_model_citation_of_a_line_it_was_not_shown_is_rejected():
    outcome = credential_review.review_llm_response(
        FREEFORM_OUTPUT, json.dumps(GOOD_CITATION), task_id=TASK, canonical_effect=f"account-secret:{ACCOUNT}@{REALM}",
        account=ACCOUNT, realm=REALM, model="m", shown_lines=frozenset({1, 2}),
    )
    assert outcome.verdict == credential_review.REJECTED and "excerpt" in outcome.record["reason"]


# --- cold-review regressions: a free-form citation the output does not support ---------------------------------

# A replication the DC refused: the account is named, no secret was produced.
DENIED_OUTPUT = (
    f"[DC] '{REALM}' will be the domain\n"
    f"[DC] 'north\\{ACCOUNT}' will be the user account\n"
    "ERROR kuhl_m_lsadump_dcsync ; GetNCChanges: 0x000020f7 (8439)\n"
)


@pytest.mark.parametrize(("output", "citation", "reason"), [
    (DENIED_OUTPUT, dict(GOOD_CITATION, line=3, after="0x000020f7"), "reports an error"),
    (DENIED_OUTPUT, dict(GOOD_CITATION, line=3, after="ERROR"), "single token"),
    (FREEFORM_OUTPUT, dict(GOOD_CITATION, line=2, after="sAMAccountName"), "single token"),
    (FREEFORM_OUTPUT, dict(GOOD_CITATION, line=2, after=""), "no text before the value"),
    (FREEFORM_OUTPUT.replace("description", f"sAMAccountName : other.user\ndescription"),
     dict(GOOD_CITATION, line=4), "another account"),
])
def test_a_model_citation_the_output_does_not_support_proves_nothing(session, monkeypatch, output, citation, reason):
    tools = session()
    _operation(monkeypatch, output)
    reviewer, _model = _reviewer(citation)
    tools.set_credential_reviewer(reviewer)
    asyncio.run(tools._reconcile_operation([str(TASK)]))
    [row] = _typed_rows(_reader())
    assert row.payload["status"] == "pending" and not engagement_state.objective_effects_complete(_state(_reader()))
    [review] = _reviews(row)
    assert review["verdict"] == credential_review.REJECTED and reason in review["reason"]


def test_a_free_form_review_stores_no_value_hash(session, monkeypatch):
    tools = session()
    _operation(monkeypatch)
    reviewer, _model = _reviewer(GOOD_CITATION)
    tools.set_credential_reviewer(reviewer)
    asyncio.run(tools._reconcile_operation([str(TASK)]))
    [review] = _reviews(_typed_rows(_reader())[0])
    assert review["verdict"] == credential_review.VALID and review["candidate"]["value_sha256"] == ""


def test_a_line_cut_short_in_the_excerpt_cannot_be_cited():
    long_line = f"description    : temporary password {SECRET}" + " " * 1200 + "tail"
    text, shown = credential_review.llm_excerpt(f"sAMAccountName : {ACCOUNT}\n{long_line}\n", ACCOUNT)
    assert 1 in shown and 2 not in shown and "tail" not in text


def test_a_host_dump_task_with_a_completion_error_adds_no_row(session, monkeypatch):
    tools = session()
    task = _history_task(params={"command": "sekurlsa::logonpasswords"})
    task["command_name"] = "mimikatz"
    _operation(monkeypatch, tasks=[task], outputs={TASK: FREEFORM_OUTPUT})
    notes = asyncio.run(tools._reconcile_operation([str(TASK)]))
    assert list(tools._ledger_snapshot().hop_rows()) == [] and "- 18: not added" in notes[0]


@pytest.mark.parametrize(("name", "value"), [("SAGE_OBSERVATION_DIRECTORY", "/tmp/capture"), ("OPENAI_LOG", "debug")])
def test_the_reviewer_refuses_when_request_bodies_would_be_recorded(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    reviewer, model = _reviewer(GOOD_CITATION)
    with pytest.raises(credential_review_llm.ReviewerUnavailable):
        asyncio.run(reviewer.locate(_messages()))
    assert model.seen == []
