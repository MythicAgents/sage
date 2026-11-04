"""`chat_sessions.py create` gives a container Sage its BloodHound settings.

A local tmux Sage's BloodHound MCP loads its own `.env` at startup. A container Sage runs the MCP baked
into the image, which has no credentials, so the channel configuration must carry them. Operators typed
them by hand three times on 2026-11-03 (once as `http:/127.0.0.1:8083`). The values come from the MCP
checkout's own `.env`, found through Sage's runtime lookup, never a host path written into the tool.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "chat_sessions.py"
SPEC = importlib.util.spec_from_file_location("chat_sessions_under_test", SCRIPT)
chat_sessions = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = chat_sessions
SPEC.loader.exec_module(chat_sessions)

SECRET = "token-key-that-must-never-be-printed"


def _mcp(tmp_path: Path, monkeypatch, lines: str) -> Path:
    mcp = tmp_path / "bloodhound_mcp"
    mcp.mkdir()
    (mcp / ".env").write_text(lines, encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / ".env.local").write_text(f"SAGE_BLOODHOUND_MCP_DIR={mcp}\n", encoding="utf-8")
    monkeypatch.delenv("SAGE_BLOODHOUND_MCP_DIR", raising=False)
    monkeypatch.setattr(chat_sessions._runtime_env(), "RUNTIME_DIR", runtime)
    return mcp


MCP_ENV = (
    "BLOODHOUND_DOMAIN=127.0.0.1\nBLOODHOUND_PORT=8083\nBLOODHOUND_SCHEME=http\n"
    f"BLOODHOUND_TOKEN_ID=id-123\nBLOODHOUND_TOKEN_KEY={SECRET}\n"
)


@pytest.mark.parametrize("mode", ["container", "published"])
def test_container_channels_get_bloodhound_from_the_mcp_env(tmp_path, monkeypatch, mode):
    _mcp(tmp_path, monkeypatch, MCP_ENV)
    config, report = chat_sessions.bloodhound_channel_config(deployment_mode=mode)

    assert config == {"BLOODHOUND_URL": "http://127.0.0.1:8083", "BLOODHOUND_TOKEN_ID": "id-123",
                      "BLOODHOUND_TOKEN_KEY": SECRET}
    assert SECRET not in json.dumps(report) and "id-123" not in json.dumps(report)
    assert report["bloodhound"]["keys"] == ["BLOODHOUND_TOKEN_ID", "BLOODHOUND_TOKEN_KEY", "BLOODHOUND_URL"]


def test_local_channels_leave_bloodhound_to_the_mcp(tmp_path, monkeypatch):
    _mcp(tmp_path, monkeypatch, MCP_ENV)
    config, report = chat_sessions.bloodhound_channel_config(deployment_mode="local")
    assert config == {} and "own .env" in report["bloodhound"]


@pytest.mark.parametrize("lines, url", [
    ("BLOODHOUND_DOMAIN=bh.example.invalid\n", "https://bh.example.invalid:443"),
    ("BLOODHOUND_DOMAIN=bh.example.invalid\nBLOODHOUND_PORT=8443\n", "https://bh.example.invalid:8443"),
    ("BLOODHOUND_DOMAIN=localhost\nBLOODHOUND_SCHEME=http\n", "http://localhost:443"),
])
def test_missing_scheme_or_port_follow_the_mcps_own_defaults(tmp_path, monkeypatch, lines, url):
    """The MCP README's minimal .env sets only DOMAIN and the tokens; the MCP then uses https on 443
    (lib/bloodhound_api.py). The channel must reach the same address."""
    _mcp(tmp_path, monkeypatch, lines + f"BLOODHOUND_TOKEN_ID=i\nBLOODHOUND_TOKEN_KEY={SECRET}\n")
    config, _ = chat_sessions.bloodhound_channel_config(deployment_mode="container")
    assert config["BLOODHOUND_URL"] == url


def test_an_unknown_deployment_mode_is_refused():
    with pytest.raises(SystemExit, match="SAGE_DEPLOYMENT_MODE"):
        chat_sessions.bloodhound_channel_config(deployment_mode="contianer")


def test_an_explicit_env_file_works_without_a_host_checkout(tmp_path, monkeypatch):
    env_file = tmp_path / "bh.env"
    env_file.write_text(f"BLOODHOUND_URL=https://bh.example.invalid:443\nBLOODHOUND_TOKEN_ID=i\nBLOODHOUND_TOKEN_KEY={SECRET}\n")
    monkeypatch.setattr(chat_sessions._runtime_env(), "RUNTIME_DIR", tmp_path / "nothing")
    monkeypatch.delenv("SAGE_BLOODHOUND_MCP_DIR", raising=False)

    config, _ = chat_sessions.bloodhound_channel_config(deployment_mode="local", env_file=str(env_file))
    assert config["BLOODHOUND_URL"] == "https://bh.example.invalid:443"


def test_container_mode_refuses_when_nothing_configures_bloodhound(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_sessions._runtime_env(), "RUNTIME_DIR", tmp_path)
    monkeypatch.delenv("SAGE_BLOODHOUND_MCP_DIR", raising=False)
    with pytest.raises(SystemExit) as caught:
        chat_sessions.bloodhound_channel_config(deployment_mode="container")
    assert "SAGE_BLOODHOUND_MCP_DIR" in str(caught.value) and "--bloodhound-env" in str(caught.value)


def test_incomplete_settings_name_what_is_missing_without_values(tmp_path, monkeypatch):
    _mcp(tmp_path, monkeypatch, f"BLOODHOUND_DOMAIN=127.0.0.1\nBLOODHOUND_TOKEN_KEY={SECRET}\n")
    with pytest.raises(SystemExit) as caught:
        chat_sessions.bloodhound_channel_config(deployment_mode="container")
    assert "BLOODHOUND_TOKEN_ID" in str(caught.value) and SECRET not in str(caught.value)


def test_opting_out_sets_nothing(tmp_path, monkeypatch):
    config, report = chat_sessions.bloodhound_channel_config(deployment_mode="container", disabled=True)
    assert config == {} and "--no-bloodhound" in report["bloodhound"]


def test_settings_reach_the_channel_metadata_alongside_the_mode(monkeypatch):
    monkeypatch.setattr(chat_sessions.nc, "default_ai_metadata",
                        lambda extra=None: {"config": {"model": "m", "provider": "openai"}})
    metadata = chat_sessions._build_metadata(
        "supervised", 200, "hybrid", extra_config={"BLOODHOUND_URL": "http://127.0.0.1:8083"})
    assert metadata["config"]["BLOODHOUND_URL"] == "http://127.0.0.1:8083"
    assert metadata["config"]["mode"] == "supervised" and metadata["config"]["policy_mode"] == "hybrid"
