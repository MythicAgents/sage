from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "sync_range_time.py"
SPEC = importlib.util.spec_from_file_location("sync_range_time", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_windows_hosts_excludes_router_and_orders_dcs_first():
    inventory = {
        "all": {
            "hosts": {
                "router": {
                    "ansible_host": "10.4.10.254",
                    "ansible_connection": "ssh",
                },
                "srv": {
                    "ansible_host": "10.4.10.22",
                    "ansible_connection": "winrm",
                },
                "dc": {
                    "ansible_host": "10.4.10.10",
                    "ansible_connection": "winrm",
                },
            }
        }
    }

    hosts = MODULE.windows_hosts(inventory)

    assert [host["inventory_hostname"] for host in hosts] == ["dc", "srv"]


def test_default_mcp_path_derives_from_repo_root():
    assert MODULE.DEFAULT_MCP_PATH == MODULE.REPO_ROOT / ".mcp.json"


def test_clock_offset_seconds_uses_absolute_utc_delta():
    controller = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc)

    assert MODULE.clock_offset_seconds("2026-06-24T13:59:55Z", controller) == 5.0


def test_clock_sync_disables_time_service_before_setting_clock():
    script = MODULE.build_clock_sync_script("2026-06-24T14:00:00+00:00")

    assert "Set-Service -Name w32time -StartupType Disabled" in script
    assert script.index("StartupType Disabled") < script.index("Set-Date")


def test_with_range_id_preserves_existing_query_and_does_not_duplicate():
    assert (
        MODULE.with_range_id("/api/v2/range/logs?tail=60", "SAGEPOLICY20260712")
        == "/api/v2/range/logs?tail=60&rangeID=SAGEPOLICY20260712"
    )
    assert (
        MODULE.with_range_id("/api/v2/range?rangeID=existing", "SAGEPOLICY20260712")
        == "/api/v2/range?rangeID=existing"
    )


def test_ludus_creds_selects_named_mcp_server_without_changing_default(monkeypatch, tmp_path):
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

    assert MODULE.ludus_creds(mcp_path) == ("https://goad", "goad.key")
    assert MODULE.ludus_creds(mcp_path, "ludus_sagerepl") == ("https://sagerepl", "sagerepl.key")

    monkeypatch.setenv(MODULE.MCP_SERVER_ENV, "ludus_sagerepl")
    assert MODULE.ludus_creds(mcp_path) == ("https://sagerepl", "sagerepl.key")
