from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "skills/sage-eval-gauge/scripts/capture_os_network.py"
SPEC = importlib.util.spec_from_file_location("capture_os_network", SOURCE)
assert SPEC and SPEC.loader
network = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(network)


def test_actual_loopback_attempts_known(tmp_path):
    binary = shutil.which("strace")
    if not binary: pytest.skip("strace unavailable")
    trace, unix_path = tmp_path / "trace", tmp_path / "synthetic.sock"
    code = """import os,socket,sys
r=socket.socket();r.bind(('127.0.0.1',0));bad=r.getsockname()[1];r.close()
s=socket.socket()
try:s.connect(('127.0.0.1',bad))
except OSError:pass
srv=socket.socket();srv.bind(('127.0.0.1',0));srv.listen(1);port=srv.getsockname()[1]
c=socket.socket();c.connect(('127.0.0.1',port));conn,_=srv.accept();c.sendall(b'abc');conn.recv(3);conn.close();c.close()
u=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);u.sendto(b'abc',('127.0.0.1',port))
a=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);b=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);b.bind(sys.argv[1]);a.sendto(b'xy',sys.argv[1])
"""
    subprocess.run([binary, "-f", "-qq", "-ttt", "--decode-fds=path", "-s", "1048576",
                    "-e", "trace=%network", "-o", str(trace), sys.executable, "-c", code, str(unix_path)],
                   check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60)
    parser = network.NetworkTraceParser()
    facts = [parser.parse_line(line) for line in trace.read_bytes().splitlines()]
    known = [item for item in facts if item["known"]]
    assert {"socket", "bind", "listen", "connect", "sendto"} <= {item["call"] for item in known}
    assert any(item["call"] == "connect" and item["errno"] == "ECONNREFUSED" for item in known)
    assert any(item["call"] == "connect" and item["result"] == 0 for item in known)
    assert any(item["call"] == "sendto" and item.get("connected_socket") is True
               and item.get("address") == "127.0.0.1" for item in known)
    assert any(item["call"] == "sendto" and item["address_family"] == "AF_INET"
               and item["returned_bytes"] == 3 for item in known)
    unix = next(item for item in known if item["call"] == "sendto" and item["address_family"] == "AF_UNIX")
    assert unix["unix_path"] == os.fsencode(str(unix_path))
    durable = network.metadata_projection(unix)
    assert "unix_path" not in durable and durable["unix_path_length"] == len(unix["unix_path"])
    assert os.fsencode(str(unix_path)) not in json.dumps(durable).encode()
    accepted = network.parse_line(
        b'42 1770000000.3 accept4(3<socket:[9]>, {sa_family=AF_INET, '
        b'sin_port=htons(2345), sin_addr=inet_addr("127.0.0.1")}, [16], SOCK_CLOEXEC) '
        b'= 4<socket:[10]>'
    )
    assert accepted["known"] and accepted["accepted_fd"] == 4


def test_ipv6_uses_sockaddr_not_payload_lookalike():
    line = (b'42 1770000000.2 sendto(3<socket:[9]>, "fake...address", 14, 0, '
            b'{sa_family=AF_INET6, sin6_port=htons(9), sin6_flowinfo=htonl(0), '
            b'inet_pton(AF_INET6, "::1", &sin6_addr), sin6_scope_id=0}, 28) = 14')
    fact = network.parse_line(line)
    assert fact["known"] and fact["address_family"] == "AF_INET6" and fact["address"] == "::1"
    assert "address" not in network.metadata_projection(fact)


@pytest.mark.parametrize("line,reason", [
    (b'42 1.0 connect(3, {sa_family=AF_INET, sin_port=htons(9), sin_addr=inet_addr("127.0.0.1")}, 16) = 0', "unresolved_descriptor"),
    (b'42 1.0 connect(3<socket:[9]>, {sa_family=AF_NET, port=9}, 16) = 0', "unsupported_or_malformed_sockaddr"),
    (b'42 1.0 connect(3<socket:[9]>, {sa_family=AF_INET, sin_port=htons(70000), sin_addr=inet_addr("127.0.0.1")}, 16) = 0', "unsupported_or_malformed_sockaddr"),
    (b'42 1.0 connect(3<socket:[9]>, {sa_family=AF_INET, sin_port=htons(9), sin_addr=inet_addr("127.0.0.1")}, 16 <unfinished ...>', "malformed_or_truncated"),
    (b'42 1.0 sendmsg(3<socket:[9]>, opaque) = 1', "unsupported_network_call"),
])
def test_invalid_and_opaque_calls_typed_unknown(line, reason):
    fact = network.parse_line(line)
    assert not fact["known"] and fact["reason"] == reason
    assert not any(key in fact for key in ("address", "unix_path", "port"))


def test_unix_path_near_match_payload_does_not_change_target():
    line = (b'42 1.0 sendto(3<socket:[9]>, "/tmp/fake.sock", 14, 0, '
            b'{sa_family=AF_UNIX, sun_path="/tmp/actual.sock"}, 19) = 14')
    fact = network.parse_line(line)
    assert fact["known"] and fact["unix_path"] == b"/tmp/actual.sock"

def test_reused_fd_with_new_socket_inode_cannot_inherit_old_peer():
    parser=network.NetworkTraceParser()
    assert parser.parse_line(b'42 1.0 socket(AF_INET, SOCK_STREAM, IPPROTO_IP) = 7<socket:[9]>')["known"]
    assert parser.parse_line(b'42 1.1 connect(7<socket:[9]>, {sa_family=AF_INET, sin_port=htons(16006), sin_addr=inet_addr("127.0.0.1")}, 16) = 0')["known"]
    assert parser.parse_line(b'42 1.2 socket(AF_INET, SOCK_STREAM, IPPROTO_IP) = 7<socket:[10]>')["known"]
    result=parser.parse_line(b'42 1.3 sendto(7<socket:[10]>, "abc", 3, 0, NULL, 0) = 3')
    assert result["known"] is False and result["reason"]=="unsupported_or_malformed_sockaddr"

def test_retryable_failed_accept_has_no_peer_or_request_effect():
    result=network.NetworkTraceParser().parse_line(b'42 1.3 accept4(7<socket:[9]>, 0x7fff1234, [128], SOCK_CLOEXEC) = -1 EAGAIN (Resource temporarily unavailable)')
    assert result["known"] and result["no_peer_accepted"] is True and result["accepted_fd"] is None

def test_actual_socketpair_clone_files_and_unshared_reuse_are_distinguished(tmp_path):
    observer_spec=importlib.util.spec_from_file_location("socketpair_process_observer",ROOT/"skills/sage-eval-gauge/scripts/process_observer.py")
    observer=importlib.util.module_from_spec(observer_spec);observer_spec.loader.exec_module(observer);parser=network.NetworkTraceParser();facts=[]
    code="""import os,socket,threading
a,b=socket.socketpair();t=threading.Thread(target=lambda:a.send(b'x'));t.start();t.join();b.recv(1)
pid=os.fork()
if pid==0:a.send(b'y');os._exit(0)
os.waitpid(pid,0);b.recv(1);old=a.fileno();a.close();c=socket.socket();assert c.fileno()==old
try:c.send(b'z')
except OSError:pass
"""
    def handler(kind,line):
        if kind=="process_lifecycle":parser.observe_process_line(line)
        elif kind=="network_attempt":facts.append(parser.parse_line(line))
    result=observer.observe([sys.executable,"-P","-c",code],handler,timeout_seconds=20,max_events=10000,max_trace_bytes=4*1024*1024,stdout_mode="devnull",stderr_mode="devnull")
    assert result["complete"] is True
    sends=[x for x in facts if x.get("call")=="sendto"]
    assert any(x.get("known") and x.get("connected_socket") and x.get("peer_role")=="local_socketpair" for x in sends)
    assert any(not x.get("known") and x.get("reason")=="unsupported_or_malformed_sockaddr" for x in sends)
