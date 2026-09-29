"""Offline checks for the non-secret Trust Walker model-matrix contract."""

import json

import pytest

from ai.hillclimb.trust_walker_pilot import (
    MATRIX_SCHEMA,
    MatrixContract,
    MatrixRoute,
    PilotContract,
    PilotError,
    load_matrix_contract,
)


def _record():
    return {
        "schema": MATRIX_SCHEMA,
        "routes": [
            {
                "route_id": "qwen32-openrouter",
                "provider": "openai",
                "model": "qwen/qwen3-32b",
                "api_endpoint": "https://openrouter.ai/api/v1",
            },
            {
                "route_id": "local-ollama",
                "provider": "openai",
                "model": "qwen3:8b",
                "api_endpoint": "http://127.0.0.1:11434/v1",
            },
        ],
    }


def _load(tmp_path, record):
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return load_matrix_contract(path)


def test_load_select_match_and_identity(tmp_path):
    contract = _load(tmp_path, _record())
    route = contract.route_for("qwen32-openrouter")
    assert route.model == "qwen/qwen3-32b"
    assert contract.validate_resolved_route(
        "qwen32-openrouter",
        {"provider": "openai", "model": route.model, "api_endpoint": route.api_endpoint, "api_key": "secret"},
    ) == route
    assert contract.to_record() == _record()
    assert len(contract.identity) == 64
    assert contract.identity == _load(tmp_path, _record()).identity
    assert "secret" not in json.dumps(contract.to_record())
    assert "secret" not in repr(contract)
    PilotContract().validate()  # Existing v4 behavior remains usable.


@pytest.mark.parametrize("change", [
    {"provider": "other"},
    {"model": "qwen/qwen3-8b"},
    {"api_endpoint": "https://openrouter.ai/api/v1/"},
])
def test_resolved_route_must_match_exactly(tmp_path, change):
    contract = _load(tmp_path, _record())
    actual = {"provider": "openai", "model": "qwen/qwen3-32b", "api_endpoint": "https://openrouter.ai/api/v1"}
    actual.update(change)
    with pytest.raises(PilotError, match="differs from contract"):
        contract.validate_resolved_route("qwen32-openrouter", actual)
    with pytest.raises(PilotError, match="unknown matrix route_id"):
        contract.route_for("absent")


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(api_key="secret"),
    lambda d: d.update(schema="unknown"),
    lambda d: d.update(routes=[]),
    lambda d: d["routes"][0].update(api_key="secret"),
    lambda d: d["routes"][1].update(route_id="qwen32-openrouter"),
    lambda d: d["routes"][0].update(api_endpoint="https://user:password@openrouter.ai/api/v1"),
    lambda d: d["routes"][0].update(api_endpoint="https://openrouter.ai/api/v1?key=secret"),
    lambda d: d["routes"][0].update(api_endpoint="ftp://openrouter.ai/api/v1"),
    lambda d: d["routes"][0].update(route_id="Bad Route"),
    lambda d: d["routes"][0].update(model=" qwen/qwen3-32b"),
])
def test_reject_malformed_or_secret_bearing_contract(tmp_path, mutation):
    record = _record()
    mutation(record)
    with pytest.raises(PilotError):
        _load(tmp_path, record)


def test_loader_rejects_invalid_json_and_missing_file(tmp_path):
    path = tmp_path / "matrix.json"
    with pytest.raises(PilotError, match="cannot read matrix contract"):
        load_matrix_contract(path)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(PilotError, match="cannot read matrix contract"):
        load_matrix_contract(path)


def test_one_route_is_a_valid_generic_contract():
    route = MatrixRoute(**_record()["routes"][0])
    contract = MatrixContract(schema=MATRIX_SCHEMA, routes=(route,))
    contract.validate()
