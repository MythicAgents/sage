"""LIVE seam adapters for the eval gauge.

⚠️  STATUS: written, NOT validated. Unlike the 9 tested gauge modules, these call your live LLM,
    live Mythic, and live BloodHound MCP — none reachable from here. The PURE parsing helpers
    (`extract_literals`, `parse_model_decision`) ARE unit-tested; the factories that touch the lab
    are best-effort, grounded in how Sage already does each thing (cited inline). Search this file
    for `FILL IN` for the spots you must confirm against your environment, then run on GOAD to verify.

Three seams (the only things the gauge still needs to run live):
  * make_model_fn   -> the bare LLM with tools   (grounded: model.py:1331 init_chat_model)
  * make_tool_executor -> raw Mythic command exec (grounded: evals/harness.py mythic.issue_task)
  * make_cypher_run -> BloodHound MCP cypher      (grounded: graph_reconciler cypher_query / data.literals)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable


# ---------------------------------------------------------------------------------------------------
# PURE helpers (unit-tested) — the error-prone parsing the seams depend on.
# ---------------------------------------------------------------------------------------------------

def extract_literals(resp: Any) -> list:
    """BloodHound MCP 'run' response -> flat list of scalar values from data.literals.
    Grounded in graph_reconciler: the CE cypher API returns scalars under data.literals as
    a list of {value, key} dicts. Fail-open -> []."""
    if not isinstance(resp, dict):
        return []
    literals = ((resp.get("data") or {}).get("literals")) or []
    return [lit.get("value") for lit in literals if isinstance(lit, dict) and lit.get("value") is not None]


def parse_model_decision(message: Any) -> dict:
    """A LangChain AIMessage -> the bare-runner decision shape.
    Tool call -> {"tool": name, "args": {...}}; otherwise -> {"final": text}."""
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        first = tool_calls[0]
        return {"tool": first.get("name"), "args": first.get("args", {}) or {}}
    content = getattr(message, "content", "") or ""
    return {"final": content if isinstance(content, str) else str(content)}


def _render_history(objective: str, history: list) -> str:
    """Render the running transcript as text the bare model reads as its own memory."""
    lines = [f"Objective: {objective}"] if objective else []
    for step in history:
        call = step.get("call", {})
        lines.append(f"\nACTION: {json.dumps(call)}\nOBSERVATION: {step.get('obs', '')}")
    lines.append("\nDecide the next single tool call, or FINAL if done/stuck.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------
# LIVE factories (NOT unit-tested — verify on the lab).
# ---------------------------------------------------------------------------------------------------

def make_model_fn(
    provider: str,             # FILL IN: same as Sage's self.provider (model.py config)
    model: str,                # FILL IN: same model id Sage runs
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    objective: str = "",
) -> Callable[[str, list, list], dict]:
    """The bare LLM (same model Sage uses) with tool-calling, NO scaffolding.
    Grounded: model.py:1331 `init_chat_model(model_provider=..., model=...)`."""
    from langchain.chat_models import init_chat_model  # imported lazily; needs the LLM creds at runtime

    kwargs: dict = {"model_provider": provider, "model": model}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    llm = init_chat_model(**kwargs)

    def model_fn(system: str, tools_spec: list, history: list) -> dict:
        bound = llm.bind_tools(tools_spec) if tools_spec else llm
        messages = [("system", system), ("human", _render_history(objective, history))]
        return parse_model_decision(bound.invoke(messages))

    return model_fn


def default_mythic_client():
    """Log in to local Mythic the way sage_task.py does (validated path)."""
    from mythic import mythic  # type: ignore
    import os
    from pathlib import Path

    pw = os.environ.get("MYTHIC_ADMIN_PASSWORD")
    if not pw:
        env = Path("/home/john/dev/mythic/.env")  # FILL IN if your Mythic .env lives elsewhere
        for line in (env.read_text(encoding="utf-8").splitlines() if env.exists() else []):
            if line.strip().startswith("MYTHIC_ADMIN_PASSWORD="):
                pw = line.split("=", 1)[1].strip().strip("'\"")
                break
    return asyncio.run(mythic.login(server_ip="127.0.0.1", username="mythic_admin", password=pw))


def make_tool_executor(
    client: Any,                       # logged-in mythic client (default_mythic_client())
    callback_display_id: int,          # the APOLLO callback the bare model acts through (e.g. cb4)
    *,
    timeout: int = 300,
) -> Callable[[dict], str]:
    """Raw Mythic command execution for the bare model — issue a command to the callback, return output.

    VALIDATED 2026-06-18: this is exactly the path `sage_task.py task-callback` uses, which returned real
    Apollo output (`whoami` -> 'NORTH\\samwell.tarly') from cb4. issue_task(wait_for_complete=False) then
    waitfor_for_task_output."""
    from mythic import mythic  # type: ignore

    async def _run(call: dict) -> str:
        cmd = call.get("tool", "")
        args = call.get("args", {})
        # Empty args -> "" (not "{}"): matches the proven no-arg path (Apollo rejects an empty-dict arg).
        params = args if isinstance(args, str) else (json.dumps(args) if args else "")
        task = await mythic.issue_task(
            client, command_name=cmd, parameters=params,
            callback_display_id=callback_display_id, wait_for_complete=False,
        )
        out = await mythic.waitfor_for_task_output(client, task_display_id=task["display_id"], timeout=timeout)
        return out.decode(errors="replace") if isinstance(out, (bytes, bytearray)) else str(out)

    def tool_executor(call: dict) -> str:
        return asyncio.run(_run(call))

    return tool_executor


def make_cypher_run(mcp_tool: Any, *, timeout: int = 30) -> Callable[[str], list]:
    """BloodHound MCP cypher runner for the probes.
    Grounded: graph_reconciler resolves the `cypher_query` MCP tool and invokes
    `ainvoke({"info_type":"run","query":q,"include_properties":False})`, reading data.literals.
    FILL IN: pass the resolved MCP tool (e.g. from your MCP_Manager.get_tool_by_name('cypher_query'))."""
    def cypher_run(query: str) -> list:
        async def _q():
            return await asyncio.wait_for(
                mcp_tool.ainvoke({"info_type": "run", "query": query, "include_properties": False}),
                timeout=timeout,
            )
        return extract_literals(asyncio.run(_q()))

    return cypher_run
