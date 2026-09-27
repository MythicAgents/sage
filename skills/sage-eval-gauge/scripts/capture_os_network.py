#!/usr/bin/env python3
"""Strict transient parser for complete process-observer network records."""
from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any


_CALLS = frozenset({"socket", "socketpair", "connect", "bind", "listen", "accept", "accept4", "sendto"})
_SOCKADDR_CALLS = frozenset({"connect", "bind", "accept", "accept4", "sendto"})
_LINE = re.compile(
    rb"^\s*(?P<pid>[1-9]\d*)\s+(?P<timestamp>\d+(?:\.\d+)?)\s+"
    rb"(?P<call>[a-z][a-z0-9_]*)\((?P<arguments>.*)\)\s+=\s+"
    rb"(?P<result>-?\d+)(?:<socket:\[(?P<result_inode>\d+)\]>)?"
    rb"(?:\s+(?P<errno>[A-Z][A-Z0-9_]+)\s+\([^\r\n]*\))?\s*$"
)
_FD = re.compile(rb"^(?P<fd>\d+)<socket:\[(?P<inode>\d+)\]>$")
_INTEGER = re.compile(rb"^(?:0|[1-9]\d*)$")
_FLAGS = re.compile(rb"^(?:0|[A-Z][A-Z0-9_]*(?:\|[A-Z][A-Z0-9_]*)*)$")
_INET = re.compile(rb'^\{sa_family=AF_INET, sin_port=htons\((?P<port>\d+)\), '
                   rb'sin_addr=inet_addr\("(?P<address>[^"\\]+)"\)\}$')
_INET6 = re.compile(rb'^\{sa_family=AF_INET6, sin6_port=htons\((?P<port>\d+)\), '
                    rb'sin6_flowinfo=htonl\((?P<flow>\d+)\), inet_pton\(AF_INET6, '
                    rb'"(?P<address>[^"\\]+)", &sin6_addr\), sin6_scope_id=(?P<scope>\d+)\}$')
_UNIX = re.compile(rb'^\{sa_family=AF_UNIX, sun_path=(?P<abstract>@)?(?P<path>"(?:\\.|[^"\\])*")\}$')
_PAIR = re.compile(rb'^\[(?P<a>\d+)<socket:\[(?P<ai>\d+)\]>, (?P<b>\d+)<socket:\[(?P<bi>\d+)\]>\]$')
_CLONE = re.compile(rb'^\s*(?P<parent>[1-9]\d*)\s+\d+(?:\.\d+)?\s+clone3?\(.*\bflags=(?P<flags>[A-Z0-9_|]+).*$')


def _unknown(call: str | None, reason: str) -> dict[str, Any]:
    return {"schema": "sage-os-network-fact-v1", "known": False, "call": call, "reason": reason}


def _split(value: bytes) -> list[bytes] | None:
    result: list[bytes] = []; start = 0; quote = escaped = False; stack: list[int] = []
    pairs = {ord("{"): ord("}"), ord("["): ord("]"), ord("("): ord(")")}
    for index, current in enumerate(value):
        if quote:
            if escaped: escaped = False
            elif current == ord("\\"): escaped = True
            elif current == ord('"'): quote = False
            continue
        if current == ord('"'): quote = True
        elif current in pairs: stack.append(pairs[current])
        elif current in (ord("}"), ord("]"), ord(")")):
            if not stack or stack.pop() != current: return None
        elif current == ord(",") and not stack:
            result.append(value[start:index].strip()); start = index + 1
    if quote or escaped or stack: return None
    result.append(value[start:].strip())
    return result


def _omitted(value: bytes) -> bool:
    quote = escaped = False
    for index, current in enumerate(value):
        if quote:
            if escaped: escaped = False
            elif current == ord("\\"): escaped = True
            elif current == ord('"'): quote = False
        elif current == ord('"'): quote = True
        elif value[index:index + 3] == b"...": return True
    return False


def _c_bytes(value: bytes) -> bytes | None:
    if len(value) < 2 or value[:1] != b'"' or value[-1:] != b'"': return None
    output = bytearray(); index = 1
    escapes = {ord("a"): 7, ord("b"): 8, ord("t"): 9, ord("n"): 10, ord("v"): 11,
               ord("f"): 12, ord("r"): 13, ord("\\"): 92, ord('"'): 34, ord("'"): 39, ord("?"): 63}
    while index < len(value) - 1:
        current = value[index]
        if current != ord("\\"):
            if current < 0x20 or current > 0x7e or current == ord('"'): return None
            output.append(current); index += 1; continue
        index += 1
        if index >= len(value) - 1: return None
        current = value[index]
        if current in escapes: output.append(escapes[current]); index += 1
        elif current == ord("x"):
            digits = value[index + 1:index + 3]
            if len(digits) != 2 or not re.fullmatch(rb"[0-9a-fA-F]{2}", digits): return None
            output.append(int(digits, 16)); index += 3
        elif ord("0") <= current <= ord("7"):
            end = index
            while end < min(index + 3, len(value) - 1) and ord("0") <= value[end] <= ord("7"): end += 1
            decoded = int(value[index:end], 8)
            if decoded > 255: return None
            output.append(decoded); index = end
        else: return None
    return bytes(output)


def _fd(value: bytes) -> tuple[int,int] | None:
    match = _FD.fullmatch(value)
    return (int(match.group("fd")),int(match.group("inode"))) if match else None


def _sockaddr(value: bytes, length: bytes) -> dict[str, Any] | None:
    if not _INTEGER.fullmatch(length): return None
    size = int(length)
    match = _INET.fullmatch(value)
    if match:
        port = int(match.group("port")); address = match.group("address").decode("ascii")
        try: parsed = ipaddress.ip_address(address)
        except ValueError: return None
        if not isinstance(parsed, ipaddress.IPv4Address) or parsed.compressed != address or port > 65535 or size != 16:
            return None
        return {"address_family": "AF_INET", "address": address, "port": port, "address_length": size}
    match = _INET6.fullmatch(value)
    if match:
        port = int(match.group("port")); address = match.group("address").decode("ascii")
        try: parsed = ipaddress.ip_address(address)
        except ValueError: return None
        if not isinstance(parsed, ipaddress.IPv6Address) or parsed.compressed != address or port > 65535 or size != 28:
            return None
        return {"address_family": "AF_INET6", "address": address, "port": port,
                "flowinfo": int(match.group("flow")), "scope_id": int(match.group("scope")), "address_length": size}
    match = _UNIX.fullmatch(value)
    if match:
        path = _c_bytes(match.group("path"))
        if path is None or not path or size < 2 or size > 110: return None
        return {"address_family": "AF_UNIX", "unix_path": path,
                "unix_abstract": bool(match.group("abstract")), "address_length": size}
    return None


class NetworkTraceParser:
    """Line parser with the minimum per-process socket-family lifecycle state."""

    def __init__(self) -> None:
        self._families: dict[tuple[int, int, int], str] = {}
        self._peers: dict[tuple[int, int, int], dict[str, Any]] = {}
        self._file_groups:dict[int,int]={}

    def observe_process_line(self,line:bytes)->bool:
        match=_CLONE.fullmatch(line)
        result=re.search(rb"\)\s+=\s+([1-9]\d*)\s*$",line) if match else None
        if result is None or b"CLONE_FILES" not in match.group("flags").split(b"|"):return False
        parent,child=int(match.group("parent")),int(result.group(1));self._file_groups[child]=self._owner(parent);return True

    def _owner(self,pid:int)->int:
        seen=set()
        while pid in self._file_groups and pid not in seen:
            seen.add(pid);pid=self._file_groups[pid]
        return pid

    def parse_line(self, line: bytes) -> dict[str, Any]:
        if not isinstance(line, bytes) or b"\r" in line or b"\n" in line or _omitted(line):
            return _unknown(None, "malformed_or_truncated")
        match = _LINE.fullmatch(line)
        if match is None:
            name = re.search(rb"\s([a-z][a-z0-9_]*)\(", line)
            return _unknown(name.group(1).decode("ascii") if name else None, "unsupported_or_malformed")
        call = match.group("call").decode("ascii")
        if call not in _CALLS:
            return _unknown(call, "unsupported_network_call")
        arguments = _split(match.group("arguments"))
        if arguments is None:
            return _unknown(call, "malformed_arguments")
        result = int(match.group("result")); errno = match.group("errno")
        if result < 0 and (result != -1 or errno is None) or result >= 0 and errno is not None:
            return _unknown(call, "malformed_result")
        pid = int(match.group("pid")); fact: dict[str, Any] = {
            "schema": "sage-os-network-fact-v1", "known": True, "pid": pid,
            "timestamp": match.group("timestamp").decode("ascii"), "call": call,
            "attempted": True, "syscall_status": "error" if result < 0 else "returned",
            "result": result, "errno": errno.decode("ascii") if errno else None,
        }
        if call == "socket":
            if len(arguments) != 3 or arguments[0] not in {b"AF_INET", b"AF_INET6", b"AF_UNIX"} \
                    or not _FLAGS.fullmatch(arguments[1]) or not _FLAGS.fullmatch(arguments[2]):
                return _unknown(call, "unsupported_socket_parameters")
            fact.update(address_family=arguments[0].decode(), socket_type=arguments[1].decode(),
                        protocol=arguments[2].decode(), fd=result if result >= 0 else None,
                        socket_inode=int(match.group("result_inode")) if match.group("result_inode") else None)
            if result >= 0 and fact["socket_inode"] is not None:
                owner=self._owner(pid)
                for key in [key for key in self._families if key[:2]==(owner,result)]:
                    self._families.pop(key,None);self._peers.pop(key,None)
                self._families[(owner,result,fact["socket_inode"])]=fact["address_family"];fact["descriptor_owner_pid"]=owner
            return fact
        if call=="socketpair":
            if (len(arguments)!=4 or arguments[0]!=b"AF_UNIX" or not _FLAGS.fullmatch(arguments[1])
                    or not _FLAGS.fullmatch(arguments[2]) or result!=0):return _unknown(call,"unsupported_socket_parameters")
            pair=_PAIR.fullmatch(arguments[3])
            if pair is None:return _unknown(call,"unresolved_descriptor")
            owner=self._owner(pid);pairs=[]
            for fd_name,inode_name in (("a","ai"),("b","bi")):
                fd,inode=int(pair.group(fd_name)),int(pair.group(inode_name));key=(owner,fd,inode)
                for old in [old for old in self._families if old[:2]==(owner,fd)]:self._families.pop(old,None);self._peers.pop(old,None)
                self._families[key]="AF_UNIX";self._peers[key]={"address_family":"AF_UNIX","peer_role":"local_socketpair"};pairs.append({"fd":fd,"socket_inode":inode})
            fact.update(address_family="AF_UNIX",socketpair=pairs,descriptor_owner_pid=owner);return fact
        descriptor = _fd(arguments[0]) if arguments else None
        if descriptor is None: return _unknown(call, "unresolved_descriptor")
        fd,inode=descriptor;owner=self._owner(pid);key=(owner,fd,inode);fact.update(fd=fd,socket_inode=inode,descriptor_owner_pid=owner)
        if call == "listen":
            if len(arguments) != 2 or not _INTEGER.fullmatch(arguments[1]):
                return _unknown(call, "malformed_listen")
            family = self._families.get(key)
            if family is None: return _unknown(call, "unresolved_socket_family")
            fact.update(address_family=family, backlog=int(arguments[1])); return fact
        positions = {"connect": (3, 1, 2), "bind": (3, 1, 2), "accept": (3, 1, 2),
                     "accept4": (4, 1, 2), "sendto": (6, 4, 5)}
        expected, address_index, length_index = positions[call]
        if len(arguments) != expected: return _unknown(call, "malformed_arguments")
        if call in {"accept","accept4"} and result==-1 and fact.get("errno") in {"EAGAIN","EWOULDBLOCK"}:
            if call=="accept4" and not _FLAGS.fullmatch(arguments[3]):return _unknown(call,"malformed_accept")
            fact["accepted_fd"]=None;fact["accepted_socket_inode"]=None;fact["no_peer_accepted"]=True
            return fact
        address_length = arguments[length_index]
        if call in {"accept", "accept4"}:
            length_match = re.fullmatch(rb"\[(\d+)\]", address_length)
            if length_match is None: return _unknown(call, "malformed_accept")
            address_length = length_match.group(1)
        connected_send = call == "sendto" and arguments[address_index] == b"NULL" and address_length == b"0"
        address = self._peers.get(key) if connected_send else _sockaddr(arguments[address_index], address_length)
        if address is None: return _unknown(call, "unsupported_or_malformed_sockaddr")
        tracked = self._families.get(key)
        if tracked is not None and tracked != address["address_family"]:
            return _unknown(call, "socket_family_mismatch")
        fact.update(address)
        if connected_send: fact["connected_socket"] = True
        elif call == "connect" and (result >= 0 or fact.get("errno") in {"EINPROGRESS","EALREADY"}):
            self._peers[key] = dict(address)
        if call == "sendto":
            if not _INTEGER.fullmatch(arguments[2]) or not _FLAGS.fullmatch(arguments[3]):
                return _unknown(call, "malformed_sendto")
            requested = int(arguments[2])
            if result > requested: return _unknown(call, "malformed_result")
            fact.update(requested_bytes=requested, flags=arguments[3].decode(), returned_bytes=result if result >= 0 else None)
        elif call == "accept" or call == "accept4":
            if not re.fullmatch(rb"\[\d+\]", arguments[2]) or call == "accept4" and not _FLAGS.fullmatch(arguments[3]):
                return _unknown(call, "malformed_accept")
            fact["accepted_fd"] = result if result >= 0 else None
            fact["accepted_socket_inode"]=int(match.group("result_inode")) if match.group("result_inode") else None
            if call == "accept4": fact["flags"] = arguments[3].decode()
            if result >= 0 and fact["accepted_socket_inode"] is not None:
                accepted=(pid,result,fact["accepted_socket_inode"]);self._families[accepted]=address["address_family"]
                self._peers[accepted]=dict(address)
        return fact


def parse_line(line: bytes) -> dict[str, Any]:
    """Parse one self-contained line; use NetworkTraceParser for listen lifecycle."""
    return NetworkTraceParser().parse_line(line)


def metadata_projection(fact: dict[str, Any]) -> dict[str, Any]:
    if fact.get("known") is not True:
        raise ValueError("network fact is not complete")
    result = {key: value for key, value in fact.items() if key not in {"address", "unix_path"}}
    if "address" in fact:
        result["address_handle"] = hashlib.sha256(
            (str(fact["address_family"]) + "\0" + str(fact["address"])).encode()
        ).hexdigest()
    if "unix_path" in fact:
        result["address_handle"] = hashlib.sha256(fact["unix_path"]).hexdigest()
        result["unix_path_length"] = len(fact["unix_path"])
    return result
