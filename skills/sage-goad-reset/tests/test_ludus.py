from __future__ import annotations

import importlib.util
import io
import json
import ssl
import sys
import threading
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "Payload_Type" / "sage"))
from ai import resource_budget


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


def _resource_ledger(tmp_path, control_limit):
    config = tmp_path / "resource-config.json"
    ledger = tmp_path / "resource-ledger.jsonl"
    config.write_text(json.dumps({
        "schema": resource_budget.CONFIG_SCHEMA,
        "resources": {
            "control_requests": {"limit": control_limit, "cleanup_reserve": 0},
            "new_staged_snapshots": {"limit": 2, "cleanup_reserve": 0},
            "physical_restores": {"limit": 2, "cleanup_reserve": 0},
        },
        "calendar_seconds": 1000,
        "cleanup_seconds": 1,
    }))
    return resource_budget.ResourceLedger(config, ledger)


def test_ludus_absent_resource_config_preserves_default_urlopen(monkeypatch):
    class Response:
        status = 200
        def read(self): return b'{"rangeNumber":4,"rangeState":"SUCCESS","VMs":[]}'
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    monkeypatch.setattr(MODULE, "_creds", lambda *_args: ("https://unit.invalid", "key"))
    monkeypatch.setattr(MODULE, "_verified_tls_config", lambda *_args: None)
    monkeypatch.setattr(MODULE, "_configured_resource_ledger", lambda: None)
    monkeypatch.setattr(MODULE, "_metered_urlopen", lambda *_a, **_k: pytest.fail("metered path used"))
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", lambda *_a, **_k: Response())

    code, result = MODULE._call("GET", "/api/v2/range")
    assert code == 200 and result["rangeNumber"] == 4


def test_ludus_metered_https_handler_dispatches_with_supplied_context(monkeypatch, tmp_path):
    sends = []
    contexts = []

    class FakeSocket:
        def sendall(self, data):
            sends.append(data)
        def makefile(self, *_args, **_kwargs):
            return io.BytesIO(
                b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 49\r\n\r\n'
                b'{"rangeNumber":4,"rangeState":"SUCCESS","VMs":[]}'
            )
        def close(self):
            return None

    def fake_connect(connection):
        contexts.append(connection._context)
        connection.sock = FakeSocket()

    ledger = _resource_ledger(tmp_path, 1)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    monkeypatch.setattr(MODULE._MeteredHTTPSConnection, "connect", fake_connect)

    request = MODULE.urllib.request.Request("https://unit.invalid/api/v2/range", method="GET")
    with MODULE._metered_urlopen(request, timeout=1, context=context, ledger=ledger) as response:
        assert json.loads(response.read())["rangeState"] == "SUCCESS"

    assert contexts == [context]
    assert len(sends) == 1
    assert ledger.status()["effective_resource_charges"]["control_requests"] == 1


@pytest.mark.parametrize("control_limit,expected_server_calls", [(2, 2), (1, 1)])
def test_ludus_meter_charges_redirect_dispatch_or_denies_before_second_io(
    monkeypatch, tmp_path, control_limit, expected_server_calls,
):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if self.path.startswith("/api/v2/range"):
                self.send_response(302)
                self.send_header("Location", "/final")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"rangeNumber":4,"rangeState":"SUCCESS","VMs":[]}')
        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ledger = _resource_ledger(tmp_path, control_limit)
    monkeypatch.setattr(MODULE, "_configured_resource_ledger", lambda: ledger)
    monkeypatch.setattr(MODULE, "_creds", lambda *_args: (f"http://127.0.0.1:{server.server_port}", "key"))
    monkeypatch.setattr(MODULE, "_verified_tls_config", lambda *_args: None)
    try:
        if control_limit == 1:
            with pytest.raises(resource_budget.ResourceBudgetExceeded):
                MODULE._call("GET", "/api/v2/range")
        else:
            assert MODULE._call("GET", "/api/v2/range")[0] == 200
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert len(calls) == expected_server_calls
    assert ledger.status()["effective_resource_charges"]["control_requests"] == control_limit


def test_verified_tls_config_is_complete_and_selects_named_server(monkeypatch, tmp_path):
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("offline CA fixture")
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {
        "ludus": {"env": {"LUDUS_CA_FILE": str(ca_file)}},
        "other": {"env": {"LUDUS_CA_FILE": str(ca_file),
                          "LUDUS_TLS_SERVER_NAME": "ludus.localdomain",
                          "LUDUS_CERT_SHA256": "a" * 64}},
    }}))
    monkeypatch.setattr(MODULE, "MCP", str(mcp_path))
    for key in MODULE.TLS_CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="requires all three"):
        MODULE._verified_tls_config("ludus")
    assert MODULE._verified_tls_config("other") == (ca_file, "ludus.localdomain", "a" * 64)
    monkeypatch.setenv("LUDUS_CERT_SHA256", "wrong")
    with pytest.raises(RuntimeError, match="SHA-256 digest"):
        MODULE._verified_tls_config("other")


def test_verified_connection_rejects_wrong_pin_before_http_credentials(monkeypatch):
    sent = []
    class FakeSocket:
        def getpeercert(self, *, binary_form):
            assert binary_form is True
            return b"presented cert"
        def sendall(self, data):
            sent.append(data)
        def close(self):
            return None
    wrapped = FakeSocket()
    context = SimpleNamespace(wrap_socket=lambda raw, *, server_hostname: wrapped)
    monkeypatch.setattr(MODULE.socket, "create_connection", lambda *_args: FakeSocket())
    connection = MODULE._VerifiedHTTPSConnection(
        "configured.example", 443, context=context, server_name="ludus.localdomain",
        certificate_sha256="0" * 64, timeout=1)
    with pytest.raises(RuntimeError, match="certificate.*pin"):
        connection.request("GET", "/api/v2/range", headers={"X-API-KEY": "offline-secret"})
    assert sent == []


def test_verified_call_preserves_request_meter_and_disallows_redirect(monkeypatch, tmp_path):
    ledger = _resource_ledger(tmp_path, 1)
    context = SimpleNamespace(verify_flags=0)
    seen = []
    class FakeResponse:
        status = 302
        def read(self): return b"redirect refused"
    class FakeConnection:
        def __init__(self, host, port, **kwargs):
            seen.append((host, port, kwargs["server_name"], kwargs["certificate_sha256"]))
        def request(self, method, path, **kwargs):
            seen.append((method, path, kwargs["headers"]["X-API-KEY"]))
        def getresponse(self): return FakeResponse()
        def close(self): return None
    monkeypatch.setattr(MODULE.ssl, "create_default_context", lambda **_kwargs: context)
    monkeypatch.setattr(MODULE, "_VerifiedHTTPSConnection", FakeConnection)
    assert MODULE._verified_call(
        "GET", "https://configured.example:8443", "/api/v2/range", None, "offline-key",
        (tmp_path / "ca.pem", "ludus.localdomain", "a" * 64), ledger,
    ) == (302, "redirect refused")
    assert seen == [("configured.example", 8443, "ludus.localdomain", "a" * 64),
                    ("GET", "/api/v2/range", "offline-key")]
    assert ledger.status()["effective_resource_charges"]["control_requests"] == 1


def test_parser_accepts_mcp_server_before_or_after_command():
    assert MODULE.build_parser().parse_args(["--mcp-server", "before", "status"]).mcp_server == "before"
    assert MODULE.build_parser().parse_args(["status", "--mcp-server", "after"]).mcp_server == "after"


@pytest.mark.parametrize("action", ["poweron", "poweroff"])
def test_explicit_power_selectors_resolve_to_names_on_wire(monkeypatch, action):
    sent = []
    inventory = {"VMs": [
        {"name": "GOAD-router", "proxmoxID": 114},
        {"name": "GOAD-DC", "proxmoxID": 119},
    ]}

    class Response:
        status = 200
        def __init__(self, data): self.data = data
        def read(self): return json.dumps(self.data).encode()
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    def urlopen(request, **_kwargs):
        sent.append((request.get_method(), request.full_url, json.loads(request.data) if request.data else None))
        return Response(inventory if request.get_method() == "GET" else {"result": "accepted"})

    monkeypatch.setattr(MODULE, "_creds", lambda server: ("https://unit.invalid", "key"))
    monkeypatch.setattr(MODULE, "_verified_tls_config", lambda server: None)
    monkeypatch.setattr(MODULE, "_configured_resource_ledger", lambda: None)
    monkeypatch.setattr(MODULE.urllib.request, "urlopen", urlopen)

    getattr(MODULE, action)("114,GOAD-DC", range_id="GOADf255df", mcp_server="ludus")
    assert sent == [
        ("GET", "https://unit.invalid/api/v2/range?rangeID=GOADf255df", None),
        ("PUT", f"https://unit.invalid/api/v2/range/{action}?rangeID=GOADf255df",
         {"machines": ["GOAD-router", "GOAD-DC"]}),
    ]


def test_power_all_preserves_single_put_without_inventory(monkeypatch):
    calls = []
    def call(method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        return 200, {"result": "accepted"}
    monkeypatch.setattr(MODULE, "_call", call)
    MODULE.poweroff("all", range_id="GOADf255df", mcp_server="ludus")
    assert calls == [("PUT", "/api/v2/range/poweroff", {"machines": ["all"]},
                      {"range_id": "GOADf255df", "mcp_server": "ludus"})]


@pytest.mark.parametrize("selector,inventory", [
    ("999", {"VMs": [{"name": "GOAD-DC", "proxmoxID": 119}]}),
    ("119", {"VMs": [{"name": "GOAD-DC", "proxmoxID": 119},
                       {"name": "GOAD-other", "proxmoxID": 119}]}),
    ("119", {"VMs": [{"name": "GOAD-DC", "proxmoxID": 119},
                       {"name": "GOAD-DC", "proxmoxID": 120}]}),
    ("119,GOAD-DC", {"VMs": [{"name": "GOAD-DC", "proxmoxID": 119}]}),
    ("all,119", {"VMs": [{"name": "GOAD-DC", "proxmoxID": 119}]}),
])
def test_power_rejects_unknown_ambiguous_or_duplicate_selector_before_put(monkeypatch, selector, inventory):
    calls = []
    def call(method, path, body=None, **kwargs):
        calls.append((method, path))
        return 200, inventory
    monkeypatch.setattr(MODULE, "_call", call)
    with pytest.raises(RuntimeError, match="power operation not sent|requires VM"):
        MODULE.poweroff(selector, range_id="GOADf255df", mcp_server="ludus")
    assert not any(method == "PUT" for method, _path in calls)


@pytest.mark.parametrize("code,inventory", [(500, "failed"), (200, {}), (200, {"VMs": []})])
def test_power_rejects_failed_or_invalid_inventory_before_put(monkeypatch, code, inventory):
    calls = []
    def call(method, path, body=None, **kwargs):
        calls.append((method, path, kwargs))
        return code, inventory
    monkeypatch.setattr(MODULE, "_call", call)
    with pytest.raises(RuntimeError, match="power operation not sent"):
        MODULE.poweron("114", range_id="GOADf255df", mcp_server="ludus")
    assert calls == [("GET", "/api/v2/range", {"range_id": "GOADf255df", "mcp_server": "ludus"})]


def test_mutation_cli_returns_nonzero_without_printing_native_error(monkeypatch, capsys):
    monkeypatch.setattr(MODULE, "_call", lambda *_args, **_kwargs: (500, "native secret-bearing failure"))

    assert MODULE.main(["poweron"]) == 2

    captured = capsys.readouterr()
    assert "HTTP 500" in captured.err
    assert "secret-bearing" not in captured.err


def test_snapshot_mutations_accept_2xx_and_reject_non2xx(monkeypatch, capsys):
    def succeeds(_method, path, *_args, **_kwargs):
        if path == "/api/v2/snapshots/list":
            return 200, {"snapshots": [{"name": "clean"}]}
        return 200, {"status": "success"}

    monkeypatch.setattr(MODULE, "_call", succeeds)

    assert MODULE.main(["snapshot", "clean"]) == 0
    assert MODULE.main(["rollback", "clean", "--yes"]) == 0
    assert MODULE.main(["poweron"]) == 0

    def mutation_fails(_method, path, *_args, **_kwargs):
        if path == "/api/v2/snapshots/list":
            return 200, {"snapshots": [{"name": "clean"}]}
        return 500, {"error": "sensitive native detail"}

    monkeypatch.setattr(MODULE, "_call", mutation_fails)
    assert MODULE.main(["snapshot", "clean"]) == 2
    assert MODULE.main(["rollback", "clean", "--yes"]) == 2
    captured = capsys.readouterr()
    assert "sensitive native detail" not in captured.err


def test_snapshot_and_restore_reserve_once_before_attempt_and_retain_failures(monkeypatch, tmp_path):
    ledger = _resource_ledger(tmp_path, 8)
    calls = []
    monkeypatch.setattr(MODULE, "_configured_resource_ledger", lambda: ledger)

    def mutation(method, path, *_args, **_kwargs):
        calls.append((method, path))
        return (500, "failed") if "create" in path else (200, {})

    monkeypatch.setattr(MODULE, "_call", mutation)
    with pytest.raises(RuntimeError):
        MODULE.snapshot("attempted")
    MODULE.rollback("clean")
    assert calls == [
        ("POST", "/api/v2/snapshots/create"),
        ("POST", "/api/v2/snapshots/rollback"),
    ]
    assert ledger.status()["effective_resource_charges"] == {
        "control_requests": 0, "new_staged_snapshots": 1, "physical_restores": 1,
    }


def test_exhausted_snapshot_allocation_denies_before_mutation(monkeypatch, tmp_path):
    ledger = _resource_ledger(tmp_path, 8)
    ledger.reserve({"new_staged_snapshots": 2}, "prior")
    monkeypatch.setattr(MODULE, "_configured_resource_ledger", lambda: ledger)
    monkeypatch.setattr(MODULE, "_call", lambda *_a, **_k: pytest.fail("mutation dispatched"))
    with pytest.raises(resource_budget.ResourceBudgetExceeded):
        MODULE.snapshot("denied")


def test_rollback_cli_prerequisite_rejects_before_restore_reservation(monkeypatch):
    monkeypatch.setattr(
        MODULE, "_reserve_effect", lambda *_a, **_k: pytest.fail("restore reservation made"),
    )
    assert MODULE.main(["rollback", "clean"]) == 2
