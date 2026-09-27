#!/usr/bin/env python3
"""Byte-transparent loopback relays for network-isolated local diagnostics."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import socket
import stat
import threading
import time


SCHEMA = "sage-diagnostic-loopback-relay-v1"
MAX_CONNECTIONS = 64
MAX_DIRECTION_BUFFER = 1024 * 1024


class DiagnosticLoopbackRelayError(RuntimeError):
    pass


def load_config(path: Path, sha256: str) -> dict:
    try:
        raw = path.read_bytes(); value = json.loads(raw)
        context = Path(value["context_directory"])
        metadata = os.stat(context, follow_symlinks=False)
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        raise DiagnosticLoopbackRelayError("loopback relay configuration is invalid") from None
    if (hashlib.sha256(raw).hexdigest() != sha256 or not isinstance(value, dict)
            or set(value) != {"schema", "context_directory", "ports", "deadline_monotonic"}
            or value.get("schema") != SCHEMA or not context.is_absolute()
            or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or not isinstance(value["ports"], list) or not value["ports"]
            or len(value["ports"]) > MAX_CONNECTIONS
            or any(type(port) is not int or not 1 <= port <= 65535 for port in value["ports"])
            or len(set(value["ports"])) != len(value["ports"])
            or type(value["deadline_monotonic"]) not in (int, float)
            or not math.isfinite(value["deadline_monotonic"])
            or value["deadline_monotonic"] <= time.monotonic()):
        raise DiagnosticLoopbackRelayError("loopback relay configuration is invalid")
    value["context_directory"] = context.resolve()
    return value


class _Relay:
    def __init__(self, config_path: Path, config_sha256: str, *, mode: str) -> None:
        self.config = load_config(config_path, config_sha256)
        if mode not in {"outer", "inner"}: raise DiagnosticLoopbackRelayError("loopback relay mode is invalid")
        self.mode = mode; self.stop = threading.Event(); self.listeners: list[socket.socket] = []
        self.connections: set[socket.socket] = set(); self.threads: list[threading.Thread] = []
        self.owned_paths: dict[Path, tuple[int, int]] = {}
        self.counts = {port: {"accepted": 0, "rejected": 0} for port in self.config["ports"]}
        self._lock = threading.Lock(); self._close_lock = threading.Lock()
        self._closing = False; self._closed = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def _socket_path(self, port: int) -> Path:
        return self.config["context_directory"] / f"loopback-{port}.sock"

    def __enter__(self):
        try:
            for port in self.config["ports"]:
                listener = socket.socket(socket.AF_UNIX if self.mode == "outer" else socket.AF_INET,
                                         socket.SOCK_STREAM)
                if self.mode == "outer":
                    path = self._socket_path(port)
                    if path.exists():
                        listener.close(); raise DiagnosticLoopbackRelayError("loopback relay socket is unavailable")
                    listener.bind(str(path)); os.chmod(path, 0o600)
                    metadata = os.stat(path, follow_symlinks=False)
                    self.owned_paths[path] = (metadata.st_dev, metadata.st_ino)
                else:
                    path = self._socket_path(port)
                    try: metadata = os.stat(path, follow_symlinks=False)
                    except OSError:
                        listener.close(); raise DiagnosticLoopbackRelayError("loopback relay socket is unavailable") from None
                    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
                        listener.close(); raise DiagnosticLoopbackRelayError("loopback relay socket is unavailable")
                    listener.bind(("127.0.0.1", port))
                listener.listen(MAX_CONNECTIONS); listener.settimeout(.1); self.listeners.append(listener)
                thread = threading.Thread(target=self._accept, args=(listener, port), daemon=True)
                thread.start(); self.threads.append(thread)
            expiry = threading.Thread(target=self._expire, daemon=True)
            expiry.start()
            return self
        except BaseException:
            self.close(); raise

    def _accept(self, listener: socket.socket, port: int) -> None:
        while not self.stop.is_set() and time.monotonic() < self.config["deadline_monotonic"]:
            try: incoming, _ = listener.accept()
            except socket.timeout: continue
            except OSError: break
            if not self._slots.acquire(blocking=False):
                self.counts[port]["rejected"] += 1; incoming.close(); continue
            self.counts[port]["accepted"] += 1
            with self._lock: self.connections.add(incoming)
            thread = threading.Thread(target=self._connect_and_forward, args=(incoming, port), daemon=True)
            thread.start(); self.threads.append(thread)

    def _connect_and_forward(self, incoming: socket.socket, port: int) -> None:
        peer = socket.socket(socket.AF_INET if self.mode == "outer" else socket.AF_UNIX, socket.SOCK_STREAM)
        with self._lock: self.connections.add(peer)
        try:
            peer.settimeout(1)
            peer.connect(("127.0.0.1", port) if self.mode == "outer" else str(self._socket_path(port)))
            incoming.setblocking(False); peer.setblocking(False)
            selector = selectors.DefaultSelector()
            selector.register(incoming, selectors.EVENT_READ, peer)
            selector.register(peer, selectors.EVENT_READ, incoming)
            peers = {incoming: peer, peer: incoming}
            buffers = {incoming: bytearray(), peer: bytearray()}
            ended: set[socket.socket] = set(); shutdown: set[socket.socket] = set()
            failed = False
            while not self.stop.is_set() and time.monotonic() < self.config["deadline_monotonic"] \
                    and selector.get_map():
                for key, mask in selector.select(.1):
                    source, target = key.fileobj, key.data
                    if mask & selectors.EVENT_READ:
                        room = MAX_DIRECTION_BUFFER - len(buffers[target])
                        try: data = source.recv(min(65536, room)) if room else None
                        except BlockingIOError: data = None
                        except OSError: data = b""
                        if data == b"": ended.add(source)
                        elif data: buffers[target].extend(data)
                    if mask & selectors.EVENT_WRITE and buffers[source]:
                        try: sent = source.send(buffers[source])
                        except BlockingIOError: sent = 0
                        except OSError: failed = True; break
                        if sent: del buffers[source][:sent]
                if failed: break
                for source, target in peers.items():
                    if source in ended and not buffers[target] and target not in shutdown:
                        try: target.shutdown(socket.SHUT_WR)
                        except OSError: pass
                        shutdown.add(target)
                    events = (selectors.EVENT_READ if source not in ended
                              and len(buffers[target]) < MAX_DIRECTION_BUFFER else 0)
                    if buffers[source]: events |= selectors.EVENT_WRITE
                    try: selector.get_key(source)
                    except KeyError:
                        if events: selector.register(source, events, target)
                    else:
                        if events: selector.modify(source, events, target)
                        else: selector.unregister(source)
                if len(ended) == 2 and not buffers[incoming] and not buffers[peer]: break
            selector.close()
        except OSError:
            pass
        finally:
            for item in (incoming, peer):
                with self._lock: self.connections.discard(item)
                try: item.close()
                except OSError: pass
            self._slots.release()

    def _expire(self) -> None:
        remaining = max(0, self.config["deadline_monotonic"] - time.monotonic())
        if not self.stop.wait(remaining): self.close()

    def receipt(self) -> dict:
        return {"schema": SCHEMA, "mode": self.mode, "listener_ready": len(self.listeners) == len(self.config["ports"])
                and all(listener.fileno() >= 0 for listener in self.listeners),
                "bindings": [{"id": f"loopback-{port}", **self.counts[port]}
                             for port in self.config["ports"]]}

    def close(self) -> None:
        with self._close_lock:
            if self._closing:
                closer = False
            else:
                self._closing = True; closer = True
        if not closer:
            self._closed.wait(2)
            return
        try:
            self.stop.set()
            with self._lock:
                for connection in tuple(self.connections):
                    try: connection.close()
                    except OSError: pass
            for listener in self.listeners:
                try: listener.close()
                except OSError: pass
            deadline = time.monotonic() + 2
            current = threading.current_thread()
            for thread in self.threads:
                if thread is not current: thread.join(max(0, deadline - time.monotonic()))
            if self.mode == "outer":
                for path, identity in tuple(self.owned_paths.items()):
                    try: metadata = os.stat(path, follow_symlinks=False)
                    except FileNotFoundError: continue
                    if stat.S_ISSOCK(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == identity:
                        try: path.unlink()
                        except FileNotFoundError: pass
            self.owned_paths.clear(); self.listeners.clear()
        finally:
            self._closed.set()

    def __exit__(self, *_args) -> None:
        self.close()


class OuterRelay(_Relay):
    def __init__(self, config_path: Path, config_sha256: str):
        super().__init__(config_path, config_sha256, mode="outer")


class InnerRelay(_Relay):
    def __init__(self, config_path: Path, config_sha256: str):
        super().__init__(config_path, config_sha256, mode="inner")
