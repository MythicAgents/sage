"""The tools/ drop-zone inventory: header identification, family matching, Mythic presence, and its surfaces."""

import asyncio
import hashlib
import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import mythic_tools  # noqa: E402
import prompt_loader  # noqa: E402
import staged_tools as st  # noqa: E402
from mythic_tools import TOOL_SAFETY_READ_ONLY, MythicTools, tool_safety_of  # noqa: E402

ILONLY, BIT32_REQUIRED, BIT32_PREFERRED = 0x1, 0x2, 0x20000


def _pe(machine=0x014C, magic=0x10B, clr_flags=None, payload=b""):
    """A minimal PE image with one section; ``clr_flags`` adds a CLR header (a .NET assembly)."""
    data = bytearray(0x400)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    coff = 0x84
    optional_size = 224 if magic == 0x10B else 240
    struct.pack_into("<HHIIIHH", data, coff, machine, 1, 0, 0, 0, optional_size, 0x0102)
    optional = coff + 20
    struct.pack_into("<H", data, optional, magic)
    rva_count_at, directories_at = (optional + 92, optional + 96) if magic == 0x10B else (optional + 108, optional + 112)
    struct.pack_into("<I", data, rva_count_at, 16)
    struct.pack_into("<8sIIII", data, optional + optional_size, b".text", 0x200, 0x2000, 0x200, 0x200)
    if clr_flags is not None:
        struct.pack_into("<II", data, directories_at + 14 * 8, 0x2008, 72)
        struct.pack_into("<I", data, 0x208, 72)
        struct.pack_into("<I", data, 0x208 + 16, clr_flags)
    return bytes(data) + payload


def _bof(machine=0x8664):
    header = struct.pack("<HHIIIHH", machine, 1, 0, 60, 1, 0, 0)
    return header + struct.pack("<8sIIIIIIHHI", b".text", 0, 0, 0, 0, 0, 0, 0, 0, 0) + b"\x00" * 40


def _elf_x64():
    data = bytearray(64)
    data[0:4] = b"\x7fELF"
    data[5] = 1
    struct.pack_into("<H", data, 18, 0x3E)
    return bytes(data)


@pytest.mark.parametrize("data,filename,kind,arch", [
    (_pe(clr_flags=ILONLY), "a.exe", st.KIND_DOTNET, "anycpu"),
    (_pe(clr_flags=ILONLY | BIT32_REQUIRED), "a.exe", st.KIND_DOTNET, "x86"),
    (_pe(clr_flags=ILONLY | BIT32_REQUIRED | BIT32_PREFERRED), "a.exe", st.KIND_DOTNET, "anycpu"),
    (_pe(machine=0x8664, magic=0x20B, clr_flags=ILONLY), "a.exe", st.KIND_DOTNET, "x64"),
    (_pe(machine=0x8664, magic=0x20B), "a.exe", st.KIND_NATIVE, "x64"),
    (_pe(), "a.dll", st.KIND_NATIVE, "x86"),
    (_bof(), "a.o", st.KIND_BOF, "x64"),
    (_bof(machine=0x014C), "a.x86.o", st.KIND_BOF, "x86"),
    (_elf_x64(), "tool", st.KIND_ELF, "x64"),
    (b"PK\x03\x04rest", "SharpHound.zip", st.KIND_ARCHIVE, ""),
    (b"Write-Host hi", "Invoke-Thing.ps1", st.KIND_POWERSHELL, ""),
    (b"#!/usr/bin/env python3\nprint(1)", "tool", st.KIND_PYTHON, ""),
    (b"#!/bin/sh\necho hi", "tool", st.KIND_SCRIPT, ""),
    # Named .exe but not a PE: the extension alone never makes it executable.
    (b"not really an executable", "fake.exe", st.KIND_UNKNOWN, ""),
    (b"MZ" + b"\x00" * 10, "truncated.exe", st.KIND_UNKNOWN, ""),
    (b"MZ" + b"\x00" * 0x3A + struct.pack("<I", 0x7FFFFFFF), "bad-offset.exe", st.KIND_UNKNOWN, ""),
])
def test_identify_reads_kind_and_arch_from_the_header(data, filename, kind, arch):
    assert st.identify(data, filename) == {"kind": kind, "arch": arch}


@pytest.mark.parametrize("filename,family", [
    ("SharpHound.exe", "sharphound"),
    ("new_sharphound_x64.exe", "sharphound"),
    ("SharpHound_x32.exe", "sharphound"),
    ("SharpHound_x64.exe", "sharphound"),
    ("SharpHound.o", "sharphound"),
    ("SharpHound_v2.13.0_windows_x86.zip", "sharphound"),
    ("Rubeus-custom-obf.exe", "rubeus"),
    ("nanodump.x64.o", "nanodump"),
    ("Koh.exe", "koh"),
    # Near misses keep their own family.
    ("SharpHoundData.zip", "sharphounddata"),
    ("Sharp.exe", "sharp"),
    ("SharpView.exe", "sharpview"),
    # Nothing left after stripping: fall back to the plain stem.
    ("x64.exe", "x64"),
])
def test_family_key_groups_renamed_and_per_architecture_builds(filename, family):
    assert st.family_key(filename) == family


def _ttps():
    return [
        ("sharphound", {"name": "SharpHound", "binary_filename": "SharpHound.exe", "binary_type": ".net-assembly"}, ""),
        ("koh", {"name": "Koh", "binary_filename": "Koh.exe / Koh.x64.o", "binary_type": "multi"}, ""),
        ("bofs", {"name": "Misc BOFs", "binary_filename": "(per-BOF .x64.o files)", "binary_type": "bof"}, ""),
        ("no-binary", {"name": "Concept", "binary_filename": "", "binary_type": "none"}, ""),
    ]


def test_ttp_index_splits_multi_file_values_and_skips_descriptions():
    rows = st.ttp_binary_index(_ttps())
    assert [(r["ttp"], r["filename"], r["family"]) for r in rows] == [
        ("sharphound", "SharpHound.exe", "sharphound"),
        ("koh", "Koh.exe", "koh"),
        ("koh", "Koh.x64.o", "koh"),
    ]


@pytest.fixture
def drop_zone(tmp_path):
    files = {
        "SharpHound.exe": _pe(clr_flags=ILONLY),
        "new_sharphound_x64.exe": _pe(machine=0x8664, magic=0x20B, clr_flags=ILONLY, payload=b"x64"),
        "SharpHound_x32.exe": _pe(clr_flags=ILONLY | BIT32_REQUIRED, payload=b"x86"),
        "SharpHound.o": _bof(),
        "SharpHoundCE.exe": _pe(clr_flags=ILONLY, payload=b"ce"),
        "SharpHoundData.zip": b"PK\x03\x04zip",
        "Mystery.exe": _pe(clr_flags=ILONLY, payload=b"mystery"),
        ".keep": b"",
    }
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "Hidden.exe").write_bytes(_pe(clr_flags=ILONLY))
    (tmp_path / "linked.exe").symlink_to(tmp_path / "SharpHound.exe")
    return tmp_path


def _by_name(inventory):
    return {entry["filename"]: entry for entry in inventory["files"]}


def test_scan_lists_top_level_plain_files_with_hashes_and_ttp_matches(drop_zone):
    inventory = st.scan(drop_zone, _ttps())
    files = _by_name(inventory)

    assert inventory["tools_dir_present"] is True
    assert set(files) == {
        "Mystery.exe", "SharpHound.exe", "SharpHound.o", "SharpHoundCE.exe", "SharpHoundData.zip",
        "SharpHound_x32.exe", "new_sharphound_x64.exe",
    }
    exact = files["SharpHound.exe"]
    assert exact["sha256"] == hashlib.sha256((drop_zone / "SharpHound.exe").read_bytes()).hexdigest()
    assert exact["ttp_matches"] == [
        {"ttp": "sharphound", "name": "SharpHound", "match": "exact", "binary_type": ".net-assembly"}
    ]
    assert (files["new_sharphound_x64.exe"]["kind"], files["new_sharphound_x64.exe"]["arch"]) == (st.KIND_DOTNET, "x64")
    assert files["new_sharphound_x64.exe"]["ttp_matches"][0]["match"] == "family"
    assert files["SharpHound_x32.exe"]["arch"] == "x86"
    assert (files["SharpHound.o"]["kind"], files["SharpHound.o"]["ttp_matches"][0]["match"]) == (st.KIND_BOF, "family")
    contained = files["SharpHoundCE.exe"]
    assert contained["ttp_matches"][0]["match"] == "name_contains"
    assert contained["family"] == "sharphound"
    assert "hint" not in contained


def test_scan_gives_unmatched_files_a_hint_and_never_fuzzy_matches_archives(drop_zone):
    files = _by_name(st.scan(drop_zone, _ttps()))

    assert files["Mystery.exe"]["ttp_matches"] == []
    assert "Ask the operator" in files["Mystery.exe"]["hint"]
    assert ".net-assembly, anycpu" in files["Mystery.exe"]["hint"]
    # The zip's name contains "sharphound", but an archive is not a runnable build of the tool.
    assert files["SharpHoundData.zip"]["ttp_matches"] == []
    assert files["SharpHoundData.zip"]["kind"] == st.KIND_ARCHIVE


def test_scan_reports_a_missing_folder_and_caches_hashes(tmp_path, monkeypatch):
    assert st.scan(tmp_path / "absent", _ttps()) == {"tools_dir_present": False, "files": [], "truncated": False}

    (tmp_path / "SharpHound.exe").write_bytes(_pe(clr_flags=ILONLY, payload=b"cache"))
    st.scan(tmp_path, _ttps())
    reads = []
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self.name) or original(self))
    st.scan(tmp_path, _ttps())
    assert reads == []


def test_scan_caps_the_ttp_list_per_file(tmp_path):
    (tmp_path / "Rubeus.exe").write_bytes(_pe(clr_flags=ILONLY, payload=b"rubeus"))
    ttps = [(f"rubeus-{i}", {"name": f"R{i}", "binary_filename": "Rubeus.exe"}, "") for i in range(8)]
    entry = st.scan(tmp_path, ttps)["files"][0]
    assert len(entry["ttp_matches"]) == 5
    assert entry["ttp_match_count"] == 8


def _row(name, content, uuid, *, row_id=1, deleted=False):
    return {
        "id": row_id, "agent_file_id": uuid, "filename_utf8": name, "deleted": deleted, "complete": True,
        "md5": hashlib.md5(content).hexdigest(), "sha1": hashlib.sha1(content).hexdigest(),
    }


def test_annotate_mythic_distinguishes_every_presence_state(drop_zone):
    inventory = st.scan(drop_zone, _ttps())
    read = lambda name: (drop_zone / name).read_bytes()  # noqa: E731
    rows = [
        _row("SharpHound.exe", read("SharpHound.exe"), "old-uuid", row_id=1),
        _row("SharpHound.exe", read("SharpHound.exe"), "sharphound-uuid", row_id=2),
        _row("renamed.exe", read("new_sharphound_x64.exe"), "x64-uuid", row_id=3),
        _row("SharpHound_x32.exe", b"older build", "stale-uuid", row_id=4),
        _row("Mystery.exe", read("Mystery.exe"), "deleted-uuid", row_id=5, deleted=True),
    ]
    st.annotate_mythic(inventory["files"], rows)
    files = _by_name(inventory)

    assert (files["SharpHound.exe"]["in_mythic"], files["SharpHound.exe"]["file_uuid"]) == ("yes", "sharphound-uuid")
    assert files["new_sharphound_x64.exe"]["in_mythic"] == "same_bytes_other_name"
    assert files["new_sharphound_x64.exe"]["file_uuid"] == "x64-uuid"
    assert files["new_sharphound_x64.exe"]["mythic_filename"] == "renamed.exe"
    assert files["SharpHound_x32.exe"]["in_mythic"] == "different_bytes"
    assert "file_uuid" not in files["SharpHound_x32.exe"]
    assert files["Mystery.exe"]["in_mythic"] == "no"

    st.annotate_mythic(inventory["files"], None)
    assert {entry["in_mythic"] for entry in inventory["files"]} == {"unknown"}


def test_lookup_finds_every_variant_and_mythic_only_copies(drop_zone):
    inventory = st.scan(drop_zone, _ttps())
    rows = [_row("SharpHound_arm.exe", b"mythic only", "mythic-only-uuid")]
    found = st.lookup(inventory, "SharpHound.exe", rows)

    assert {entry["filename"] for entry in found["staged"]} == {
        "SharpHound.exe", "new_sharphound_x64.exe", "SharpHound_x32.exe", "SharpHound.o", "SharpHoundCE.exe",
    }
    assert found["mythic_only"] == [{"mythic_filename": "SharpHound_arm.exe", "file_uuid": "mythic-only-uuid"}]
    assert "Several variants" in found["note"]
    assert "operator_message" not in found


def test_lookup_asks_the_operator_when_a_tool_is_staged_nowhere(drop_zone):
    inventory = st.scan(drop_zone, _ttps())

    found = st.lookup(inventory, "nanodump.x64.o", [])
    assert found["staged"] == [] and found["mythic_only"] == []
    assert found["operator_message"].startswith("I need nanodump.x64.o (any variant) and it isn't in tools/ or Mythic")
    assert "tell me to continue" in found["operator_message"]

    unchecked = st.lookup(inventory, "nanodump.x64.o", None)
    assert "Mythic was not checked" in unchecked["operator_message"]

    # A near-miss name does not count as the tool.
    assert st.lookup(inventory, "Sharp.exe", [])["staged"] == []


def _tools(monkeypatch, tools_dir, ttps, rows):
    mt = MythicTools(agent_task_id="test")
    mt.client = object()
    monkeypatch.setattr(mythic_tools.ttp_library, "TOOLS_DIR", tools_dir)
    monkeypatch.setattr(mythic_tools.ttp_library, "iter_ttps", lambda: iter(ttps))

    async def fake_rows():
        return rows

    monkeypatch.setattr(mt, "_uploaded_file_rows", fake_rows)
    return mt


def test_list_staged_tools_is_a_read_only_operator_tool():
    assert tool_safety_of("list_staged_tools", MythicTools) == TOOL_SAFETY_READ_ONLY
    assert "list_staged_tools" not in mythic_tools.GUARDED_TOOLS
    assert "list_staged_tools" not in mythic_tools.SCOPE_REQUIREMENTS
    assert "list_staged_tools" in prompt_loader.get_prompt_tools("mythic_operator")


def test_list_staged_tools_returns_the_inventory_with_mythic_presence(monkeypatch, drop_zone):
    rows = [_row("SharpHound.exe", (drop_zone / "SharpHound.exe").read_bytes(), "sharphound-uuid")]
    mt = _tools(monkeypatch, drop_zone, _ttps(), rows)

    result = json.loads(asyncio.run(mt.list_staged_tools()))

    assert result["mythic_checked"] is True
    assert result["count"] == 7
    files = {entry["filename"]: entry for entry in result["files"]}
    assert (files["SharpHound.exe"]["in_mythic"], files["SharpHound.exe"]["file_uuid"]) == ("yes", "sharphound-uuid")
    assert files["Mystery.exe"]["in_mythic"] == "no"
    assert not any(key.startswith("_") for entry in result["files"] for key in entry)


def test_list_staged_tools_lookup_and_unreadable_mythic(monkeypatch, drop_zone):
    mt = _tools(monkeypatch, drop_zone, _ttps(), None)

    result = json.loads(asyncio.run(mt.list_staged_tools("SharpHound.exe")))
    assert result["mythic_checked"] is False
    assert {entry["in_mythic"] for entry in result["staged"]} == {"unknown"}
    assert len(result["staged"]) == 5

    missing = json.loads(asyncio.run(mt.list_staged_tools("Whisker.exe")))
    assert "Whisker.exe" in missing["operator_message"]


def test_uploaded_file_rows_is_none_when_mythic_fails(monkeypatch):
    mt = MythicTools(agent_task_id="test")
    assert asyncio.run(mt._uploaded_file_rows()) is None

    mt.client = object()

    async def broken(**_kwargs):
        raise RuntimeError("missing_scope")
        yield  # pragma: no cover

    monkeypatch.setattr(mythic_tools.mythic, "get_all_uploaded_files", broken)
    assert asyncio.run(mt._uploaded_file_rows()) is None


def test_ensure_tool_uploaded_missing_points_at_staged_variants(monkeypatch, drop_zone):
    mt = _tools(monkeypatch, drop_zone, _ttps(), [])
    monkeypatch.setattr(mt, "_require_request_contract_effect", lambda *_a, **_k: None)

    async def not_in_mythic(_filename):
        return None

    monkeypatch.setattr(mt, "_find_uploaded_file_by_name", not_in_mythic)
    (drop_zone / "SharpHound.exe").unlink()

    result = json.loads(asyncio.run(mt.ensure_tool_uploaded("SharpHound.exe")))
    assert result["status"] == "missing"
    assert {v["filename"] for v in result["staged_variants"]} == {
        "new_sharphound_x64.exe", "SharpHound_x32.exe", "SharpHound.o", "SharpHoundCE.exe",
    }
    assert "Staged variants of this tool exist" in result["note"]

    nothing = json.loads(asyncio.run(mt.ensure_tool_uploaded("Whisker.exe")))
    assert nothing["status"] == "missing" and "staged_variants" not in nothing
