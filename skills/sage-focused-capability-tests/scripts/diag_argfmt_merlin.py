#!/usr/bin/env python3
"""Throwaway diagnostic (gitignored) — grounds the arg-format + Merlin workstreams.

1. List installed payload types (is Merlin actually installed?).
2. Dump apollo command schema WITH parameter_group_name for jump_wmi / inline_assembly
   / execute_assembly / jump_psexec (the multi-group + ChooseOne commands).
3. Pull recent error/failed tasks for those commands with original_params (reproduce the
   malformation that Sage produced).
"""
import asyncio
import json
import sys

SAGE_ROOT = "/home/john/dev/sage/Payload_Type/sage"
sys.path.insert(0, SAGE_ROOT)

from evals.harness import resolve_password, login_to_mythic  # noqa: E402
from mythic import mythic  # noqa: E402

PAYLOAD_TYPES_Q = """
query AllPayloadTypes {
  payloadtype { name note }
}
"""

APOLLO_CMDS_Q = """
query ApolloCmdSchema {
  command(where: {payloadtype: {name: {_eq: "apollo"}},
                  cmd: {_in: ["jump_wmi","inline_assembly","execute_assembly","jump_psexec","wmiexecute"]}},
          order_by: {cmd: asc}) {
    cmd
    commandparameters(order_by: {parameter_group_name: asc}) {
      name
      cli_name
      type
      parameter_group_name
      required
      choices
      default_value
    }
  }
}
"""

MERLIN_CMDS_Q = """
query MerlinCmdSchema {
  command(where: {payloadtype: {name: {_eq: "merlin"}},
                  deleted: {_eq: false}},
          order_by: {cmd: asc}) {
    cmd
    needs_admin
    description
    commandparameters(order_by: {parameter_group_name: asc}) {
      name
      cli_name
      type
      parameter_group_name
      required
      choices
      default_value
    }
  }
}
"""

TOOL_FILES_Q = """
query ToolFiles {
  filemeta(where: {deleted: {_eq: false}, is_payload: {_eq: false}},
           order_by: {id: desc}, limit: 200) {
    id
    agent_file_id
    filename_utf8
    complete
    timestamp
  }
}
"""

ERR_TASKS_Q = """
query ErrTasks {
  task(where: {command_name: {_in: ["jump_wmi","inline_assembly","execute_assembly","jump_psexec"]}},
       order_by: {id: desc}, limit: 30) {
    display_id
    command_name
    original_params
    status
    timestamp
  }
}
"""


async def main():
    pw = resolve_password()
    client = await login_to_mythic(pw)

    print("=" * 70)
    print("SECTION 1 — INSTALLED PAYLOAD TYPES (Merlin check)")
    print("=" * 70)
    try:
        r = await mythic.execute_custom_query(client, PAYLOAD_TYPES_Q)
        names = [p["name"] for p in r.get("payloadtype", [])]
        print(json.dumps(names, indent=2))
        print(f"\nMERLIN INSTALLED: {'merlin' in [n.lower() for n in names]}")
    except Exception as e:
        print(f"ERR payload types: {e}")

    print("\n" + "=" * 70)
    print("SECTION 2 — APOLLO COMMAND SCHEMA (with parameter_group_name)")
    print("=" * 70)
    try:
        r = await mythic.execute_custom_query(client, APOLLO_CMDS_Q)
        for cmd in r.get("command", []):
            print(f"\n### {cmd['cmd']}")
            # group params by parameter_group_name
            groups = {}
            for p in cmd.get("commandparameters", []):
                g = p.get("parameter_group_name") or "(none)"
                groups.setdefault(g, []).append(p)
            for g, params in groups.items():
                print(f"  GROUP '{g}':")
                for p in params:
                    ch = p.get("choices")
                    ch_s = f" choices={ch}" if ch else ""
                    print(f"    - {p['name']} (cli={p.get('cli_name')}, type={p.get('type')}, "
                          f"required={p.get('required')}){ch_s}")
    except Exception as e:
        print(f"ERR apollo cmds: {e}")

    print("\n" + "=" * 70)
    print("SECTION 2b — MERLIN COMMAND SCHEMA (filtered for exec/read/proof commands)")
    print("=" * 70)
    interesting = (
        "assembly", "cat", "cmd", "download", "exec", "get", "ls", "powershell",
        "ps", "read", "rev", "revert", "shell", "token", "impersonat", "upload", "wmi",
    )
    try:
        r = await mythic.execute_custom_query(client, MERLIN_CMDS_Q)
        for cmd in r.get("command", []):
            name = cmd["cmd"]
            haystack = " ".join([name, cmd.get("description") or ""]).lower()
            if not any(term in haystack for term in interesting):
                continue
            print(f"\n### {name}  admin={cmd.get('needs_admin')}")
            desc = (cmd.get("description") or "").strip().replace("\n", " ")
            if desc:
                print(f"  {desc[:220]}")
            groups = {}
            for p in cmd.get("commandparameters", []):
                g = p.get("parameter_group_name") or "(none)"
                groups.setdefault(g, []).append(p)
            if not groups:
                print("  (no parameters)")
            for g, params in groups.items():
                print(f"  GROUP '{g}':")
                for p in params:
                    ch = p.get("choices")
                    ch_s = f" choices={ch}" if ch else ""
                    default = p.get("default_value")
                    default_s = f" default={default!r}" if default not in (None, "") else ""
                    print(f"    - {p['name']} (cli={p.get('cli_name')}, type={p.get('type')}, "
                          f"required={p.get('required')}){default_s}{ch_s}")
    except Exception as e:
        print(f"ERR merlin cmds: {e}")

    print("\n" + "=" * 70)
    print("SECTION 2c — REGISTERED TOOL FILES (filtered)")
    print("=" * 70)
    tool_terms = (
        "certify", "rubeus", "sharp", "standin", "seatbelt", "lapstoolkit",
        "whisker", "forgecert", "goldencert",
    )
    try:
        r = await mythic.execute_custom_query(client, TOOL_FILES_Q)
        rows = r.get("filemeta", []) if isinstance(r, dict) else []
        matched = []
        for row in rows:
            name = row.get("filename_utf8") or ""
            if any(term in name.lower() for term in tool_terms):
                matched.append(row)
        if not matched:
            print("  (no matching registered tool files in latest 200 filemeta rows)")
        for row in matched:
            print(
                f"  {row.get('filename_utf8')} complete={row.get('complete')} "
                f"uuid={row.get('agent_file_id')} ts={row.get('timestamp')}"
            )
    except Exception as e:
        print(f"ERR tool files: {e}")

    print("\n" + "=" * 70)
    print("SECTION 3 — RECENT TASKS FOR THESE COMMANDS (reproduce malformation)")
    print("=" * 70)
    try:
        r = await mythic.execute_custom_query(client, ERR_TASKS_Q)
        for t in r.get("task", []):
            print(f"\n  #{t['display_id']} {t['command_name']} status={t['status']} @ {t['timestamp']}")
            print(f"    original_params: {t['original_params']}")
    except Exception as e:
        print(f"ERR tasks: {e}")


if __name__ == "__main__":
    asyncio.run(main())
