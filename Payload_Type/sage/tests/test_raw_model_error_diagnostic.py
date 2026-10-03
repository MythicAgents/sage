"""Provider failures remain diagnosable after raw checkpoint completion without leaking response text."""

import json

import httpx
import langchain.chat_models

from ai.hillclimb import bare_runner, live_seams


def test_sdk_429_is_retained_in_raw_checkpoint_and_reload(tmp_path, monkeypatch):
    secret = "synthetic-secret-never-retain"
    observed = []

    def respond(request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(429, headers={"x-request-id": "req_abc12345", "retry-after": "3",
                                            "x-private": secret},
                              json={"error": {"message": secret, "code": "rate_limit_exceeded"}})

    original = langchain.chat_models.init_chat_model
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(langchain.chat_models, "init_chat_model",
                            lambda **kwargs: original(**kwargs, http_client=client))
        model = live_seams.make_model_fn("openai", "offline-model", api_key="offline-placeholder",
                                         base_url="https://example.invalid/v1", max_retries=0)
        path = tmp_path / "checkpoint.json"
        runner = bare_runner.BareModelRunner(
            model, lambda _: (_ for _ in ()).throw(AssertionError("no tool")),
            checkpoint_path=path, checkpoint_binding={"route": "offline"},
            event_sink=observed.append,
        )
        result = runner.run("offline objective")

    expected = {"error_type": "RateLimitError", "provider_code": "rate_limit_exceeded",
                "http_status": 429, "request_id": "req_abc12345", "retry_after_seconds": 3.0}
    assert result.stopped == "error"
    assert result.termination == {"cause": "other_declared_boundary", "subtype": "model_error"}
    assert result.model_error_diagnostic == expected
    assert result.model_attempts == 1 and result.model_calls == 0
    assert observed[-1] == {"event": "model_request_failed", "model_attempt": 1,
                            "diagnostic": expected}
    saved = json.loads(path.read_text())
    assert saved["phase"] == "completed"
    assert saved["result"]["model_error_diagnostic"] == expected
    assert secret not in path.read_text()
    resumed = bare_runner.BareModelRunner(
        lambda *_: (_ for _ in ()).throw(AssertionError("no retry")),
        lambda _: (_ for _ in ()).throw(AssertionError("no tool")),
        checkpoint_path=path, checkpoint_binding={"route": "offline"}, resume=True,
    )
    assert resumed.run("offline objective").model_error_diagnostic == expected


def test_sdk_400_cyber_policy_is_retained_without_response_text_or_tool_dispatch(tmp_path, monkeypatch):
    secret = "synthetic-secret-never-retain"
    observed = []
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            400,
            headers={"x-request-id": "req_abc12345", "x-private": secret},
            json={"error": {"message": secret, "type": "invalid_request_error",
                            "code": "cyber_policy"}},
        )

    original = langchain.chat_models.init_chat_model
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(langchain.chat_models, "init_chat_model",
                            lambda **kwargs: original(**kwargs, http_client=client))
        model = live_seams.make_model_fn("openai", "offline-model", api_key="offline-placeholder",
                                         base_url="https://example.invalid/v1", max_retries=0)
        path = tmp_path / "checkpoint.json"
        result = bare_runner.BareModelRunner(
            model, lambda _: (_ for _ in ()).throw(AssertionError("no tool")),
            checkpoint_path=path, checkpoint_binding={"route": "offline"},
            event_sink=observed.append,
        ).run("offline objective")

    expected = {"error_type": "BadRequestError", "provider_code": "cyber_policy",
                "http_status": 400, "request_id": "req_abc12345"}
    assert len(requests) == 1
    assert result.stopped == "error"
    assert result.model_attempts == 1 and result.model_calls == 0
    assert result.transcript == []
    assert result.model_error_diagnostic == expected
    assert observed[-1] == {"event": "model_request_failed", "model_attempt": 1,
                            "diagnostic": expected}
    assert json.loads(path.read_text())["result"]["model_error_diagnostic"] == expected
    assert secret not in path.read_text()
    resumed = bare_runner.BareModelRunner(
        lambda *_: (_ for _ in ()).throw(AssertionError("no retry")),
        lambda _: (_ for _ in ()).throw(AssertionError("no tool")),
        checkpoint_path=path, checkpoint_binding={"route": "offline"}, resume=True,
    )
    assert resumed.run("offline objective").model_error_diagnostic == expected


def test_untrusted_provider_fields_are_dropped_and_old_checkpoint_loads(tmp_path):
    secret = "synthetic-secret-never-retain"

    class BadRequestError(RuntimeError):
        diagnostic = {"error_type": "RateLimitError", "provider_code": secret,
                      "http_status": True, "request_id": secret,
                      "retry_after_seconds": 999999, "message": secret}

    path = tmp_path / "checkpoint.json"
    result = bare_runner.BareModelRunner(
        lambda *_: (_ for _ in ()).throw(BadRequestError(secret)), lambda _: "unused",
        checkpoint_path=path, checkpoint_binding={"route": "offline"},
    ).run("offline objective")
    assert result.model_error_diagnostic == {"error_type": "RateLimitError"}
    assert secret not in path.read_text()

    saved = json.loads(path.read_text())
    saved["result"].pop("model_error_diagnostic")
    path.write_text(json.dumps(saved))
    legacy = bare_runner.BareModelRunner(
        lambda *_: (_ for _ in ()).throw(AssertionError("no retry")), lambda _: "unused",
        checkpoint_path=path, checkpoint_binding={"route": "offline"}, resume=True,
    ).run("offline objective")
    assert legacy.model_error_diagnostic is None


def test_near_match_cyber_policy_code_is_not_retained():
    class BadRequestError(RuntimeError):
        code = "cyber_policy_detail"
        status_code = 400

    failure = BadRequestError("synthetic-secret-never-retain")
    code = live_seams._typed_provider_error_code(failure)
    adapter_diagnostic = live_seams._request_error_diagnostic(failure, code)
    result = bare_runner.BareModelRunner(
        lambda *_: (_ for _ in ()).throw(live_seams.ModelRequestBoundaryError(
            error_type="BadRequestError", provider_code=code, diagnostic=adapter_diagnostic,
        )),
        lambda _: (_ for _ in ()).throw(AssertionError("no tool")),
    ).run("offline objective")
    assert result.model_error_diagnostic == {"error_type": "BadRequestError", "http_status": 400}


def test_bounded_provider_timeout_retains_local_type_only():
    result = bare_runner.BareModelRunner(
        lambda *_: (_ for _ in ()).throw(TimeoutError("synthetic-secret-never-retain")),
        lambda _: "unused", timeout=30, provider_timeout_is_wall=False,
    ).run("offline objective")
    assert result.stopped == "error"
    assert result.model_error_diagnostic == {"error_type": "TimeoutError"}
