#!/usr/bin/env python3
"""Throwaway (gitignored) — offline verification of _validate_command_parameters.
Pre-seeds the schema cache with the LIVE-pulled apollo schemas so no Mythic call happens.
Verifies the three reproduced failure modes + that the correct form passes (ISC-37/38/39/40/42)."""
import asyncio
import sys

sys.path.insert(0, "/home/john/dev/sage/Payload_Type/sage")
from ai.langgraph.mythic_tools import MythicTools  # noqa: E402

# Real apollo schemas (pulled live 2026-06-05), as flat commandparameter lists.
JUMP_WMI = [
    {"name": "command", "cli_name": "command", "type": "String", "parameter_group_name": "Default", "required": False, "choices": None},
    {"name": "host", "cli_name": "host", "type": "String", "parameter_group_name": "Default", "required": True, "choices": None},
    {"name": "remote_path", "cli_name": "remote_path", "type": "String", "parameter_group_name": "Default", "required": False, "choices": None},
    {"name": "Payload", "cli_name": "Payload", "type": "ChooseOne", "parameter_group_name": "specific_payload", "required": True, "choices": None},
    {"name": "command", "cli_name": "command", "type": "String", "parameter_group_name": "specific_payload", "required": False, "choices": None},
    {"name": "remote_path", "cli_name": "remote_path", "type": "String", "parameter_group_name": "specific_payload", "required": False, "choices": None},
    {"name": "host", "cli_name": "host", "type": "String", "parameter_group_name": "specific_payload", "required": True, "choices": None},
]
INLINE_ASSEMBLY = [
    {"name": "assembly_name", "cli_name": "Assembly", "type": "ChooseOne", "parameter_group_name": "Default", "required": True, "choices": None},
    {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String", "parameter_group_name": "Default", "required": False, "choices": None},
    {"name": "assembly_file", "cli_name": "assembly_file", "type": "File", "parameter_group_name": "New Assembly", "required": True, "choices": None},
    {"name": "assembly_arguments", "cli_name": "Arguments", "type": "String", "parameter_group_name": "New Assembly", "required": False, "choices": None},
]


def make_tools():
    t = object.__new__(MythicTools)  # skip __init__ (no Mythic client needed)
    t.client = object()
    t._cmd_schema_cache = {
        ("apollo", "jump_wmi"): JUMP_WMI,
        ("apollo", "inline_assembly"): INLINE_ASSEMBLY,
    }

    async def _fake_resolve(_cb):
        return "apollo"

    t._resolve_payload_type = _fake_resolve
    return t


async def run():
    t = make_tools()
    results = []

    async def check(label, command, params, expect_error, must_contain=None):
        msg = await t._validate_command_parameters(command, params, 24)
        got_error = msg is not None
        ok = got_error == expect_error
        if ok and expect_error and must_contain:
            ok = all(s.lower() in msg.lower() for s in must_contain)
        results.append((ok, label, msg))
        print(f"[{'PASS' if ok else 'FAIL'}] {label}\n        -> {msg}")

    # mode A — invented names (#1789)
    await check("ISC-37 mode A: jump_wmi {computer,payload}", "jump_wmi",
                {"computer": "WINTERFELL", "payload": "x"}, True, ["unknown", "host"])
    # mode B — group mix (#1762)
    await check("ISC-38 mode B: inline_assembly {assembly_file+assembly_name}", "inline_assembly",
                {"assembly_file": "uuid", "assembly_name": "Sharp.exe", "assembly_arguments": "-a"}, True,
                ["one parameter group"])
    # mode C — bare-uuid ChooseOne (#1790)
    await check("ISC-39 mode C: jump_wmi Payload=bare-uuid", "jump_wmi",
                {"Payload": "34601126-e3e9-4f1c-85de-a9f2b5635a2b", "host": "WINTERFELL",
                 "command": "C:\\x.exe", "remote_path": "ADMIN$\\x.exe"}, True, ["display string"])
    # ISC-42 — correct specific_payload form (#1830) passes
    await check("ISC-42 correct jump_wmi (display string) passes", "jump_wmi",
                {"Payload": "apollo_R.exe - Russel Apollo - ab658b46-89d4-4206-979b-3313348784b9",
                 "host": "WINTERFELL", "command": "C:\\x.exe", "remote_path": "ADMIN$\\x.exe"}, False)
    # correct New Assembly form passes (#1763)
    await check("correct inline_assembly (New Assembly group) passes", "inline_assembly",
                {"assembly_file": "62aae9fd-c69d-41db-a72b-1b58501ef225", "assembly_arguments": "-c All"}, False)
    # ISC-40 fail-open — empty dict
    await check("ISC-40 fail-open: empty params", "jump_wmi", {}, False)

    # ISC-40 fail-open — unresolvable payload type
    async def _none(_cb):
        return None
    t._resolve_payload_type = _none
    await check("ISC-40 fail-open: unresolvable payload type", "jump_wmi",
                {"computer": "X"}, False)

    print("\n" + "=" * 50)
    passed = sum(1 for ok, _, _ in results if ok)
    print(f"RESULT: {passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run()) else 1)
