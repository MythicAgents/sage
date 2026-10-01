"""Offline checks for the operator's one-request matrix route receipt producer."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills/sage-eval-gauge/scripts/matrix_route_smoke.py"
spec = importlib.util.spec_from_file_location("matrix_route_smoke", SCRIPT)
assert spec and spec.loader
smoke = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smoke
spec.loader.exec_module(smoke)


def route() -> dict[str, str]:
    return {
        "provider": "openai", "model": "test/model", "api_endpoint": "https://openrouter.ai/api/v1",
        "api_key": "test-private-key",
    }


def test_offline_recipe_makes_no_provider_call(monkeypatch, tmp_path, capsys):
    from ai.hillclimb.trust_walker_pilot import MatrixRoute

    selected = MatrixRoute("test", "openai", "test/model", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(smoke, "configured_route", lambda *_: (selected, route()))
    monkeypatch.setattr(smoke, "probe_catalog", lambda *_: pytest.fail("unexpected catalog query"))
    monkeypatch.setattr(smoke, "run_smoke", lambda *_: pytest.fail("unexpected model request"))
    assert smoke.main(["--route-id", "test", "--output-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["execute_flag_required"] is True
    assert list(tmp_path.iterdir()) == []


def test_catalog_probe_uses_zdr_filter_and_does_not_infer_provider(monkeypatch):
    import httpx

    calls = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"id": "test/model"}]}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setattr(smoke, "configure_sage_ssl_cert_file", lambda: "")
    monkeypatch.setattr(httpx, "Client", Client)
    result = smoke.probe_catalog(route())
    assert result["reachable"] is True and result["listed"] is True
    assert result["inference_calls"] == 0 and result["zdr_query"] is True
    assert calls[0][0] == "https://openrouter.ai/api/v1/models"
    assert calls[0][1]["params"] == {"zdr": "true"}


def test_receipt_uses_response_backend_metadata_and_pilot_accepts_it(monkeypatch, tmp_path):
    from ai.hillclimb.trust_walker_pilot import MatrixRoute

    selected = MatrixRoute("test", "openai", "test/model", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(smoke, "PRIVATE_ROOT", tmp_path)
    monkeypatch.setattr(smoke, "configured_route", lambda *_: (selected, route()))
    monkeypatch.setattr(smoke, "probe_catalog", lambda *_: {
        "provider": "openai", "model": "test/model", "endpoint_host": "openrouter.ai",
        "endpoint_port": 443, "endpoint_path": "/api/v1", "reachable": True,
        "listed": True, "inference_calls": 0, "zdr_query": True,
    })
    metadata = {
        "accounting": "pre_arm_smoke", "configured_provider": "openai",
        "configured_model": "test/model",
        "observed_backend": {
            "backend_provenance_source": "response_metadata.model_provider+response_metadata.model_name",
            "effective_backend": "openai:test/model", "observed_provider": "openai",
            "observed_model": "test/model",
        },
        "usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
    }
    monkeypatch.setattr(smoke.live_seams, "model_route_auth_smoke", lambda *_, **__: (True, json.dumps(metadata)))
    assert smoke.execute(Path("unused"), "test", tmp_path)["ok"] is True
    assert smoke.pilot._validate_route_smoke(tmp_path / smoke.RECEIPT_NAME, route(), selected)["usage"]["total_tokens"] == 10
    assert (tmp_path / smoke.RECEIPT_NAME).stat().st_mode & 0o777 == 0o600
    with pytest.raises(smoke.RouteSmokeError, match="already exists"):
        smoke.execute(Path("unused"), "test", tmp_path)


def test_mismatched_backend_is_preserved_as_failed_receipt(monkeypatch, tmp_path):
    from ai.hillclimb.trust_walker_pilot import MatrixRoute

    selected = MatrixRoute("test", "openai", "test/model", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(smoke, "PRIVATE_ROOT", tmp_path)
    monkeypatch.setattr(smoke, "configured_route", lambda *_: (selected, route()))
    monkeypatch.setattr(smoke, "probe_catalog", lambda *_: {
        "provider": "openai", "model": "test/model", "endpoint_host": "openrouter.ai",
        "endpoint_port": 443, "endpoint_path": "/api/v1", "reachable": True,
        "listed": True, "inference_calls": 0, "zdr_query": True,
    })
    observed = {
        "configured_provider": "openai", "configured_model": "test/model",
        "observed_backend": {"effective_backend": "openai:other", "observed_provider": "openai", "observed_model": "other"},
        "usage": {"total_tokens": 3},
    }
    monkeypatch.setattr(smoke.live_seams, "model_route_auth_smoke", lambda *_, **__: (True, json.dumps(observed)))
    assert smoke.execute(Path("unused"), "test", tmp_path)["ok"] is False
    retained = json.loads((tmp_path / smoke.RECEIPT_NAME).read_text())
    assert retained["detail"]["observed_backend"]["observed_model"] == "other"
    assert retained["error"] == "backend_or_usage_mismatch"
    with pytest.raises(smoke.pilot.PilotError):
        smoke.pilot._validate_route_smoke(tmp_path / smoke.RECEIPT_NAME, route(), selected)


def test_configured_route_rejects_wrong_active_model_before_provider(monkeypatch, tmp_path):
    from ai.hillclimb.trust_walker_pilot import MatrixRoute

    selected = MatrixRoute("test", "openai", "test/model", "https://openrouter.ai/api/v1")
    class Contract:
        def route_for(self, route_id):
            assert route_id == "test"
            return selected

    monkeypatch.setattr(smoke.pilot, "load_matrix_contract", lambda *_: Contract())
    monkeypatch.setattr(smoke.orchestrate, "_load_sage_model_environment", lambda: {
        "provider": "openai", "model": "wrong/model", "API_ENDPOINT": selected.api_endpoint,
        "API_KEY": "test-private-key",
    })
    with pytest.raises(smoke.pilot.PilotError, match="configured model differs"):
        smoke.execute(tmp_path / "contract.json", "test", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_public_output_path_rejected_before_provider(monkeypatch, tmp_path):
    from ai.hillclimb.trust_walker_pilot import MatrixRoute

    selected = MatrixRoute("test", "openai", "test/model", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(smoke, "configured_route", lambda *_: (selected, route()))
    monkeypatch.setattr(smoke, "probe_catalog", lambda *_: pytest.fail("unexpected provider query"))
    with pytest.raises(smoke.RouteSmokeError, match="private .sage_history"):
        smoke.execute(Path("unused"), "test", tmp_path)
