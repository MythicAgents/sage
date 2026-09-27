from __future__ import annotations

import http.client
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import aiohttp
import pytest
import requests

from ai import resource_budget, resource_clients


ROOT = Path(__file__).parents[3]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


SYNC = _load("metered_sync_range_time", "skills/sage-goad-reset/scripts/sync_range_time.py")
DEPLOY = _load(
    "metered_deploy_payload_via_ludus",
    "skills/sage-mythic-payload-deploy/scripts/deploy_payload_via_ludus.py",
)


@pytest.fixture(autouse=True)
def restore_process_clients():
    requests_send = requests.adapters.HTTPAdapter.send
    aiohttp_request = aiohttp.ClientSession._request
    aiohttp_send = aiohttp.ClientRequest.send
    try:
        yield
    finally:
        requests.adapters.HTTPAdapter.send = requests_send
        aiohttp.ClientSession._request = aiohttp_request
        aiohttp.ClientRequest.send = aiohttp_send


def _budget(monkeypatch, tmp_path, *, control: int, guest: int):
    config = tmp_path / "config.json"
    ledger = tmp_path / "ledger.jsonl"
    config.write_text(json.dumps({
        "schema": resource_budget.CONFIG_SCHEMA,
        "resources": {
            "control_requests": {"limit": control, "cleanup_reserve": 0},
            "guest_commands": {"limit": guest, "cleanup_reserve": 0},
        },
        "calendar_seconds": 1000,
        "cleanup_seconds": 1,
    }))
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_CONFIG", str(config))
    monkeypatch.setenv("SAGE_RESOURCE_BUDGET_LEDGER", str(ledger))
    return resource_budget.ResourceLedger(config, ledger)


def _callback_budget(tmp_path, limit=2):
    config = tmp_path / "callback-config.json"
    ledger = tmp_path / "callback-ledger.jsonl"
    config.write_text(json.dumps({
        "schema": resource_budget.CONFIG_SCHEMA,
        "resources": {"callback_launches": {"limit": limit, "cleanup_reserve": 0}},
        "calendar_seconds": 1000,
        "cleanup_seconds": 1,
    }))
    return resource_budget.ResourceLedger(config, ledger)


@pytest.mark.parametrize("module", [SYNC, DEPLOY])
def test_run_ps_reserves_guest_then_charges_each_soap_send(monkeypatch, tmp_path, module):
    ledger = _budget(monkeypatch, tmp_path, control=2, guest=1)
    sent = []
    monkeypatch.setattr(
        resource_clients, "_ORIGINAL_REQUESTS_SEND",
        lambda _adapter, request, **_kwargs: sent.append(request) or object(),
    )

    class Session:
        def run_ps(self, _script):
            adapter = requests.adapters.HTTPAdapter(max_retries=0)
            adapter.send("soap-open")
            adapter.send("soap-command")
            return SimpleNamespace(status_code=0, std_out=b"ok", std_err=b"")

    assert module.run_ps(Session(), "private guest content")["status_code"] == 0
    assert sent == ["soap-open", "soap-command"]
    charges = ledger.status()["effective_resource_charges"]
    assert charges == {"control_requests": 2, "guest_commands": 1}
    assert "private guest content" not in ledger.ledger_path.read_text()


@pytest.mark.parametrize(("module", "error"), [(SYNC, RuntimeError), (DEPLOY, RuntimeError)])
def test_run_ps_denial_precedes_target_and_failure_consumes_reservation(
    monkeypatch, tmp_path, module, error,
):
    ledger = _budget(monkeypatch, tmp_path, control=2, guest=1)
    ledger.reserve({"guest_commands": 1}, "prior-guest-command")

    class DeniedSession:
        calls = 0
        def run_ps(self, _script):
            self.calls += 1

    denied = DeniedSession()
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        module.run_ps(denied, "denied")
    assert denied.calls == 0

    # A separate ledger gives the helper one guest command; a target-side failure
    # cannot refund it.
    (tmp_path / "failure").mkdir(exist_ok=True)
    ledger = _budget(monkeypatch, tmp_path / "failure", control=2, guest=1)

    class FailedSession:
        def run_ps(self, _script):
            raise error("target failure")

    with pytest.raises(error):
        module.run_ps(FailedSession(), "failing")
    assert ledger.status()["effective_resource_charges"]["guest_commands"] == 1


@pytest.mark.parametrize("module", [SYNC, DEPLOY])
def test_winrm_session_installs_control_wrapper_before_session_build(monkeypatch, tmp_path, module):
    ledger = _budget(monkeypatch, tmp_path, control=1, guest=1)
    sent = []
    monkeypatch.setattr(
        resource_clients, "_ORIGINAL_REQUESTS_SEND",
        lambda _adapter, request, **_kwargs: sent.append(request) or object(),
    )
    winrm = ModuleType("winrm")

    def build_session(*_args, **_kwargs):
        requests.adapters.HTTPAdapter(max_retries=0).send("session-build")
        return object()

    winrm.Session = build_session
    monkeypatch.setitem(sys.modules, "winrm", winrm)
    host = {"ansible_host": "unit.invalid", "ansible_user": "user", "ansible_password": "secret"}
    if module is SYNC:
        module.winrm_session(host)
    else:
        module.winrm_session(host, 20, 30)
    assert sent == ["session-build"]
    assert ledger.status()["effective_resource_charges"] == {"control_requests": 1, "guest_commands": 0}


def test_inventory_https_uses_maintained_scoped_opener_and_preserves_selector(
    monkeypatch, tmp_path,
):
    ledger = _budget(monkeypatch, tmp_path, control=2, guest=1)
    sends = []

    class FakeSocket:
        def sendall(self, data):
            sends.append(data)
        def makefile(self, *_args, **_kwargs):
            body = b'{"inventory":{"all":{"hosts":{}}}}'
            return io.BytesIO(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + body
            )
        def close(self):
            return None

    def fake_connect(connection):
        connection.sock = FakeSocket()

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", fake_connect)
    monkeypatch.setattr(SYNC, "ludus_creds", lambda *_args: ("https://unit.invalid", "key"))
    monkeypatch.setattr(DEPLOY, "ludus_creds", lambda *_args: ("https://unit.invalid", "key"))

    assert SYNC.load_inventory(Path("unused"), "SAGEREPL", "ludus_sagerepl") == {"all": {"hosts": {}}}
    result = DEPLOY.ludus_get("/api/v2/range/ansibleinventory", Path("unused"), "GOADf255df", "ludus")
    assert result == {"inventory": {"all": {"hosts": {}}}}
    assert b"rangeID=SAGEREPL" in sends[0]
    assert b"rangeID=GOADf255df" in sends[1]
    assert ledger.status()["effective_resource_charges"] == {"control_requests": 2, "guest_commands": 0}


@pytest.mark.parametrize("module", [SYNC, DEPLOY])
def test_absent_ledger_keeps_unmetered_run_ps_path(monkeypatch, module):
    monkeypatch.delenv("SAGE_RESOURCE_BUDGET_CONFIG", raising=False)
    monkeypatch.delenv("SAGE_RESOURCE_BUDGET_LEDGER", raising=False)
    before = requests.adapters.HTTPAdapter.send

    class Session:
        def run_ps(self, _script):
            return SimpleNamespace(status_code=0, std_out=b"ok", std_err=b"")

    assert module.run_ps(Session(), "plain")["status_code"] == 0
    assert requests.adapters.HTTPAdapter.send is before


def test_launch_attempts_reserve_once_and_retain_failed_attempt(monkeypatch, tmp_path):
    ledger = _callback_budget(tmp_path)
    calls = []
    monkeypatch.setattr(DEPLOY, "configured_resource_ledger", lambda: ledger)

    def run_ps(_session, script):
        calls.append(script)
        if len(calls) == 2:
            raise RuntimeError("guest failure")
        return {"status_code": 0, "stdout": '{"Method":"current-user"}', "stderr": ""}

    monkeypatch.setattr(DEPLOY, "run_ps", run_ps)
    assert DEPLOY.launch_current_user(object(), r"C:\payload.exe")["Method"] == "current-user"
    with pytest.raises(RuntimeError):
        DEPLOY.launch_current_user(object(), r"C:\payload.exe")
    assert len(calls) == 2
    assert ledger.status()["effective_resource_charges"]["callback_launches"] == 2


def test_launch_prerequisite_and_exhausted_allocation_precede_effect(monkeypatch, tmp_path):
    monkeypatch.setattr(
        DEPLOY, "reserve_effect", lambda *_a, **_k: pytest.fail("reservation before prerequisites"),
    )
    args = SimpleNamespace(launch_method="scheduled-task", run_as_user="", allow_s4u_task=False)
    with pytest.raises(DEPLOY.DeployError):
        DEPLOY.launch_payload(object(), r"C:\payload.exe", args)

    ledger = _callback_budget(tmp_path, 1)
    ledger.reserve({"callback_launches": 1}, "prior")
    monkeypatch.setattr(DEPLOY, "configured_resource_ledger", lambda: ledger)
    monkeypatch.setattr(DEPLOY, "reserve_effect", lambda amounts, operation: ledger.reserve(amounts, operation))
    monkeypatch.setattr(DEPLOY, "run_ps", lambda *_a, **_k: pytest.fail("launch dispatched"))
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        DEPLOY.launch_current_user(object(), r"C:\payload.exe")
