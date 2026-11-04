import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-goad-reset" / "scripts" / "bh_reset.py"
SPEC = importlib.util.spec_from_file_location("bh_reset", SCRIPT)
bh_reset = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(bh_reset)


def test_bloodhound_dir_uses_the_runtime_lookup_and_never_a_sibling(tmp_path, monkeypatch):
    """Process env, then Payload_Type/sage/.env.local, then .env; unresolved is a named refusal."""
    import pytest

    (tmp_path / "bloodhound_mcp").mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.delenv("SAGE_BLOODHOUND_MCP_DIR", raising=False)
    monkeypatch.setattr(bh_reset.sage_runtime_env, "RUNTIME_DIR", runtime)
    with pytest.raises(SystemExit) as excinfo:
        bh_reset.bloodhound_mcp_dir()
    assert "SAGE_BLOODHOUND_MCP_DIR" in str(excinfo.value)

    (runtime / ".env.local").write_text(f"SAGE_BLOODHOUND_MCP_DIR={tmp_path / 'mcp'}\n", encoding="utf-8")
    assert bh_reset.bloodhound_mcp_dir() == tmp_path / "mcp"


def test_wait_for_empty_delays_first_poll_and_retries(monkeypatch):
    responses = iter([
        (200, [{"name": "OLD.LOCAL"}]),
        (200, []),
    ])
    sleeps = []
    monkeypatch.setattr(bh_reset, "_domains", lambda: next(responses))

    result = bh_reset.wait_for_empty(
        initial_wait=10,
        poll_interval=5,
        attempts=2,
        sleep=sleeps.append,
    )

    assert result is True
    assert sleeps == [10, 5]
