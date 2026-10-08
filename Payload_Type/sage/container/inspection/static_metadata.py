"""Bounded, non-executing PE classification for the inspection workload."""

from __future__ import annotations

import struct


_MACHINES = {0x14C: "x86", 0x8664: "x86_64", 0xAA64: "arm64"}


def classify(data: bytes) -> dict:
    """Return header facts only; malformed files remain explicitly unclassified."""
    result = {"format": "unknown", "architecture": None, "managed": None,
              "evidence": "header_only", "partial": True}
    if len(data) < 64 or data[:2] != b"MZ":
        return result
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if offset > len(data) - 24 or data[offset:offset + 4] != b"PE\0\0":
        return result
    machine, sections, _, _, _, optional_size, _ = struct.unpack_from("<HHIIIHH", data, offset + 4)
    optional = offset + 24
    if optional_size < 2 or optional + optional_size > len(data):
        return result
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x10B:
        directory = 96
    elif magic == 0x20B:
        directory = 112
    else:
        return result
    result.update(format="pe", architecture=_MACHINES.get(machine, "other"),
                  machine=machine, sections=sections, managed=False)
    # COM descriptor is data directory 14. Its presence classifies CLR metadata,
    # but does not imply the assembly is runnable under this image.
    if optional_size >= directory + 15 * 8:
        clr_rva, clr_size = struct.unpack_from("<II", data, optional + directory + 14 * 8)
        result["managed"] = bool(clr_rva and clr_size)
    if result["managed"]:
        result.update(_clr_metadata(data))
        if result.get("clr_metadata_status") == "parsed":
            result["evidence"] = "static_pe_clr_metadata"
    return result


def _version(row) -> str:
    return ".".join(str(getattr(row, field)) for field in
                    ("MajorVersion", "MinorVersion", "BuildNumber", "RevisionNumber"))


def _framework_attribute(tables) -> str | None:
    """Decode only the first SerString of the assembly's framework attribute."""
    table = getattr(tables, "CustomAttribute", None)
    for row in table.rows if table else ():
        try:
            if row.Parent.table.name != "Assembly":
                continue
            kind = row.Type.row.Class.row
            if (str(kind.TypeName) != "TargetFrameworkAttribute"
                    or str(kind.TypeNamespace) != "System.Runtime.Versioning"):
                continue
            value = row.Value.value
            if value[:2] != b"\x01\x00" or len(value) < 3:
                return None
            first = value[2]
            if first < 0x80:
                size, offset = first, 3
            elif first < 0xC0 and len(value) >= 4:
                size, offset = ((first & 0x3F) << 8) | value[3], 4
            elif first < 0xE0 and len(value) >= 6:
                size = ((first & 0x1F) << 24) | (value[3] << 16) | (value[4] << 8) | value[5]
                offset = 6
            else:
                return None
            if size > 256 or offset + size > len(value):
                return None
            return value[offset:offset + size].decode("utf-8", "replace")
        except (AttributeError, IndexError, TypeError, ValueError):
            continue
    return None


def _clr_metadata(data: bytes) -> dict:
    try:
        import dnfile
    except ImportError:
        return {"clr_metadata_status": "parser_unavailable"}
    try:
        pe = dnfile.dnPE(data=data)
        net = pe.net
        if net is None:
            return {"clr_metadata_status": "invalid"}
        tables = net.mdtables
        assembly = getattr(tables, "Assembly", None)
        module = getattr(tables, "Module", None)
        refs = getattr(tables, "AssemblyRef", None)
        primary = assembly.rows[0] if assembly and assembly.rows else None
        module_row = module.rows[0] if module and module.rows else None
        reference_rows = refs.rows if refs else ()
        resources = getattr(net, "resources", ())
        return {
            "clr_metadata_status": "parsed",
            "clr_version": net.metadata.struct.Version.decode("utf-8", "replace").strip("\0")[:64],
            "assembly": ({"name": str(primary.Name)[:128], "version": _version(primary)}
                         if primary else None),
            "mvid": str(module_row.Mvid) if module_row else None,
            "target_framework": _framework_attribute(tables),
            "referenced_assemblies": [
                {"name": str(row.Name)[:128], "version": _version(row)}
                for row in reference_rows[:128]
            ],
            "embedded_resources": [
                {"name": str(resource.name)[:128], "size": int(resource.size)}
                for resource in resources[:128]
                if getattr(resource, "size", None) is not None
            ],
            "cli_grammar_status": "not_extracted",
        }
    except (AttributeError, IndexError, KeyError, OSError, TypeError, ValueError):
        return {"clr_metadata_status": "invalid"}
