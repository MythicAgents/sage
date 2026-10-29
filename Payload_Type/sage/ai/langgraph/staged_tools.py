"""Read-only inventory of the operator's ``tools/`` drop zone.

Lists what the operator staged so planning can use it: each file's hashes, its type and architecture read
from the file header (never from the extension alone, and never by running it), the TTPs that document it,
and whether Mythic's file store already holds the same bytes. A staged file grants no authority; uploading
and tasking stay on the guarded path.

Pure functions apart from reading files; the Mythic file rows are passed in by the caller. Never raises
from the public helpers.
"""

from __future__ import annotations

import hashlib
import re
import struct
from pathlib import Path
from typing import Any, Iterable

KIND_DOTNET = ".net-assembly"
KIND_NATIVE = "native-exe"
KIND_BOF = "bof"
KIND_POWERSHELL = "powershell-script"
KIND_PYTHON = "python-script"
KIND_SCRIPT = "script"
KIND_ELF = "elf"
KIND_ARCHIVE = "archive"
KIND_UNKNOWN = "unknown"

# Kinds a TTP can be run as. An archive or an unrecognised blob only matches a TTP by exact filename.
RUNNABLE_KINDS = frozenset({
    KIND_DOTNET, KIND_NATIVE, KIND_BOF, KIND_POWERSHELL, KIND_PYTHON, KIND_SCRIPT, KIND_ELF,
})

MAX_FILES = 500
MAX_HASH_BYTES = 256 * 1024 * 1024
_MAX_TTP_MATCHES = 5
# A TTP family must be at least this long to match inside a longer staged name ("sharphoundce").
_MIN_CONTAINED_FAMILY = 5

_PE_MACHINES = {0x014C: "x86", 0x8664: "x64", 0xAA64: "arm64", 0x01C4: "arm"}
_ELF_MACHINES = {0x03: "x86", 0x3E: "x64", 0xB7: "arm64", 0x28: "arm"}
_ARCHIVE_MAGIC = (b"PK\x03\x04", b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07")
_SCRIPT_EXTENSIONS = {
    ".ps1": KIND_POWERSHELL, ".psm1": KIND_POWERSHELL, ".py": KIND_PYTHON,
    ".sh": KIND_SCRIPT, ".bat": KIND_SCRIPT, ".cmd": KIND_SCRIPT, ".vbs": KIND_SCRIPT, ".js": KIND_SCRIPT,
}
_STRIPPED_EXTENSIONS = {
    "exe", "dll", "o", "obj", "bof", "ps1", "psm1", "py", "sh", "bat", "cmd", "vbs", "js", "zip", "bin", "elf",
}
_ARCH_TOKENS = {
    "x64", "x86", "x32", "amd64", "arm64", "arm", "win32", "win64", "32", "64", "32bit", "64bit", "i386", "i686",
}
_FILLER_TOKENS = {
    "new", "custom", "latest", "release", "debug", "final", "mod", "modified", "obf", "obfuscated", "copy",
    "packed", "signed", "patched", "compiled", "bin", "windows", "win", "linux",
}
_VERSION_RE = re.compile(r"^v?\d+[a-z]?$")
_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")

_HASH_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}


def family_key(filename: str) -> str:
    """Normalise a tool filename to its family: ``new_SharpHound_x64.exe`` and ``SharpHound.o`` -> ``sharphound``.

    Drops extensions, architecture and version parts, and filler words operators add to renamed builds.
    Falls back to the plain stem when nothing else is left.
    """
    name = Path(str(filename or "")).name.casefold()
    stem = name
    while True:
        base, dot, ext = stem.rpartition(".")
        if dot and base and (ext in _STRIPPED_EXTENSIONS or ext in _ARCH_TOKENS):
            stem = base
            continue
        break
    tokens = [t for t in re.split(r"[^a-z0-9]+", stem) if t]
    kept = [t for t in tokens if t not in _ARCH_TOKENS and t not in _FILLER_TOKENS and not _VERSION_RE.match(t)]
    return "".join(kept) or "".join(tokens)


def identify(data: bytes, filename: str = "") -> dict[str, str]:
    """Return ``{"kind", "arch"}`` from the file header; ``arch`` is ``""`` when it does not apply."""
    try:
        pe = _identify_pe(data)
        if pe is not None:
            return pe
        if data[:4] == b"\x7fELF" and len(data) >= 20:
            little = data[5:6] != b"\x02"
            machine = struct.unpack_from("<H" if little else ">H", data, 18)[0]
            return {"kind": KIND_ELF, "arch": _ELF_MACHINES.get(machine, "unknown")}
        if any(data.startswith(magic) for magic in _ARCHIVE_MAGIC):
            return {"kind": KIND_ARCHIVE, "arch": ""}
        coff = _identify_coff(data)
        if coff is not None:
            return coff
        suffix = Path(str(filename or "")).suffix.casefold()
        if suffix in _SCRIPT_EXTENSIONS:
            return {"kind": _SCRIPT_EXTENSIONS[suffix], "arch": ""}
        if data.startswith(b"#!"):
            first_line = data.split(b"\n", 1)[0]
            return {"kind": KIND_PYTHON if b"python" in first_line else KIND_SCRIPT, "arch": ""}
    except (struct.error, IndexError, ValueError):
        pass
    return {"kind": KIND_UNKNOWN, "arch": ""}


def _identify_pe(data: bytes) -> dict[str, str] | None:
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 24 > len(data) or data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        return None
    coff = e_lfanew + 4
    machine, section_count = struct.unpack_from("<HH", data, coff)
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]
    optional = coff + 20
    arch = _PE_MACHINES.get(machine, "unknown")
    magic = struct.unpack_from("<H", data, optional)[0] if optional + 2 <= len(data) else 0
    if magic == 0x10B:
        rva_count_at, directories_at = optional + 92, optional + 96
    elif magic == 0x20B:
        rva_count_at, directories_at = optional + 108, optional + 112
    else:
        return {"kind": KIND_NATIVE, "arch": arch}
    clr_rva = 0
    if rva_count_at + 4 <= len(data) and struct.unpack_from("<I", data, rva_count_at)[0] > 14:
        if directories_at + 15 * 8 <= len(data):
            clr_rva = struct.unpack_from("<I", data, directories_at + 14 * 8)[0]
    if not clr_rva:
        return {"kind": KIND_NATIVE, "arch": arch}
    if magic == 0x10B and machine == 0x014C:
        flags = _clr_flags(data, optional + optional_size, section_count, clr_rva)
        if flags is not None and flags & 0x1 and (not flags & 0x2 or flags & 0x20000):
            arch = "anycpu"
    return {"kind": KIND_DOTNET, "arch": arch}


def _clr_flags(data: bytes, sections_at: int, section_count: int, clr_rva: int) -> int | None:
    """Read the CLR header flags (ILONLY, 32BITREQUIRED, 32BITPREFERRED) by mapping the RVA to a file offset."""
    for index in range(min(section_count, 96)):
        entry = sections_at + index * 40
        if entry + 40 > len(data):
            return None
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, entry + 8)
        if virtual_address <= clr_rva < virtual_address + max(virtual_size, raw_size):
            offset = raw_pointer + clr_rva - virtual_address
            if offset + 20 > len(data):
                return None
            return struct.unpack_from("<I", data, offset + 16)[0]
    return None


def _identify_coff(data: bytes) -> dict[str, str] | None:
    """A Beacon Object File is a bare COFF object: a known machine, no optional header, sane section table."""
    if len(data) < 20:
        return None
    machine, section_count, _, symbol_table, _, optional_size, _ = struct.unpack_from("<HHIIIHH", data, 0)
    if machine not in _PE_MACHINES or optional_size != 0 or not 0 < section_count <= 96:
        return None
    if 20 + section_count * 40 > len(data) or not 0 < symbol_table < len(data):
        return None
    return {"kind": KIND_BOF, "arch": _PE_MACHINES[machine]}


def ttp_binary_index(ttps: Iterable[tuple[str, dict[str, Any], Any]]) -> list[dict[str, Any]]:
    """Flatten TTP ``binary_filename`` values into ``{ttp, name, filename, family, binary_type}`` rows.

    A value may name several files (``Koh.exe / Koh.x64.o``). Descriptive values such as
    ``(per-BOF .x64.o files)`` name no file and are skipped.
    """
    rows: list[dict[str, Any]] = []
    for slug, frontmatter, _body in ttps:
        value = str((frontmatter or {}).get("binary_filename") or "").strip()
        if not value or value.startswith("("):
            continue
        for filename in _FILENAME_RE.findall(value):
            if not any(ch.isalpha() for ch in filename):
                continue
            family = family_key(filename)
            if not family:
                continue
            rows.append({
                "ttp": slug,
                "name": str(frontmatter.get("name") or slug),
                "filename": filename,
                "family": family,
                "binary_type": str(frontmatter.get("binary_type") or ""),
            })
    return rows


def _ttp_matches(filename: str, family: str, kind: str, index: list[dict[str, Any]]) -> tuple[list[dict], str]:
    """Return the TTPs documenting a staged file, strongest match first, and the family to group it under."""
    lowered = filename.casefold()
    exact = [row for row in index if row["filename"].casefold() == lowered]
    same_family = [row for row in index if row["family"] == family and row not in exact]
    matches = [(row, "exact") for row in exact] + [(row, "family") for row in same_family]
    resolved_family = family
    if not matches and kind in RUNNABLE_KINDS:
        contained = [row for row in index if len(row["family"]) >= _MIN_CONTAINED_FAMILY and row["family"] in family]
        if contained:
            longest = max(len(row["family"]) for row in contained)
            contained = [row for row in contained if len(row["family"]) == longest]
            matches = [(row, "name_contains") for row in contained]
            resolved_family = contained[0]["family"]
    if exact:
        resolved_family = exact[0]["family"]
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row, how in matches:
        if row["ttp"] in seen:
            continue
        seen.add(row["ttp"])
        out.append({"ttp": row["ttp"], "name": row["name"], "match": how, "binary_type": row["binary_type"]})
    return out, resolved_family


def _hashes(path: Path, size: int, mtime_ns: int) -> dict[str, Any]:
    key = (str(path), size, mtime_ns)
    cached = _HASH_CACHE.get(key)
    if cached is not None:
        return cached
    if size > MAX_HASH_BYTES:
        with path.open("rb") as handle:
            header = handle.read(64 * 1024)
        result = {"sha256": None, "md5": None, "sha1": None, "too_large": True, **identify(header, path.name)}
    else:
        data = path.read_bytes()
        result = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "md5": hashlib.md5(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(),
            **identify(data, path.name),
        }
    _HASH_CACHE[key] = result
    return result


def scan(tools_dir: Path, ttps: Iterable[tuple[str, dict[str, Any], Any]]) -> dict[str, Any]:
    """Inventory the top level of ``tools_dir``: plain files only, skipping dotfiles and symlinks."""
    index = ttp_binary_index(ttps)
    if not tools_dir.is_dir():
        return {"tools_dir_present": False, "files": [], "truncated": False}
    entries: list[dict[str, Any]] = []
    truncated = False
    for path in sorted(tools_dir.iterdir(), key=lambda p: p.name.casefold()):
        if path.name.startswith(".") or path.is_symlink() or not path.is_file():
            continue
        if len(entries) >= MAX_FILES:
            truncated = True
            break
        try:
            stat = path.stat()
            facts = _hashes(path, stat.st_size, stat.st_mtime_ns)
        except OSError as exc:
            entries.append({"filename": path.name, "error": str(exc)})
            continue
        family = family_key(path.name)
        matches, family = _ttp_matches(path.name, family, facts["kind"], index)
        entry: dict[str, Any] = {
            "filename": path.name,
            "size": stat.st_size,
            "sha256": facts["sha256"],
            "kind": facts["kind"],
            "arch": facts["arch"],
            "family": family,
            "ttp_matches": matches[:_MAX_TTP_MATCHES],
            "_md5": facts["md5"],
            "_sha1": facts["sha1"],
        }
        if len(matches) > _MAX_TTP_MATCHES:
            entry["ttp_match_count"] = len(matches)
        if facts.get("too_large"):
            entry["note"] = f"Larger than {MAX_HASH_BYTES // (1024 * 1024)} MiB; not hashed."
        if not matches:
            arch = f", {facts['arch']}" if facts["arch"] else ""
            entry["hint"] = (
                f"No TTP documents this file ({facts['kind']}{arch}). Ask the operator what it is for and how "
                "to run it before using it."
            )
        entries.append(entry)
    return {"tools_dir_present": True, "files": entries, "truncated": truncated}


def annotate_mythic(entries: list[dict[str, Any]], mythic_rows: list[dict[str, Any]] | None) -> None:
    """Set ``in_mythic`` on each entry from Mythic's uploaded-file rows (``None`` means not checked).

    - ``yes``: Mythic holds this filename with the same bytes; ``file_uuid`` is ready to task with.
    - ``same_bytes_other_name``: the bytes are in Mythic under another name; pass ``file_uuid``.
    - ``different_bytes``: Mythic has this filename with other bytes; uploading the staged copy replaces it.
    - ``no`` / ``unknown``.
    """
    rows = [
        row for row in (mythic_rows or [])
        if isinstance(row, dict) and not row.get("deleted") and row.get("complete", True) is not False
    ]
    for entry in entries:
        if "error" in entry:
            continue
        if mythic_rows is None:
            entry["in_mythic"] = "unknown"
            continue
        md5, sha1 = entry.get("_md5"), entry.get("_sha1")
        same_bytes = [r for r in rows if md5 and sha1 and r.get("md5") == md5 and r.get("sha1") == sha1]
        named = [r for r in rows if str(r.get("filename_utf8") or "").casefold() == entry["filename"].casefold()]
        named_same = [r for r in named if r in same_bytes]
        if named_same:
            entry["in_mythic"] = "yes"
            entry["file_uuid"] = _latest(named_same).get("agent_file_id")
        elif same_bytes:
            row = _latest(same_bytes)
            entry["in_mythic"] = "same_bytes_other_name"
            entry["file_uuid"] = row.get("agent_file_id")
            entry["mythic_filename"] = row.get("filename_utf8")
        elif named:
            entry["in_mythic"] = "different_bytes"
        else:
            entry["in_mythic"] = "no"


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=lambda r: int(r.get("id") or 0))


def lookup(
    inventory: dict[str, Any],
    binary_filename: str,
    mythic_rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Find every staged or Mythic-held variant of ``binary_filename`` and say what to do when none exists."""
    query = str(binary_filename or "").strip()
    family = family_key(query)
    lowered = query.casefold()
    staged = [
        entry for entry in inventory.get("files", [])
        if entry.get("filename", "").casefold() == lowered
        or (family and (entry.get("family") == family or family_key(entry.get("filename", "")) == family))
    ]
    staged_names = {entry["filename"].casefold() for entry in staged}
    mythic_only: list[dict[str, Any]] = []
    for row in mythic_rows or []:
        if not isinstance(row, dict) or row.get("deleted") or row.get("complete", True) is False:
            continue
        name = str(row.get("filename_utf8") or "")
        if name.casefold() in staged_names:
            continue
        if name.casefold() == lowered or (family and family_key(name) == family):
            mythic_only.append({"mythic_filename": name, "file_uuid": row.get("agent_file_id")})
    result: dict[str, Any] = {
        "query": query,
        "family": family,
        "staged": staged,
        "mythic_only": mythic_only,
    }
    if not staged and not mythic_only:
        where = "tools/ or Mythic's file store" if mythic_rows is not None else "tools/ (Mythic was not checked)"
        result["operator_message"] = (
            f"I need {query} (any variant) and it isn't in {where}. Drop it into Sage's tools/ folder or upload "
            "it to Mythic, then tell me to continue."
        )
    elif len(staged) + len(mythic_only) > 1:
        result["note"] = (
            "Several variants exist. Choose the one whose kind and arch fit the callback's architecture and the "
            "agent's execution command (an assembly for execute_assembly, a bof for a BOF command)."
        )
    return result


def public_view(entry: dict[str, Any]) -> dict[str, Any]:
    """Drop the internal md5/sha1 used only for the Mythic comparison."""
    return {key: value for key, value in entry.items() if not key.startswith("_")}
