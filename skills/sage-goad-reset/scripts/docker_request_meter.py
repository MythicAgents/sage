#!/usr/bin/env python3
"""Owner-only Unix proxy that meters Docker HTTP request handshakes.

Each downstream connection carries one HTTP/1 request.  Upgrade traffic is
relayed bidirectionally after that single charged handshake.  The proxy adds no
request authorization; callers must separately bind the exact Docker command.
"""
from __future__ import annotations

import os
import errno
from pathlib import Path
import select
import socket
import stat
import sys
import tempfile
import threading
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "Payload_Type" / "sage"))
from ai import resource_budget  # noqa: E402


MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_CHUNK_LINE_BYTES = 8192
MAX_CONCURRENT_CONNECTIONS = 16
_TOKEN = frozenset(b"!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ERRORS = {
    "bad": b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    "denied": b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    "upstream": b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    "expect": b"HTTP/1.1 417 Expectation Failed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    "busy": b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
}


class DockerRequestMeterError(RuntimeError):
    pass


class _ReadFailure(DockerRequestMeterError):
    pass


class _Reader:
    def __init__(self, connection: socket.socket, initial: bytes = b"") -> None:
        self.connection = connection
        self.buffer = bytearray(initial)

    def _fill(self, deadline: float) -> None:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ReadFailure("timeout")
            ready, _, _ = select.select([self.connection], [], [], remaining)
            if not ready:
                raise _ReadFailure("timeout")
            try:
                data = self.connection.recv(65536)
            except BlockingIOError:
                continue
            except OSError:
                raise _ReadFailure("read") from None
            if not data:
                raise _ReadFailure("closed")
            self.buffer.extend(data); return

    def exact(self, size: int, deadline: float) -> bytes:
        while len(self.buffer) < size:
            self._fill(deadline)
        value = bytes(self.buffer[:size]); del self.buffer[:size]
        return value

    def line(self, limit: int, deadline: float) -> bytes:
        while True:
            marker = self.buffer.find(b"\r\n")
            if marker >= 0:
                if marker + 2 > limit:
                    raise _ReadFailure("line")
                value = bytes(self.buffer[:marker + 2]); del self.buffer[:marker + 2]
                return value
            if len(self.buffer) >= limit:
                raise _ReadFailure("line")
            self._fill(deadline)


def _headers(reader: _Reader, deadline: float) -> tuple[bytes, bytes, list[tuple[bytes, bytes]]]:
    while True:
        marker = reader.buffer.find(b"\r\n\r\n")
        if marker >= 0:
            if marker + 4 > MAX_HEADER_BYTES:
                raise _ReadFailure("header")
            raw = bytes(reader.buffer[:marker + 4]); remainder = bytes(reader.buffer[marker + 4:])
            reader.buffer.clear()
            break
        if len(reader.buffer) >= MAX_HEADER_BYTES:
            raise _ReadFailure("header")
        reader._fill(deadline)
    lines = raw[:-4].split(b"\r\n")
    if not lines or not lines[0]:
        raise _ReadFailure("request")
    values = []
    for line in lines[1:]:
        if b":" not in line or line[:1] in b" \t":
            raise _ReadFailure("header")
        name, value = line.split(b":", 1)
        if not name or any(character not in _TOKEN for character in name):
            raise _ReadFailure("header")
        values.append((name.lower(), value.strip()))
    return raw, remainder, values


def _request_line(raw: bytes) -> tuple[bytes, bytes]:
    line = raw.split(b"\r\n", 1)[0]
    parts = line.split(b" ")
    if len(parts) != 3 or parts[2] != b"HTTP/1.1" or not parts[0] \
            or any(character not in _TOKEN for character in parts[0]):
        raise _ReadFailure("request")
    method, path = parts[0].upper(), parts[1]
    if method == b"CONNECT" or (not path.startswith(b"/") and path != b"*") or b"://" in path:
        raise _ReadFailure("tunnel")
    return method, path


def _framing(headers: list[tuple[bytes, bytes]]) -> tuple[str, int]:
    lengths = [value for name, value in headers if name == b"content-length"]
    transfers = [value for name, value in headers if name == b"transfer-encoding"]
    if transfers and lengths or len(lengths) > 1 or len(transfers) > 1:
        raise _ReadFailure("framing")
    if lengths:
        if not lengths[0].isdigit():
            raise _ReadFailure("framing")
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            raise _ReadFailure("body")
        return "length", length
    if transfers:
        if transfers[0].lower() != b"chunked":
            raise _ReadFailure("framing")
        return "chunked", 0
    return "none", 0


def _chunked(reader: _Reader, deadline: float, sink=None) -> bytes:
    result, decoded = bytearray(), 0
    while True:
        line = reader.line(MAX_CHUNK_LINE_BYTES, deadline)
        try:
            size = int(line[:-2].split(b";", 1)[0], 16)
        except ValueError:
            raise _ReadFailure("chunk") from None
        if size < 0 or decoded + size > MAX_BODY_BYTES:
            raise _ReadFailure("body")
        if size == 0:
            if sink is None:
                result.extend(line)
            else:
                _send_all(sink, line, deadline)
            trailer_size = 0
            while True:
                trailer = reader.line(MAX_CHUNK_LINE_BYTES, deadline)
                trailer_size += len(trailer)
                if trailer_size > MAX_HEADER_BYTES:
                    raise _ReadFailure("header")
                if sink is None:
                    result.extend(trailer)
                else:
                    _send_all(sink, trailer, deadline)
                if trailer == b"\r\n":
                    return bytes(result)
        data = reader.exact(size + 2, deadline)
        if not data.endswith(b"\r\n"):
            raise _ReadFailure("chunk")
        value = line + data
        if sink is None:
            result.extend(value)
            if len(result) > MAX_BODY_BYTES + MAX_HEADER_BYTES:
                raise _ReadFailure("body")
        else:
            _send_all(sink, value, deadline)
        decoded += size


def _valid_response(raw: bytes, headers: list[tuple[bytes, bytes]], method: bytes) -> tuple[int, str, int]:
    line = raw.split(b"\r\n", 1)[0]; parts = line.split(b" ", 2)
    if len(parts) < 2 or parts[0] != b"HTTP/1.1" or len(parts[1]) != 3 or not parts[1].isdigit():
        raise _ReadFailure("response")
    status = int(parts[1])
    framing, length = _framing(headers)
    if status == 101:
        framing, length = "upgrade", 0
    elif method == b"HEAD" or status in (204, 304):
        framing, length = "zero", 0
    elif 100 <= status < 200:
        raise _ReadFailure("interim")
    return status, framing, length


def _connection_close(raw: bytes) -> bytes:
    lines = raw[:-4].split(b"\r\n")
    lines = [lines[0], *(line for line in lines[1:] if line.split(b":", 1)[0].lower() != b"connection")]
    return b"\r\n".join([*lines, b"Connection: close", b"", b""])


def _send_all(connection: socket.socket, value: bytes, deadline: float) -> None:
    offset = 0
    while offset < len(value):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [connection], [], remaining)[1]:
            raise _ReadFailure("backpressure")
        try:
            count = connection.send(value[offset:])
        except BlockingIOError:
            continue
        except OSError:
            raise _ReadFailure("send") from None
        if count <= 0:
            raise _ReadFailure("send")
        offset += count


class DockerRequestMeter:
    def __init__(self, upstream_socket: str | Path, ledger: Any, timeout: float, *, temporary_parent=None):
        self.upstream_socket = Path(upstream_socket)
        if not self.upstream_socket.is_absolute() or not isinstance(timeout, (int, float)) \
                or isinstance(timeout, bool) or not 0 < timeout <= 86400:
            raise DockerRequestMeterError("Docker proxy configuration is invalid")
        try:
            metadata = os.stat(self.upstream_socket, follow_symlinks=False)
        except OSError:
            raise DockerRequestMeterError("Docker upstream socket is unavailable") from None
        if not stat.S_ISSOCK(metadata.st_mode) or not hasattr(ledger, "reserve"):
            raise DockerRequestMeterError("Docker proxy configuration is invalid")
        self.ledger, self.timeout = ledger, float(timeout)
        self.temporary_parent = temporary_parent
        self.directory: Path | None = None
        self.socket_path: Path | None = None
        self.docker_host: str | None = None
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connections: set[socket.socket] = set()
        self._handlers: set[threading.Thread] = set()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_CONNECTIONS)
        self._guard = threading.Lock()

    def __enter__(self):
        parent = None if self.temporary_parent is None else str(Path(self.temporary_parent))
        directory = path = listener = None
        try:
            directory = Path(tempfile.mkdtemp(prefix="sage-docker-meter-", dir=parent)); os.chmod(directory, 0o700)
            path = directory / "docker.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); listener.bind(str(path)); os.chmod(path, 0o600)
            listener.listen(); listener.settimeout(0.1)
        except OSError:
            if listener is not None: listener.close()
            if path is not None:
                try: os.unlink(path)
                except OSError: pass
            if directory is not None:
                try: os.rmdir(directory)
                except OSError: pass
            raise DockerRequestMeterError("Docker proxy could not listen") from None
        self.directory, self.socket_path, self.docker_host, self._listener = directory, path, f"unix://{path}", listener
        self._thread = threading.Thread(target=self._serve, daemon=True); self._thread.start()
        return self

    def _serve(self):
        while not self._stop.is_set():
            try:
                downstream, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if self._stop.is_set():
                downstream.close(); break
            downstream.setblocking(False)
            if not self._slots.acquire(blocking=False):
                try: _send_all(downstream, _ERRORS["busy"], time.monotonic() + 0.1)
                except _ReadFailure: pass
                downstream.close(); continue
            with self._guard:
                self._connections.add(downstream)
            thread = threading.Thread(target=self._run_handler, args=(downstream,), daemon=True)
            with self._guard:
                self._handlers.add(thread)
            thread.start()

    def _run_handler(self, downstream: socket.socket):
        try:
            self._handle(downstream)
        finally:
            current = threading.current_thread()
            with self._guard:
                self._connections.discard(downstream); self._handlers.discard(current)
            downstream.close(); self._slots.release()

    @staticmethod
    def _reply(connection: socket.socket, kind: str, deadline: float):
        try:
            _send_all(connection, _ERRORS[kind], deadline)
        except (OSError, _ReadFailure):
            pass

    def _handle(self, downstream: socket.socket):
        deadline = time.monotonic() + self.timeout
        reader = _Reader(downstream)
        try:
            raw, initial, headers = _headers(reader, deadline)
            method, _path = _request_line(raw)
        except _ReadFailure:
            self._reply(downstream, "bad", deadline); return
        try:
            self.ledger.reserve({"control_requests": 1}, "docker-http-request")
        except resource_budget.ResourceBudgetError:
            self._reply(downstream, "denied", deadline); return
        try:
            framing, length = _framing(headers)
            if any(name == b"expect" for name, _value in headers):
                self._reply(downstream, "expect", deadline); return
            reader.buffer.extend(initial)
            body = reader.exact(length, deadline) if framing == "length" else (
                _chunked(reader, deadline) if framing == "chunked" else b""
            )
            upgrade = any(name == b"upgrade" for name, _value in headers) and any(
                name == b"connection" and b"upgrade" in {item.strip().lower() for item in value.split(b",")}
                for name, value in headers
            )
            tail = bytes(reader.buffer)
            if tail and not upgrade:
                raise _ReadFailure("pipeline")
        except _ReadFailure:
            self._reply(downstream, "bad", deadline); return
        upstream = None
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); upstream.setblocking(False)
            with self._guard: self._connections.add(upstream)
            code = upstream.connect_ex(str(self.upstream_socket))
            if code not in (0, errno.EINPROGRESS, errno.EAGAIN, errno.EWOULDBLOCK):
                raise OSError(code, "connect")
            if code != 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [upstream], [], remaining)[1] \
                        or upstream.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                    raise OSError("connect")
            request_raw = raw if upgrade else _connection_close(raw)
            _send_all(upstream, request_raw + body, deadline)
        except (OSError, _ReadFailure):
            if upstream is not None:
                with self._guard: self._connections.discard(upstream)
                upstream.close()
            self._reply(downstream, "upstream", deadline); return
        try:
            try:
                response_reader = _Reader(upstream)
                response_raw, response_initial, response_headers = _headers(response_reader, deadline)
                status, response_framing, response_length = _valid_response(response_raw, response_headers, method)
                _send_all(downstream, response_raw if status == 101 else _connection_close(response_raw), deadline)
                response_reader.buffer.extend(response_initial)
                if status == 101:
                    if method == b"CONNECT" or not upgrade:
                        raise _ReadFailure("upgrade")
                    if tail:
                        _send_all(upstream, tail, deadline)
                    if response_reader.buffer:
                        _send_all(downstream, bytes(response_reader.buffer), deadline); response_reader.buffer.clear()
                    self._duplex(downstream, upstream, deadline); return
                if response_framing == "length":
                    _send_all(downstream, response_reader.exact(response_length, deadline), deadline)
                elif response_framing == "chunked":
                    _chunked(response_reader, deadline, sink=downstream)
                elif response_framing == "none" and response_reader.buffer:
                    _send_all(downstream, bytes(response_reader.buffer), deadline)
                if response_framing == "none":
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0: break
                        ready, _, _ = select.select([upstream], [], [], remaining)
                        if not ready: break
                        data = upstream.recv(65536)
                        if not data: break
                        _send_all(downstream, data, deadline)
            except (OSError, _ReadFailure):
                return
        finally:
            with self._guard: self._connections.discard(upstream)
            upstream.close()

    @staticmethod
    def _duplex(downstream: socket.socket, upstream: socket.socket, deadline: float):
        peers = [downstream, upstream]
        while peers and time.monotonic() < deadline:
            ready, _, _ = select.select(peers, [], [], deadline - time.monotonic())
            if not ready: return
            for source in ready:
                try:
                    data = source.recv(65536)
                except OSError:
                    return
                if not data: return
                target = upstream if source is downstream else downstream
                try:
                    _send_all(target, data, deadline)
                except (OSError, _ReadFailure):
                    return

    def close(self):
        self._stop.set()
        if self._listener is not None:
            self._listener.close(); self._listener = None
        if self._thread is not None:
            self._thread.join(timeout=1); self._thread = None
        with self._guard:
            connections, handlers = tuple(self._connections), tuple(self._handlers)
        for connection in connections:
            try: connection.close()
            except OSError: pass
        deadline = time.monotonic() + 2
        for handler in handlers:
            handler.join(timeout=max(0, deadline - time.monotonic()))
        if self.directory is not None:
            directory, self.directory = self.directory, None
            try:
                if self.socket_path is not None: os.unlink(self.socket_path)
            except OSError: pass
            try: os.rmdir(directory)
            except OSError: pass
            self.socket_path = None; self.docker_host = None

    def __exit__(self, exc_type, exc, traceback):
        self.close()
