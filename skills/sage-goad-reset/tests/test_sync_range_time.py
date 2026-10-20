from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import pytest


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


def _sync_args(action="sync"):
    return SimpleNamespace(
        action=action,
        mcp_path="/unused/.mcp.json",
        range_id="GOAD",
        mcp_server="ludus",
        max_skew_seconds=60.0,
        auth_wait_seconds=30.0,
        yes=True,
    )


def _five_host_inventory():
    return {
        "all": {
            "hosts": {
                name: {**host, "ansible_connection": "winrm"}
                for name, host in zip(
                    ("dc01", "dc02", "srv01", "srv02", "srv03"),
                    _hosts("dc01", "dc02", "srv01", "srv02", "srv03"),
                )
            }
        }
    }


def test_main_sync_emits_parseable_json_and_sends_progress_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(MODULE, "parse_args", lambda: _sync_args())
    monkeypatch.setattr(MODULE, "load_inventory", lambda *_args: _five_host_inventory())
    monkeypatch.setattr(MODULE, "wait_for_winrm_auth", lambda *_args, **_kwargs: {"ready": True})
    monkeypatch.setattr(MODULE, "winrm_session", lambda value: value)
    monkeypatch.setattr(MODULE, "run_ps", lambda *_args: {"status_code": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr(MODULE, "check_clocks", lambda *_args: {"ready": True, "hosts": []})

    assert MODULE.main() == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"hosts": [], "ready": True}
    assert "set dc01 (10.0.0.1)" in captured.err


def test_main_sync_keeps_failed_clock_result_on_json_stdout(monkeypatch, capsys):
    monkeypatch.setattr(MODULE, "parse_args", lambda: _sync_args())
    monkeypatch.setattr(MODULE, "load_inventory", lambda *_args: _five_host_inventory())
    monkeypatch.setattr(MODULE, "wait_for_winrm_auth", lambda *_args, **_kwargs: {"ready": True})
    monkeypatch.setattr(MODULE, "winrm_session", lambda value: value)
    monkeypatch.setattr(MODULE, "run_ps", lambda *_args: {"status_code": 0, "stdout": "", "stderr": ""})
    failed = {"ready": False, "over_limit": ["dc01"]}
    monkeypatch.setattr(MODULE, "check_clocks", lambda *_args: failed)

    assert MODULE.main() == 1

    captured = capsys.readouterr()
    assert json.loads(captured.out) == failed
    assert "set dc01 (10.0.0.1)" in captured.err


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


# --- WinRM auth readiness ---------------------------------------------------------------------
#
# A rolled-back guest reports its IP through the Ludus agent well before AD can authenticate a
# domain account. A reset that waits only for IPs and then syncs gets `InvalidCredentialsError: 401`
# from a DC whose credentials are perfectly correct. The wait below is on the condition that
# actually gates the sync.


def _hosts(*names):
    return [{"inventory_hostname": name, "ansible_host": f"10.0.0.{i}"} for i, name in enumerate(names, 1)]


def test_auth_wait_returns_immediately_when_every_host_answers(monkeypatch):
    monkeypatch.setattr(MODULE, "winrm_session", lambda host: host)
    monkeypatch.setattr(MODULE, "run_ps", lambda session, script: "")
    result = MODULE.wait_for_winrm_auth(_hosts("dc01", "dc02"), sleep=lambda _s: None)
    assert result == {"ready": True, "attempts": 1, "pending": [], "last_error": ""}


def test_auth_wait_retries_until_a_late_host_authenticates(monkeypatch):
    calls = {"n": 0}

    def flaky(session, script):
        calls["n"] += 1
        if session["inventory_hostname"] == "dc01" and calls["n"] < 3:
            raise RuntimeError("the specified credentials were rejected by the server")
        return ""

    monkeypatch.setattr(MODULE, "winrm_session", lambda host: host)
    monkeypatch.setattr(MODULE, "run_ps", flaky)
    result = MODULE.wait_for_winrm_auth(_hosts("dc01"), sleep=lambda _s: None)
    assert result["ready"] is True
    assert result["attempts"] == 3


def test_auth_wait_gives_up_within_its_budget_and_names_the_pending_hosts(monkeypatch):
    clock = {"t": 0.0}

    def never(session, script):
        raise RuntimeError("the specified credentials were rejected by the server")

    monkeypatch.setattr(MODULE, "winrm_session", lambda host: host)
    monkeypatch.setattr(MODULE, "run_ps", never)
    result = MODULE.wait_for_winrm_auth(
        _hosts("dc01", "dc02"),
        timeout_seconds=30,
        poll_seconds=10,
        sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
        now=lambda: clock["t"],
    )
    assert result["ready"] is False
    assert result["pending"] == ["dc01", "dc02"]
    assert "rejected by the server" in result["last_error"]


def test_auth_wait_uses_late_inventory_credentials_and_updates_sync_hosts(monkeypatch):
    hosts = _hosts("dc01", "dc02")
    refreshed = [
        {**host, "ansible_user": "operator", "ansible_password": "test-only"}
        for host in hosts
    ]
    refresh_calls = []

    def session(host):
        if not host.get("ansible_user") or not host.get("ansible_password"):
            raise RuntimeError("lacks Ludus WinRM connection variables")
        return host

    monkeypatch.setattr(MODULE, "winrm_session", session)
    monkeypatch.setattr(MODULE, "run_ps", lambda _session, _script: None)
    result = MODULE.wait_for_winrm_auth(
        hosts, refresh_hosts=lambda: refresh_calls.append(1) or refreshed, sleep=lambda _s: None
    )

    assert result == {"ready": True, "attempts": 2, "pending": [], "last_error": ""}
    assert len(refresh_calls) == 1
    assert all(host["ansible_user"] == "operator" for host in hosts)


def test_auth_wait_times_out_when_refreshed_inventory_never_has_credentials(monkeypatch):
    clock = {"t": 0.0}
    hosts = _hosts("dc01", "dc02")
    monkeypatch.setattr(
        MODULE, "winrm_session",
        lambda host: (_ for _ in ()).throw(RuntimeError("lacks Ludus WinRM connection variables")),
    )
    result = MODULE.wait_for_winrm_auth(
        hosts,
        refresh_hosts=lambda: [dict(host) for host in hosts],
        timeout_seconds=20,
        poll_seconds=10,
        sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
        now=lambda: clock["t"],
    )
    assert result["ready"] is False
    assert result["attempts"] == 3
    assert result["pending"] == ["dc01", "dc02"]
    assert "connection variables" in result["last_error"]


@pytest.mark.parametrize(
    "refreshed",
    [
        _hosts("dc01"),
        [{**_hosts("dc01", "dc02")[0], "ansible_host": "10.0.0.99"}, _hosts("dc01", "dc02")[1]],
        _hosts("dc01", "dc02", "srv01"),
    ],
)
def test_auth_wait_fails_closed_if_host_identity_changes(monkeypatch, refreshed):
    hosts = _hosts("dc01", "dc02")
    monkeypatch.setattr(
        MODULE, "winrm_session",
        lambda host: (_ for _ in ()).throw(RuntimeError("not ready")),
    )
    with pytest.raises(RuntimeError, match="host identities changed"):
        MODULE.wait_for_winrm_auth(
            hosts, refresh_hosts=lambda: refreshed, sleep=lambda _s: None
        )
    assert [host["inventory_hostname"] for host in hosts] == ["dc01", "dc02"]


def test_main_rejects_partial_windows_inventory_before_any_sync(monkeypatch):
    monkeypatch.setattr(MODULE, "parse_args", lambda: _sync_args())
    monkeypatch.setattr(
        MODULE,
        "load_inventory",
        lambda *_args: {
            "all": {"hosts": {"dc01": {**_hosts("dc01")[0], "ansible_connection": "winrm"}}}
        },
    )
    monkeypatch.setattr(MODULE, "sync_clocks", lambda _hosts: pytest.fail("must not sync partial inventory"))
    with pytest.raises(RuntimeError, match="exactly five GOAD Windows hosts"):
        MODULE.main()


def test_main_refreshes_inventory_before_syncing_late_credentials(monkeypatch):
    first = _five_host_inventory()
    refreshed = _five_host_inventory()
    for host in refreshed["all"]["hosts"].values():
        host.update(ansible_user="operator", ansible_password="test-only")
    inventory_calls = []
    synced = []

    def inventory(*_args):
        inventory_calls.append(1)
        return first if len(inventory_calls) == 1 else refreshed

    def session(host):
        if not host.get("ansible_user") or not host.get("ansible_password"):
            raise RuntimeError("lacks Ludus WinRM connection variables")
        return host

    monkeypatch.setattr(MODULE, "parse_args", lambda: _sync_args())
    monkeypatch.setattr(MODULE, "load_inventory", inventory)
    monkeypatch.setattr(MODULE, "winrm_session", session)
    monkeypatch.setattr(MODULE, "run_ps", lambda *_args: None)
    monkeypatch.setattr(MODULE, "sync_clocks", lambda hosts: synced.extend(dict(host) for host in hosts))
    monkeypatch.setattr(MODULE, "check_clocks", lambda *_args: {"ready": True, "hosts": []})
    original_wait = MODULE.wait_for_winrm_auth
    monkeypatch.setattr(
        MODULE,
        "wait_for_winrm_auth",
        lambda hosts, **kwargs: original_wait(hosts, sleep=lambda _seconds: None, **kwargs),
    )

    assert MODULE.main() == 0
    assert len(inventory_calls) == 2
    assert len(synced) == 5
    assert all(host["ansible_user"] == "operator" for host in synced)
