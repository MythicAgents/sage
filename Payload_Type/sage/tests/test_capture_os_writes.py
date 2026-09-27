from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "skills/sage-eval-gauge/scripts/capture_os_writes.py"
SPEC = importlib.util.spec_from_file_location("capture_os_writes", SOURCE)
assert SPEC and SPEC.loader
capture = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(capture)


def _line(call: str, arguments: bytes, result: bytes = b"4") -> bytes:
    return b"123 1770000000.125 " + call.encode() + b"(" + arguments + b") = " + result


def test_scalar_escapes_short_and_failed_attempts_are_separate():
    content = b'a"b\\\0\nA'
    fact = capture.parse_line(_line("write", b'7</tmp/sink>, "a\\"b\\\\\\000\\n\\x41", 7', b"3"))
    assert fact["known"] is True and fact["attempted_content"] == content
    assert fact["attempted_bytes"] == fact["requested_bytes"] == 7
    assert fact["actual_written_bytes"] == 3 and fact["syscall_status"] == "returned"

    failed = capture.parse_line(_line("pwrite64", b'7</tmp/sink>, "DATA", 4, 99', b"-1 EIO (I/O error)"))
    assert failed["known"] is True and failed["attempted_content"] == b"DATA"
    assert failed["offset"] == 99 and failed["actual_written_bytes"] is None
    assert failed["syscall_status"] == "error" and failed["errno"] == "EIO"


def test_full_writev_and_pwritev_arrays():
    vectors = b'[{iov_base="ab", iov_len=2}, {iov_base="c\\000d", iov_len=3}]'
    fact = capture.parse_line(_line("writev", b"8</tmp/out>, " + vectors + b", 2", b"5"))
    assert fact["known"] is True and fact["attempted_content"] == b"abc\0d"
    assert fact["iov_lengths"] == [2, 3] and fact["offset"] is None
    positioned = capture.parse_line(_line("pwritev2", b"8</tmp/out>, " + vectors + b", 2, 14, RWF_DSYNC", b"5"))
    assert positioned["known"] is True and positioned["offset"] == 14


@pytest.mark.parametrize("line,reason", [
    (_line("write", b'7</tmp/sink>, "x"..., 2'), "truncated_or_omitted"),
    (_line("writev", b'7</tmp/sink>, [{iov_base="a", iov_len=1}, ...], 2'), "truncated_or_omitted"),
    (_line("write", b'7, "data", 4'), "unresolved_descriptor"),
    (_line("write", b'7</tmp/sink>, "bad\\q", 4'), "malformed_or_truncated_content"),
    (_line("write", b'7</tmp/sink>, "\\777", 1'), "malformed_or_truncated_content"),
    (_line("write", b'7</tmp/sink>, "a\tb", 3'), "malformed_or_truncated_content"),
    (_line("write", b'7</tmp/sink>, "data", 5'), "incomplete_content"),
    (_line("sendfile", b"7</tmp/sink>, 8</tmp/in>, NULL, 4"), "unsupported_transfer"),
])
def test_unknown_is_typed_and_never_carries_empty_bytes(line, reason):
    fact = capture.parse_line(line)
    assert fact == {"schema": "sage-os-write-fact-v1", "known": False,
                    "call": fact["call"], "reason": reason, "attempted_content": None}


def test_paths_and_payload_lookalikes_do_not_change_descriptor():
    fact = capture.parse_line(_line(
        "write", b'2</tmp/sink-near>, "payload 9</tmp/sink>", 20', b"20"
    ))
    assert fact["known"] is True and fact["fd_path"] == b"/tmp/sink-near"
    projection = capture.metadata_projection(fact)
    assert projection["path_handle"] == hashlib.sha256(b"/tmp/sink-near").hexdigest()
    assert projection["content_sha256"] == hashlib.sha256(b"payload 9</tmp/sink>").hexdigest()
    assert b"/tmp/sink" not in repr(projection).encode() and b"payload" not in repr(projection).encode()
    ellipsis = capture.parse_line(_line("write", b'2</tmp/sink>, "actual...data", 13', b"13"))
    assert ellipsis["known"] is True and ellipsis["attempted_content"] == b"actual...data"


def test_actual_sqlite_pwrite_records_reconcile_exact_lengths(tmp_path):
    strace = shutil.which("strace")
    if not strace:
        pytest.skip("strace unavailable")
    trace, database = tmp_path / "trace", tmp_path / "database"
    subprocess.run([
        strace, "-f", "-qq", "-ttt", "--decode-fds=path", "-s", "1048576",
        "-e", "trace=pwrite64", "-o", str(trace), sys.executable, "-c",
        "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);c.execute('create table t(x)');"
        "c.execute('insert into t values(?)',(bytes(range(128)),));c.commit();c.close()", str(database),
    ], check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
       stderr=subprocess.DEVNULL, timeout=60)
    facts = [capture.parse_line(line) for line in trace.read_bytes().splitlines()]
    writes = [fact for fact in facts if fact["known"] and fact["call"] == "pwrite64"
              and fact["fd_path"] == os.fsencode(str(database))]
    assert writes and all(fact["attempted_bytes"] == fact["requested_bytes"] for fact in writes)
    assert all(fact["actual_written_bytes"] == fact["requested_bytes"] for fact in writes)
    assert any(fact["attempted_content"].startswith(b"SQLite format 3\0") for fact in writes)


def test_actual_writev_line_is_complete(tmp_path):
    strace = shutil.which("strace")
    if not strace:
        pytest.skip("strace unavailable")
    trace = tmp_path / "trace"
    subprocess.run([
        strace, "-f", "-qq", "-ttt", "--decode-fds=path", "-s", "1048576",
        "-e", "trace=writev", "-o", str(trace), sys.executable, "-c",
        "import os;os.writev(os.open('/dev/null',os.O_WRONLY),[b'abc',b'de'])",
    ], check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
       stderr=subprocess.DEVNULL, timeout=60)
    facts = [capture.parse_line(line) for line in trace.read_bytes().splitlines()]
    writes = [fact for fact in facts if fact["known"] and fact["call"] == "writev"]
    assert any(fact["attempted_content"] == b"abcde" and fact["iov_lengths"] == [3, 2]
               and fact["actual_written_bytes"] == 5 for fact in writes)

def test_actual_namespace_mutations_and_failed_eexist_are_distinguished(tmp_path):
    strace=shutil.which("strace")
    if not strace:pytest.skip("strace unavailable")
    trace=tmp_path/"mutation-trace";base=tmp_path/"base";base.mkdir()
    code="import os,sys;d=os.open(sys.argv[1],os.O_RDONLY);os.mkdir('child',dir_fd=d);" \
         "\ntry:os.mkdir('child',dir_fd=d)\nexcept FileExistsError:pass\nos.rmdir('child',dir_fd=d)"
    subprocess.run([strace,"-f","-qq","-ttt","--decode-fds=path","-s","1048576","-e",
     "trace=mkdir,mkdirat,unlink,unlinkat","-o",str(trace),sys.executable,"-c",code,str(base)],
     check=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
    facts=[capture.parse_mutation_line(line,cwd=str(tmp_path)) for line in trace.read_bytes().splitlines()]
    known=[x for x in facts if x["known"]];assert any(x["mutated"] and x["path"]==str(base/"child") for x in known)
    assert any(not x["mutated"] and x["errno"]=="EEXIST" for x in known)

def test_rename_variants_require_two_resolved_paths_and_safe_flags():
    facts=[capture.parse_mutation_line(line,cwd="/tmp/work") for line in (
        _line("rename",b'"/tmp/work/state.tmp", "/tmp/work/state"',b"0"),
        _line("renameat",b'5</tmp/work>, "state.tmp", 5</tmp/work>, "state"',b"0"),
        _line("renameat2",b'AT_FDCWD, "state.tmp", AT_FDCWD, "state", 0',b"0"))]
    assert all(x["known"] and x["atomic_move"] and x["source_path"]=="/tmp/work/state.tmp"
               and x["path"]=="/tmp/work/state" for x in facts)
    for line in (_line("rename",b'"state.tmp", "/tmp/work/state"',b"0"),
                 _line("renameat2",b'AT_FDCWD, "state.tmp", AT_FDCWD, "state", RENAME_EXCHANGE',b"0")):
        assert capture.parse_mutation_line(line)["known"] is False
    for resolved in (b"/tmp/work/../outside",b"/tmp/work//state",b"/tmp/work\\state"):
        line=_line("openat",b'AT_FDCWD, "ignored", O_RDONLY',b"3<"+resolved+b">")
        assert capture.parse_mutation_line(line,cwd="/tmp/work")["known"] is False

def test_shared_writable_mmap_identifies_backing_path_without_mapping_bytes():
    fact=capture.parse_mmap_line(b'42 1.0 mmap(NULL, 32768, PROT_READ|PROT_WRITE, MAP_SHARED, 5</tmp/phoenix.db-shm>, 0) = 0x7f00')
    assert fact["known"] and fact["mutated"] and fact["path"]=="/tmp/phoenix.db-shm"
    failed=capture.parse_mmap_line(b'42 1.0 mmap(NULL, 32768, PROT_READ|PROT_WRITE, MAP_SHARED, 5</tmp/phoenix.db-shm>, 0) = -1 EACCES (Permission denied)')
    assert failed["known"] and failed["mutated"] is False
