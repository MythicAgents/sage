from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "ludus.py"
SPEC = importlib.util.spec_from_file_location("ludus", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_with_range_id_preserves_existing_query_and_does_not_duplicate():
    assert (
        MODULE._with_range_id("/api/v2/range/logs?tail=60", "SAGEPOLICY20260712")
        == "/api/v2/range/logs?tail=60&rangeID=SAGEPOLICY20260712"
    )
    assert (
        MODULE._with_range_id("/api/v2/range?rangeID=existing", "SAGEPOLICY20260712")
        == "/api/v2/range?rangeID=existing"
    )


def test_default_mcp_path_derives_from_repo_root():
    assert MODULE.MCP == str(MODULE.REPO_ROOT / ".mcp.json")


def test_parser_accepts_range_id_before_or_after_command():
    assert MODULE.build_parser().parse_args(["--range-id", "before", "status"]).range_id == "before"
    assert MODULE.build_parser().parse_args(["status", "--range-id", "after"]).range_id == "after"


def test_creds_selects_named_mcp_server_without_changing_default(monkeypatch, tmp_path):
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "ludus": {"env": {"LUDUS_URL": "https://goad", "LUDUS_API_KEY": "goad.key"}},
                    "ludus_sagerepl": {
                        "env": {"LUDUS_URL": "https://sagerepl", "LUDUS_API_KEY": "sagerepl.key"}
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE, "MCP", str(mcp_path))

    assert MODULE._creds() == ("https://goad", "goad.key")
    assert MODULE._creds("ludus_sagerepl") == ("https://sagerepl", "sagerepl.key")

    monkeypatch.setenv(MODULE.MCP_SERVER_ENV, "ludus_sagerepl")
    assert MODULE._creds() == ("https://sagerepl", "sagerepl.key")


def test_parser_accepts_mcp_server_before_or_after_command():
    assert MODULE.build_parser().parse_args(["--mcp-server", "before", "status"]).mcp_server == "before"
    assert MODULE.build_parser().parse_args(["status", "--mcp-server", "after"]).mcp_server == "after"
