from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[3]
SAGE_ROOT = ROOT / "Payload_Type" / "sage"
SPEC = importlib.util.spec_from_file_location(
    "docker_request_meter", ROOT / "skills/sage-goad-reset/scripts/docker_request_meter.py"
)
meter = importlib.util.module_from_spec(SPEC); assert SPEC and SPEC.loader; SPEC.loader.exec_module(meter)
from ai import resource_budget


def _ledger(tmp_path, limit=10, cleanup=0):
    config, ledger = tmp_path / "config.json", tmp_path / "ledger.jsonl"
    config.write_text(json.dumps({"schema": resource_budget.CONFIG_SCHEMA,
        "resources": {"control_requests": {"limit": limit, "cleanup_reserve": cleanup}},
        "calendar_seconds": 100, "cleanup_seconds": 10}))
    return resource_budget.ResourceLedger(config, ledger)


class _Daemon:
    def __init__(self, path, handler): self.path, self.handler, self.calls = path, handler, []
    def __enter__(self):
        self.sock = socket.socket(socket.AF_UNIX); self.sock.bind(str(self.path)); self.sock.listen()
        self.stop = threading.Event(); self.children=[]
        def run():
            while not self.stop.is_set():
                try: client, _ = self.sock.accept()
                except OSError: break
                if self.stop.is_set(): client.close(); break
                self.calls.append(client)
                child=threading.Thread(target=self.handler,args=(client,),daemon=True); self.children.append(child); child.start()
        self.thread = threading.Thread(target=run, daemon=True); self.thread.start(); return self
    def __exit__(self, *_args):
        self.stop.set(); self.sock.close()
        try:
            wake=socket.socket(socket.AF_UNIX); wake.connect(str(self.path)); wake.close()
        except OSError: pass
        self.thread.join(1)
        for child in self.children: child.join(1)
        self.path.unlink(missing_ok=True)


def _read_headers(client):
    value=b""
    while b"\r\n\r\n" not in value: value += client.recv(4096)
    return value


def _send(path, value, *, shutdown=False):
    client=socket.socket(socket.AF_UNIX); client.connect(str(path)); client.sendall(value)
    if shutdown: client.shutdown(socket.SHUT_WR)
    response=b""
    while True:
        try: part=client.recv(65536)
        except ConnectionResetError: break
        if not part: break
        response += part
    client.close(); return response


def test_get_post_exact_bytes_and_sequential_metering(tmp_path):
    upstream=tmp_path/"up.sock"; received=[]
    def handler(client):
        value=_read_headers(client)
        if b"Content-Length: 4\r\n" in value and not value.endswith(b"data"): value += client.recv(4)
        received.append(value); client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"); client.close()
    with _Daemon(upstream,handler), meter.DockerRequestMeter(upstream,_ledger(tmp_path),2,temporary_parent=tmp_path) as proxy:
        get=b"GET /version HTTP/1.1\r\nHost: docker\r\n\r\n"
        post=b"POST /v1/containers HTTP/1.1\r\nHost: docker\r\nContent-Length: 4\r\n\r\ndata"
        assert _send(proxy.socket_path,get).endswith(b"ok")
        assert _send(proxy.socket_path,post).endswith(b"ok")
    assert received == [
        b"GET /version HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n",
        b"POST /v1/containers HTTP/1.1\r\nHost: docker\r\nContent-Length: 4\r\nConnection: close\r\n\r\ndata",
    ]
    assert resource_budget.ResourceLedger(tmp_path/"config.json",tmp_path/"ledger.jsonl").status()["effective_resource_charges"]["control_requests"]==2


def test_budget_denial_and_malformed_or_overlimit_never_connect_upstream(tmp_path):
    upstream=tmp_path/"up.sock"
    def handler(client): client.close()
    with _Daemon(upstream,handler) as daemon, meter.DockerRequestMeter(upstream,_ledger(tmp_path,1),1,temporary_parent=tmp_path) as proxy:
        denied=b"GET /first HTTP/1.1\r\n\r\n"
        _send(proxy.socket_path,denied)
        assert b"429" in _send(proxy.socket_path,denied)
    assert len(daemon.calls)==1

    upstream2=tmp_path/"up2.sock"; second=tmp_path/"second"; second.mkdir(); ledger=_ledger(second,3)
    with _Daemon(upstream2,handler) as daemon, meter.DockerRequestMeter(upstream2,ledger,0.1,temporary_parent=tmp_path) as proxy:
        malformed=b"GET /bad HTTP/1.1\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n"
        huge=f"POST /huge HTTP/1.1\r\nContent-Length: {meter.MAX_BODY_BYTES+1}\r\n\r\n".encode()
        partial=b"POST /partial HTTP/1.1\r\nContent-Length: 3\r\n\r\nx"
        assert b"400" in _send(proxy.socket_path,malformed)
        assert b"400" in _send(proxy.socket_path,huge)
        assert b"400" in _send(proxy.socket_path,partial,shutdown=True)
    assert len(daemon.calls)==0 and ledger.status()["effective_resource_charges"]["control_requests"]==3


def test_chunked_upload_and_streaming_chunked_response(tmp_path):
    upstream=tmp_path/"up.sock"; received=[]
    def handler(client):
        received.append(_read_headers(client))
        client.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\none\r\n3\r\ntwo\r\n0\r\n\r\n"); client.close()
    request=(b"POST /build HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
             b"3\r\none\r\n3\r\ntwo\r\n0\r\n\r\n")
    with _Daemon(upstream,handler), meter.DockerRequestMeter(upstream,_ledger(tmp_path),2,temporary_parent=tmp_path) as proxy:
        response=_send(proxy.socket_path,request)
    expected=(b"POST /build HTTP/1.1\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
              b"3\r\none\r\n3\r\ntwo\r\n0\r\n\r\n")
    assert received==[expected] and b"Connection: close" in response and response.endswith(b"3\r\ntwo\r\n0\r\n\r\n")


def test_upgrade_handshake_has_one_charge_and_duplex_tail(tmp_path):
    upstream=tmp_path/"up.sock"; seen=[]
    def handler(client):
        value=_read_headers(client); seen.append(value)
        client.sendall(b"HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
        assert client.recv(4)==b"ping"; client.sendall(b"pong"); client.close()
    ledger=_ledger(tmp_path)
    with _Daemon(upstream,handler), meter.DockerRequestMeter(upstream,ledger,2,temporary_parent=tmp_path) as proxy:
        response=_send(proxy.socket_path,b"POST /exec/start HTTP/1.1\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 0\r\n\r\nping")
    assert b"101 Switching Protocols" in response and response.endswith(b"pong")
    assert len(seen)==1 and ledger.status()["effective_resource_charges"]["control_requests"]==1


def test_failed_upstream_attempt_stays_charged_and_proxy_cleans_only_its_files(tmp_path):
    upstream=tmp_path/"up.sock"; outside=tmp_path/"keep"; outside.write_text("keep")
    def handler(client): client.close()
    ledger=_ledger(tmp_path)
    with _Daemon(upstream,handler), meter.DockerRequestMeter(upstream,ledger,1,temporary_parent=tmp_path) as proxy:
        directory=proxy.directory
        assert oct(directory.stat().st_mode&0o777)=="0o700" and oct(proxy.socket_path.stat().st_mode&0o777)=="0o600"
        _send(proxy.socket_path,b"GET /failure HTTP/1.1\r\n\r\n")
    assert ledger.status()["effective_resource_charges"]["control_requests"]==1
    assert outside.read_text()=="keep" and not directory.exists()


def test_cleanup_default_reservation_uses_only_reserved_control(tmp_path):
    upstream=tmp_path/"up.sock"
    def handler(client): _read_headers(client); client.sendall(b"HTTP/1.1 204 No Content\r\n\r\n"); client.close()
    active=_ledger(tmp_path,2,1); active.reserve({"control_requests":1},"active")
    cleanup=resource_budget.ResourceLedger(tmp_path/"config.json",tmp_path/"ledger.jsonl",default_cleanup=True)
    with _Daemon(upstream,handler), meter.DockerRequestMeter(upstream,cleanup,1,temporary_parent=tmp_path) as proxy:
        assert b"204" in _send(proxy.socket_path,b"GET /cleanup HTTP/1.1\r\n\r\n")
    assert cleanup.status()["effective_resource_charges"]["control_requests"]==2


def test_held_stream_does_not_block_independent_request(tmp_path):
    upstream=tmp_path/"up.sock"; held=threading.Event(); release=threading.Event(); response=[]
    def handler(client):
        request=_read_headers(client)
        if b"/stream " in request:
            client.sendall(b"HTTP/1.1 200 OK\r\n\r\nheld"); held.set(); release.wait(2)
        else:
            client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        client.close()
    ledger=_ledger(tmp_path)
    with _Daemon(upstream,handler), meter.DockerRequestMeter(upstream,ledger,2,temporary_parent=tmp_path) as proxy:
        stream=threading.Thread(target=lambda:response.append(_send(proxy.socket_path,b"GET /stream HTTP/1.1\r\n\r\n")))
        stream.start(); assert held.wait(1)
        assert _send(proxy.socket_path,b"GET /inspect HTTP/1.1\r\n\r\n").endswith(b"ok")
        assert stream.is_alive(); release.set(); stream.join(2)
    assert response[0].endswith(b"held") and ledger.status()["effective_resource_charges"]["control_requests"]==2


@pytest.mark.parametrize("wire_request,wire_response", [
    (b"HEAD /version HTTP/1.1\r\n\r\n",b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n"),
    (b"GET /204 HTTP/1.1\r\n\r\n",b"HTTP/1.1 204 No Content\r\n\r\n"),
])
def test_head_and_204_end_without_waiting_for_upstream_keepalive(tmp_path,wire_request,wire_response):
    upstream=tmp_path/"up.sock"; hold=threading.Event()
    def handler(client): _read_headers(client); client.sendall(wire_response); hold.wait(1); client.close()
    with _Daemon(upstream,handler), meter.DockerRequestMeter(upstream,_ledger(tmp_path),0.8,temporary_parent=tmp_path) as proxy:
        started=time.monotonic(); received=_send(proxy.socket_path,wire_request); elapsed=time.monotonic()-started
        hold.set()
    assert elapsed < 0.4 and b"Connection: close" in received


def test_expect_oversized_single_read_and_backpressure_fail_without_thread_leak(tmp_path):
    upstream=tmp_path/"up.sock"; dispatched=[]
    def handler(client):
        dispatched.append(_read_headers(client))
        try: client.sendall(b"HTTP/1.1 200 OK\r\n\r\n"+b"x"*(8*1024*1024))
        except OSError: pass
        client.close()
    ledger=_ledger(tmp_path,3)
    with _Daemon(upstream,handler) as daemon, meter.DockerRequestMeter(upstream,ledger,0.15,temporary_parent=tmp_path) as proxy:
        oversized=b"GET /large HTTP/1.1\r\nX-Value: "+b"a"*meter.MAX_HEADER_BYTES+b"\r\n\r\n"
        oversized_response=_send(proxy.socket_path,oversized)
        assert not oversized_response or b"400" in oversized_response
        expect=b"POST /expect HTTP/1.1\r\nExpect: 100-continue\r\nContent-Length: 1\r\n\r\nx"
        assert b"417" in _send(proxy.socket_path,expect)
        slow=socket.socket(socket.AF_UNIX); slow.connect(str(proxy.socket_path)); slow.sendall(b"GET /backpressure HTTP/1.1\r\n\r\n")
        time.sleep(0.3); slow.close(); directory=proxy.directory
    assert len(daemon.calls)==1 and len(dispatched)==1
    assert not directory.exists() and ledger.status()["effective_resource_charges"]["control_requests"]==2
