#!/usr/bin/env python3
"""Strict transient decoding for complete strace write records.

This module reconstructs bytes attempted at one decoded file descriptor.  It
does not classify a sink, retain a payload, or infer whether a write had an
effect.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
from collections.abc import Mapping, Sequence
from typing import Any


_CALLS = frozenset({"write", "pwrite64", "writev", "pwritev", "pwritev2"})
_UNSUPPORTED = frozenset({"sendfile", "splice", "tee", "vmsplice", "copy_file_range"})
_LINE = re.compile(
    rb"^\s*(?P<pid>[1-9]\d*)\s+(?P<timestamp>\d+(?:\.\d+)?)\s+"
    rb"(?P<call>[a-z][a-z0-9_]*)\((?P<arguments>.*)\)\s+=\s+"
    rb"(?P<result>-?\d+)(?:\s+(?P<errno>[A-Z][A-Z0-9_]+)\s+\([^\r\n]*\))?\s*$"
)
_FD = re.compile(rb"^(?P<fd>\d+)<(?P<path>[^<>\r\n]+)>$")
_INTEGER = re.compile(rb"^(?:0|[1-9]\d*)$")
_OFFSET = re.compile(rb"^-?(?:0|[1-9]\d*)$")
_MUTATION_LINE = re.compile(
    rb"^\s*(?P<pid>[1-9]\d*)\s+(?P<timestamp>\d+(?:\.\d+)?)\s+"
    rb"(?P<call>[a-z][a-z0-9_]*)\((?P<arguments>.*)\)\s+=\s+(?P<result>-?\d+)"
    rb"(?:<(?P<resolved>[^<>\r\n]+)>)?(?:\s+(?P<errno>[A-Z][A-Z0-9_]+)\s+\([^\r\n]*\))?\s*$")
_MUTATION_CALLS=frozenset({"open","openat","mkdir","mkdirat","unlink","unlinkat",
                           "rename","renameat","renameat2"})
_DIRFD=re.compile(rb"^(?:[0-9]+)<([^<>\r\n]+)>$")
_MMAP_LINE=re.compile(rb"^\s*(?P<pid>[1-9]\d*)\s+(?P<timestamp>\d+(?:\.\d+)?)\s+mmap\(NULL, (?P<length>[1-9]\d*), (?P<protection>[A-Z|0-9_]+), (?P<flags>[A-Z|0-9_]+), (?P<fd>\d+)<(?P<path>[^<>\r\n]+)>, (?P<offset>\d+)\) = (?:(?P<address>0x[0-9a-f]+)|-1 (?P<errno>[A-Z][A-Z0-9_]+) \([^\r\n]*\))\s*$")


def _unknown(call: str | None, reason: str) -> dict[str, Any]:
    return {"schema": "sage-os-write-fact-v1", "known": False, "call": call, "reason": reason,
            "attempted_content": None}


def _split(arguments: bytes) -> list[bytes] | None:
    values: list[bytes] = []
    start = 0
    quote = escaped = False
    stack: list[int] = []
    pairs = {ord("["): ord("]"), ord("{"): ord("}")}
    for index, value in enumerate(arguments):
        if quote:
            if escaped:
                escaped = False
            elif value == ord("\\"):
                escaped = True
            elif value == ord('"'):
                quote = False
            continue
        if value == ord('"'):
            quote = True
        elif value in pairs:
            stack.append(pairs[value])
        elif value in (ord("]"), ord("}")):
            if not stack or stack.pop() != value:
                return None
        elif value == ord(",") and not stack:
            values.append(arguments[start:index].strip()); start = index + 1
    if quote or stack or escaped:
        return None
    values.append(arguments[start:].strip())
    return values


def _omitted(arguments: bytes) -> bool:
    quote = escaped = False
    for index, value in enumerate(arguments):
        if quote:
            if escaped:
                escaped = False
            elif value == ord("\\"):
                escaped = True
            elif value == ord('"'):
                quote = False
        elif value == ord('"'):
            quote = True
        elif arguments[index:index + 3] == b"...":
            return True
    return False


def _c_bytes(value: bytes) -> bytes | None:
    if len(value) < 2 or value[:1] != b'"' or value[-1:] != b'"':
        return None
    output = bytearray(); index = 1
    simple = {ord("a"): 7, ord("b"): 8, ord("t"): 9, ord("n"): 10, ord("v"): 11,
              ord("f"): 12, ord("r"): 13, ord("\\"): 92, ord('"'): 34, ord("'"): 39,
              ord("?"): 63}
    while index < len(value) - 1:
        current = value[index]
        if current != ord("\\"):
            if current > 0x7f or current < 0x20 or current == ord('"'):
                return None
            output.append(current); index += 1; continue
        index += 1
        if index >= len(value) - 1:
            return None
        current = value[index]
        if current in simple:
            output.append(simple[current]); index += 1
        elif current == ord("x"):
            if index + 2 >= len(value) - 1:
                return None
            digits = value[index + 1:index + 3]
            if not re.fullmatch(rb"[0-9a-fA-F]{2}", digits):
                return None
            output.append(int(digits, 16)); index += 3
        elif ord("0") <= current <= ord("7"):
            end = index
            while end < min(index + 3, len(value) - 1) and ord("0") <= value[end] <= ord("7"):
                end += 1
            decoded = int(value[index:end], 8)
            if decoded > 255:
                return None
            output.append(decoded); index = end
        else:
            return None
    return bytes(output)


def _descriptor(value: bytes) -> tuple[int, bytes] | None:
    match = _FD.fullmatch(value)
    return (int(match.group("fd")), match.group("path")) if match else None


def _iovecs(value: bytes, expected_count: int) -> tuple[bytes, list[int]] | None:
    if value[:1] != b"[" or value[-1:] != b"]":
        return None
    members = _split(value[1:-1])
    if members is None or len(members) != expected_count:
        return None
    content = bytearray(); lengths: list[int] = []
    for member in members:
        fields = _split(member[1:-1]) if member[:1] == b"{" and member[-1:] == b"}" else None
        if fields is None or len(fields) != 2 or not fields[0].startswith(b"iov_base=") \
                or not fields[1].startswith(b"iov_len="):
            return None
        decoded = _c_bytes(fields[0][len(b"iov_base="):])
        length_text = fields[1][len(b"iov_len="):]
        if decoded is None or not _INTEGER.fullmatch(length_text) or len(decoded) != int(length_text):
            return None
        content.extend(decoded); lengths.append(len(decoded))
    return bytes(content), lengths


def parse_line(line: bytes) -> dict[str, Any]:
    """Return an exact transient fact or a typed, content-free unknown."""
    if not isinstance(line, bytes) or b"\n" in line or b"\r" in line:
        return _unknown(None, "malformed_line")
    match = _LINE.fullmatch(line)
    if match is None:
        name = re.search(rb"\s([a-z][a-z0-9_]*)\(", line)
        call = name.group(1).decode("ascii") if name else None
        return _unknown(call, "unsupported_transfer" if call in _UNSUPPORTED else "malformed_line")
    call = match.group("call").decode("ascii")
    if call not in _CALLS:
        return _unknown(call, "unsupported_transfer")
    if _omitted(match.group("arguments")):
        return _unknown(call, "truncated_or_omitted")
    arguments = _split(match.group("arguments"))
    if arguments is None:
        return _unknown(call, "malformed_arguments")
    vector = call in {"writev", "pwritev", "pwritev2"}
    expected = 3 if call in {"write", "writev"} else 4 if call in {"pwrite64", "pwritev"} else 5
    if len(arguments) != expected:
        return _unknown(call, "malformed_arguments")
    descriptor = _descriptor(arguments[0])
    if descriptor is None:
        return _unknown(call, "unresolved_descriptor")
    if vector:
        if not _INTEGER.fullmatch(arguments[2]):
            return _unknown(call, "malformed_iovec")
        combined = _iovecs(arguments[1], int(arguments[2]))
        if combined is None:
            return _unknown(call, "malformed_or_omitted_iovec")
        content, lengths = combined
        requested = sum(lengths)
    else:
        content = _c_bytes(arguments[1]); lengths = None
        if content is None or not _INTEGER.fullmatch(arguments[2]):
            return _unknown(call, "malformed_or_truncated_content")
        requested = int(arguments[2])
        if len(content) != requested:
            return _unknown(call, "incomplete_content")
    if not content:
        return _unknown(call, "empty_write")
    offset = None
    if call.startswith("pwrite"):
        offset_argument = arguments[3]
        if not _OFFSET.fullmatch(offset_argument):
            return _unknown(call, "malformed_offset")
        offset = int(offset_argument)
        if call == "pwritev2" and not arguments[4]:
            return _unknown(call, "malformed_flags")
    result = int(match.group("result")); errno = match.group("errno")
    if result < 0:
        if result != -1 or errno is None:
            return _unknown(call, "malformed_result")
        status, actual = "error", None
    else:
        if errno is not None or result > requested:
            return _unknown(call, "malformed_result")
        status, actual = "returned", result
    fd, path = descriptor
    return {
        "schema": "sage-os-write-fact-v1", "known": True, "pid": int(match.group("pid")),
        "timestamp": match.group("timestamp").decode("ascii"), "call": call, "fd": fd,
        "fd_path": path, "attempted_content": content, "attempted_bytes": len(content),
        "requested_bytes": requested, "iov_lengths": lengths, "offset": offset,
        "syscall_status": status, "actual_written_bytes": actual,
        "errno": errno.decode("ascii") if errno else None,
    }


def metadata_projection(fact: Mapping[str, Any]) -> dict[str, Any]:
    """Remove the exact path/content while preserving measurable mechanics."""
    if fact.get("known") is not True or not isinstance(fact.get("fd_path"), bytes) \
            or not isinstance(fact.get("attempted_content"), bytes):
        raise ValueError("write fact is not complete")
    content, path = fact["attempted_content"], fact["fd_path"]
    return {
        "schema": "sage-os-write-metadata-v1", "known": True,
        "pid": fact["pid"], "timestamp": fact["timestamp"], "call": fact["call"],
        "fd_handle": f"fd-{fact['fd']}", "path_handle": hashlib.sha256(path).hexdigest(),
        "content_sha256": hashlib.sha256(content).hexdigest(), "attempted_bytes": len(content),
        "requested_bytes": fact["requested_bytes"], "iov_lengths": fact["iov_lengths"],
        "offset": fact["offset"], "syscall_status": fact["syscall_status"],
        "actual_written_bytes": fact["actual_written_bytes"], "errno": fact["errno"],
    }


def _mutation_path(value:bytes,dirfd:bytes|None,*,cwd:str|None)->str|None:
    path=_c_bytes(value)
    if path is not None and not path.startswith(b"/"):
        if dirfd is None or dirfd==b"AT_FDCWD":base=os.fsencode(cwd) if cwd else None
        else:
            base_match=_DIRFD.fullmatch(dirfd);base=base_match.group(1) if base_match else None
        if base is None:return None
        path=base.rstrip(b"/")+b"/"+path
    if path is None or not path.startswith(b"/") or b"\\" in path \
            or any(x in {b"",b".",b".."} for x in path.split(b"/")[1:]):
        return None
    try:return posixpath.normpath(path.decode("ascii"))
    except UnicodeError:return None


def parse_mutation_line(line:bytes,*,cwd:str|None=None)->dict[str,Any]:
    """Resolve demonstrated open/mkdir/unlink namespaces; failures have no mutation."""
    if not isinstance(line,bytes) or b"\n" in line or b"\r" in line:return _unknown(None,"malformed_line")
    match=_MUTATION_LINE.fullmatch(line)
    if match is None:return _unknown(None,"malformed_line")
    call=match.group("call").decode("ascii");result=int(match.group("result"));errno=match.group("errno")
    if call not in _MUTATION_CALLS:return _unknown(call,"unsupported_mutation")
    arguments=_split(match.group("arguments"))
    if arguments is None:return _unknown(call,"malformed_arguments")
    if call in {"rename","renameat","renameat2"}:
        expected={"rename":2,"renameat":4,"renameat2":5}[call]
        if len(arguments)!=expected:return _unknown(call,"malformed_arguments")
        if call=="rename":src=_mutation_path(arguments[0],None,cwd=cwd);dst=_mutation_path(arguments[1],None,cwd=cwd)
        else:
            src=_mutation_path(arguments[1],arguments[0],cwd=cwd)
            dst=_mutation_path(arguments[3],arguments[2],cwd=cwd)
        if src is None or dst is None:return _unknown(call,"mutation_path_unresolved")
        if call=="renameat2" and arguments[4]!=b"0":return _unknown(call,"unsafe_rename_flags")
        if result<0:
            return {"schema":"sage-os-file-mutation-v1","known":result==-1 and errno is not None,"call":call,
                    "pid":int(match.group("pid")),"timestamp":match.group("timestamp").decode(),
                    "mutated":False,"errno":errno.decode() if errno else None,
                    "source_path":src,"path":dst,"atomic_move":True}
        if result!=0 or errno is not None:return _unknown(call,"malformed_result")
        return {"schema":"sage-os-file-mutation-v1","known":True,"call":call,
                "pid":int(match.group("pid")),"timestamp":match.group("timestamp").decode(),
                "mutated":True,"path":dst,"source_path":src,"atomic_move":True,
                "errno":None,"remove_directory":False}
    if result<0:
        return {"schema":"sage-os-file-mutation-v1","known":result==-1 and errno is not None,"call":call,
                "pid":int(match.group("pid")),"timestamp":match.group("timestamp").decode(),
                "mutated":False,"errno":errno.decode() if errno else None}
    positions={"open":(0,None),"openat":(1,0),"mkdir":(0,None),"mkdirat":(1,0),
               "unlink":(0,None),"unlinkat":(1,0)}
    path_index,dir_index=positions[call]
    if path_index>=len(arguments):return _unknown(call,"malformed_arguments")
    path=_c_bytes(arguments[path_index])
    if match.group("resolved") is not None and call in {"open","openat"}:
        path=match.group("resolved")
    elif path is not None and not path.startswith(b"/"):
        if dir_index is None or arguments[dir_index]==b"AT_FDCWD":base=os.fsencode(cwd) if cwd else None
        else:
            base_match=_DIRFD.fullmatch(arguments[dir_index]);base=base_match.group(1) if base_match else None
        if base is None:return _unknown(call,"relative_namespace_unresolved")
        path=base.rstrip(b"/")+b"/"+path
    if path is None or not path.startswith(b"/") or b"\\" in path or any(x in {b"",b".",b".."} for x in path.split(b"/")[1:]):
        return _unknown(call,"mutation_path_unresolved")
    try:normalized=posixpath.normpath(path.decode("ascii"))
    except UnicodeError:return _unknown(call,"mutation_path_unresolved")
    remove_directory=(call=="unlink" or call=="unlinkat") and any(arg==b"AT_REMOVEDIR" for arg in arguments)
    return {"schema":"sage-os-file-mutation-v1","known":True,"call":call,
            "pid":int(match.group("pid")),"timestamp":match.group("timestamp").decode(),
            "mutated":True,"path":normalized,"errno":None,"remove_directory":remove_directory}


def parse_mmap_line(line:bytes)->dict[str,Any]:
    """Resolve the exact file backing a demonstrated shared writable mapping."""
    match=_MMAP_LINE.fullmatch(line) if isinstance(line,bytes) else None
    if match is None:return _unknown("mmap","unsupported_mmap")
    if b"PROT_WRITE" not in match.group("protection").split(b"|") or b"MAP_SHARED" not in match.group("flags").split(b"|"):
        return _unknown("mmap","unsupported_mmap")
    path=match.group("path")
    if not path.startswith(b"/") or b"\\" in path:return _unknown("mmap","mmap_path_unresolved")
    try:decoded=path.decode("ascii")
    except UnicodeError:return _unknown("mmap","mmap_path_unresolved")
    return {"schema":"sage-os-mmap-v1","known":True,"call":"mmap","pid":int(match.group("pid")),
        "timestamp":match.group("timestamp").decode(),"fd":int(match.group("fd")),"path":decoded,
        "length":int(match.group("length")),"offset":int(match.group("offset")),
        "mutated":match.group("address") is not None,"errno":match.group("errno").decode() if match.group("errno") else None}
