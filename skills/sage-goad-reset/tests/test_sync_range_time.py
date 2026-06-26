from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
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


def test_clock_offset_seconds_uses_absolute_utc_delta():
    controller = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc)

    assert MODULE.clock_offset_seconds("2026-06-24T13:59:55Z", controller) == 5.0


def test_clock_sync_disables_time_service_before_setting_clock():
    script = MODULE.build_clock_sync_script("2026-06-24T14:00:00+00:00")

    assert "Set-Service -Name w32time -StartupType Disabled" in script
    assert script.index("StartupType Disabled") < script.index("Set-Date")
