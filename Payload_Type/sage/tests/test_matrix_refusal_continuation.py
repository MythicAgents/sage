"""Completed refusals terminate both arms and remain replayable outcomes."""
import asyncio
import json
from types import SimpleNamespace

import langchain.chat_models

from ai.hillclimb import bare_runner, live_seams, trust_walker_pilot as pilot
from ai.langgraph import autonomous_controller as controller, policy
from .test_matrix_timeout_continuation import _pair


def _message(content, *, refusal=None, tool_calls=None):
    return SimpleNamespace(
        content=content,
        additional_kwargs={"refusal": refusal} if refusal is not None else {},
        response_metadata={"model_provider": "openai", "model_name": "test-model"},
        usage_metadata={"input_tokens": 7, "output_tokens": 5, "total_tokens": 12},
        tool_calls=tool_calls or [], invalid_tool_calls=[],
    )


def test_raw_structured_refusal_preempts_tool_and_retains_cost():
    message = _message("", refusal="I cannot assist with that request.",
                       tool_calls=[{"name": "issue_command", "args": {"command": "whoami"}}])
    decision = live_seams.parse_model_decision(message)
    decision["_usage"] = message.usage_metadata
    dispatched = []
    result = bare_runner.BareModelRunner(
        lambda *_: decision, lambda call: dispatched.append(call),
    ).run("objective")
    assert result.termination == {"cause": "explicit_terminal_decision", "subtype": "refusal"}
    assert result.final_text == "I cannot assist with that request."
    assert result.total_tokens == 12
    assert result.model_calls == 1 and dispatched == []


def test_raw_factory_preserves_openai_refusal_usage_and_backend(monkeypatch):
    response = _message("", refusal="I cannot assist with that request.")
    calls = []

    class Bound:
        def bind_tools(self, specs):
            assert specs == [{"name": "issue_command"}]
            return self

        def invoke(self, messages):
            calls.append(messages)
            return response

    monkeypatch.setattr(live_seams, "configure_sage_ssl_cert_file", lambda: "")
    monkeypatch.setattr(langchain.chat_models, "init_chat_model", lambda **_: Bound())
    model = live_seams.make_model_fn("openai", "test-model", objective="objective")
    decision = model("system", [{"name": "issue_command"}], [])
    assert len(calls) == 1
    assert decision["final"] == "I cannot assist with that request."
    assert decision["_refusal"] == {"source": "additional_kwargs.refusal"}
    assert decision["_usage"] == response.usage_metadata
    assert decision["_backend"]["effective_backend"] == "openai:test-model"


def test_refusal_classifier_keeps_tool_and_near_match_distinct():
    tool = _message("I cannot assist with that request.", tool_calls=[{"name": "list_callbacks", "args": {}}])
    assert live_seams.parse_model_decision(tool)["tool"] == "list_callbacks"
    for content in (
        'The prior output said "I cannot assist with that request."',
        "I cannot execute this because the server is offline.",
        "I cannot provide the result because the tool failed.",
        "STUCK",
    ):
        assert "_refusal" not in live_seams.parse_model_decision(_message(content))
    assert live_seams.parse_model_decision(_message("I'm sorry, but I can't help with that request."))["_refusal"] == {
        "source": "content.text",
    }


def test_native_policy_refusal_ends_controller_without_execute():
    response = _message("", refusal="I cannot assist with that request.")
    calls = []
    candidate = SimpleNamespace(name="test-action", target="target", preconditions=[], effects=["effect"], reason="")
    state = SimpleNamespace(achieved_effects=lambda: set())
    backend = policy.LLMPolicy(
        lambda _request: calls.append("model") or response,
        provider="openai", model_id="test-model",
        catalog=[{"name": "test-action", "description": "test"}],
        expected_response_provider="openai", expected_response_model="test-model",
    )
    kernel = controller.AutonomousController(
        observe=lambda: state,
        execute=lambda *_: calls.append("execute") or {"ok": True},
        frontier_fn=lambda _: [candidate], policy_backend=backend,
        objective="test", episode_id="episode-refusal",
    )
    result = asyncio.run(kernel.run())
    assert result.status == controller.STATUS_REFUSAL
    assert calls == ["model"]
    decision = result.decisions[0]
    assert decision["disposition"] == "refusal"
    assert decision["raw_response"] == "I cannot assist with that request."
    assert decision["token_usage"] == {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12}


def test_native_route_mismatch_precedes_refusal():
    response = _message("", refusal="I cannot assist with that request.")
    response.response_metadata["model_name"] = "wrong-model"
    backend = policy.LLMPolicy(
        lambda _: response,
        catalog=[{"name": "test-action", "description": "test"}],
        expected_response_provider="openai", expected_response_model="test-model",
    )
    candidate = SimpleNamespace(name="test-action", target="target", preconditions=[], effects=["effect"], reason="")
    state = SimpleNamespace(achieved_effects=lambda: set())
    decision = asyncio.run(backend.select(
        episode_id="episode-mismatch", objective="test", state=state,
        candidates=[candidate], history=[],
    ))
    assert decision.disposition == "stop"
    assert "route mismatch" in decision.rationale


def test_refusal_pair_continues_and_replay_rechecks_evidence(tmp_path, monkeypatch):
    contract, root, deps, events = _pair(tmp_path, monkeypatch, policy=False)
    contract = pilot.MatrixPairContract(**{**contract.__dict__, "continue_after_refusal": True})
    original_run = deps.run_arm
    deps.run_arm = lambda arm, start, progress: {
        **original_run(arm, start, progress),
        "termination": "halted_refusal" if arm == "shipping-sage" else {
            "cause": "explicit_terminal_decision", "subtype": "refusal",
        },
    }
    report = pilot.run_pair(contract, root, deps)
    assert report["run_complete"] is True
    assert report["comparison_ready"] is False
    assert report["lifecycle_complete"] is False
    assert [event[0] for event in events] == ["run", "settle", "observe", "cleanup"] * 2
    rows = pilot.read_records(root / "pilot.jsonl")
    assert all(row["refusal_continuation_eligible"] for row in rows if row.get("record_type") == "arm_result")
    assert pilot.summarize_pair(rows, contract, artifact_dir=root)["run_complete"] is True
    (root / "shipping-sage-native-observation.json").write_text(json.dumps({}))
    assert pilot.summarize_pair(rows, contract, artifact_dir=root)["run_complete"] is False


def test_legacy_refusal_does_not_advance_unsettled_pair(tmp_path, monkeypatch):
    contract, root, deps, events = _pair(tmp_path, monkeypatch, policy=False)
    original_run = deps.run_arm
    deps.run_arm = lambda arm, start, progress: {**original_run(arm, start, progress), "termination": "halted_refusal"}
    report = pilot.run_pair(contract, root, deps)
    assert report["run_complete"] is False
    assert ("cleanup", "shipping-sage") not in events
