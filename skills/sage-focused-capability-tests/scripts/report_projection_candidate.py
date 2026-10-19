"""Development-only literal source projection for bounded native reports.

Install around one matrix case. Nothing in the Sage runtime imports this module.
The boundary preserves literal source content and provenance. Tool text and proposed
conditions can still be misleading; an independent grader must check their meaning.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import json
from typing import Any
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage, ToolMessage


PROTOCOL = ("For operator-facing narrative/report content, use a JSON object of the exact form "
            '{"proposals":[{"verify":["neutral condition requiring observation"],'
            '"if_verified":"proposed next action"}]}. '
            "Put this JSON in any final answer or summary string required by an existing tool. "
            "Keep tool names, arguments, routing, and approval flow as their tool schemas require. "
            "Use an empty proposals list for a factual-only answer. Within this report JSON, "
            "do not add fact, summary, rationale, or other fields. Keep verification "
            "conditions neutral and conditional; do not assert unobserved premises.")
CONTROL_TOOLS = frozenset({"respond_to_user", "request_continuation",
                           "summarize_and_handback", "handback_to_supervisor"})
MAX_OBSERVATIONS = 24
MAX_SOURCE_CHARS = 4000
MAX_TOTAL_CHARS = 16000


class ReportProjectionCandidate:
    """Per-case audit and source store; instantiate anew for each matrix case."""

    def __init__(self) -> None:
        self.observations: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.candidate_format_failures: list[dict[str, Any]] = []
        self.protocol_requests: list[dict[str, Any]] = []
        self.request_id: str | None = None
        self._rendered: dict[str, tuple[str | None, int, str]] = {}

    def audit(self) -> dict[str, Any]:
        """Serializable record for a retained per-case diagnostic."""
        return {"protocol_requests": self.protocol_requests,
                "observations": self.observations,
                "events": self.events,
                "candidate_format_failures": self.candidate_format_failures}

    def collect(self, callback: Any, output: Any) -> None:
        if not isinstance(output, ToolMessage):
            return
        name = str(output.name or callback._tool_call_to_name.get(output.tool_call_id, ""))
        if not name or name in CONTROL_TOOLS or name.startswith("transfer_to_"):
            return
        # The callback receives a ToolMessage only after the worker's actual call.
        # An unpaired injected message is not an observation for this projection.
        if output.tool_call_id not in callback._tool_call_to_name:
            return
        content = output.content if isinstance(output.content, str) else json.dumps(
            output.content, ensure_ascii=False, sort_keys=True, default=str)
        args = getattr(callback, "_tool_call_to_args", {}).get(output.tool_call_id)
        args = json.loads(json.dumps(args, ensure_ascii=False, default=str))
        self.observations.append({"request_id": self.request_id, "agent": callback.agent_name,
                                  "tool": name, "tool_call_id": output.tool_call_id,
                                  "arguments": args,
                                  "status": str(getattr(output, "status", "success")),
                                  "content": content})

    def _sources(self) -> str:
        if not self.observations:
            return "No tool observations were collected for this case. Scope and requested facts remain unknown."
        lines = ["Observed tool results (raw excerpts; no inferred facts):"]
        remaining = MAX_TOTAL_CHARS
        for row in self.observations[:MAX_OBSERVATIONS]:
            if remaining <= 0:
                break
            raw = row["content"]
            excerpt = raw[:min(MAX_SOURCE_CHARS, remaining)]
            remaining -= len(excerpt)
            label = (f"- source tool={row['tool']} call={row['tool_call_id']} "
                     f"collected_request={row['request_id'] or 'unknown'} "
                     f"status={row['status']}; exact output excerpt:")
            arguments = json.dumps(row["arguments"], ensure_ascii=False, sort_keys=True,
                                   default=str) if row["arguments"] is not None else "unavailable"
            if len(arguments) > 1000:
                arguments = arguments[:1000] + f" [arguments truncated: {len(arguments) - 1000} characters omitted]"
            lines += [label, f"  arguments={arguments}", "```text", excerpt, "```"]
            if len(excerpt) < len(raw):
                lines.append(f"[source truncated: {len(raw) - len(excerpt)} characters omitted]")
        if len(self.observations) > MAX_OBSERVATIONS or remaining <= 0:
            lines.append(f"[source list capped: {max(0, len(self.observations) - MAX_OBSERVATIONS)} records omitted; further content may be omitted]")
        lines.append("The excerpts establish only their displayed fields and text; unshown properties are unknown.")
        return "\n".join(lines)

    @staticmethod
    def _parse_proposals(raw: str) -> list[dict[str, Any]] | None:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(value, dict) or set(value) != {"proposals"} or not isinstance(value["proposals"], list):
            return None
        for item in value["proposals"]:
            if not isinstance(item, dict) or set(item) != {"verify", "if_verified"}:
                return None
            if (not isinstance(item["verify"], list) or not item["verify"]
                    or any(not isinstance(v, str) or not v.strip() for v in item["verify"])
                    or not isinstance(item["if_verified"], str) or not item["if_verified"].strip()):
                return None
        return value["proposals"]

    def project(self, raw: str, surface: str) -> str:
        # Only exact known native wrappers may carry a previously rendered report.
        # A new observation forces recomposition from the original proposal JSON.
        candidate = raw.strip()
        for prefix in ("🤖[Supervisor]> ", "🤖[Mythic_Operator]> ", "🤖[Generalist]> "):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):].strip()
                break
        cache = self._rendered.get(candidate)
        if cache and cache[0] == self.request_id:
            if cache[1] == len(self.observations):
                if raw != candidate:
                    self.events.append({"surface": surface, "request_id": self.request_id,
                                        "raw": raw, "displayed": candidate,
                                        "format_valid": True, "reused_projection": True})
                return candidate
            raw = cache[2]
        if raw.startswith("📊 **Final Report**\n"):
            raw = raw.split("\n", 1)[1]
        proposals = self._parse_proposals(raw)
        if proposals is None:
            self.candidate_format_failures.append({"surface": surface,
                                                   "request_id": self.request_id,
                                                   "raw": raw})
        lines = [self._sources()]
        if proposals is None:
            lines.append("Candidate format failure: no valid conditional proposal was supplied.")
        elif proposals:
            lines.append("Proposed next actions (model-authored; conditions require independent checking):")
            for item in proposals:
                conditions = "; ".join(v.strip() for v in item["verify"])
                lines.append(f"- Before acting, verify {conditions}; If verified, {item['if_verified'].strip()}")
        else:
            lines.append("No next action proposed.")
        displayed = "\n\n".join(lines)
        self._rendered[displayed] = (self.request_id, len(self.observations), raw)
        self.events.append({"surface": surface, "request_id": self.request_id,
                            "raw": raw, "displayed": displayed,
                            "format_valid": proposals is not None})
        return displayed


@contextmanager
def install(adapter: ReportProjectionCandidate, *, matrix_module=None):
    """Reversibly patch the existing native matrix during one case execution.

    The transport's original handler still owns request journaling, budget checks,
    network allowlisting, and response capture. The modified body is what it records.
    """
    from ai.langgraph import model as model_module
    from sage_chat import service
    if matrix_module is None:
        import run_operator_prompt_matrix as matrix
    else:
        matrix = matrix_module

    original_contract = model_module.Model.install_request_contract
    original_tool_end = model_module.MessageCaptureCallback.on_tool_end
    original_format = model_module.Model._format_message_for_streaming
    original_open = model_module.Model._open_delegation
    original_close = model_module.Model._close_delegation
    original_agent_text = model_module.Model._emit_agent_text
    original_service = service._with_task_provenance
    original_transport = matrix.MatrixTransport.handle_async_request

    def contract(self, value):
        result = original_contract(self, value)
        adapter.request_id = str(getattr(value, "request_id", "") or "") or None
        return result

    async def tool_end(self, output, **kwargs):
        await original_tool_end(self, output, **kwargs)
        adapter.collect(self, output)

    def formatted(self, message, agent_name=None):
        if isinstance(message, AIMessage) and message.content:
            text = message.content if isinstance(message.content, str) else None
            if text and not getattr(message, "tool_calls", None):
                replacement = adapter.project(text, f"stream:{agent_name or message.name or 'AI'}")
                message = message.model_copy(update={"content": replacement})
            elif text:
                adapter.events.append({"surface": "intermediate_suppressed",
                                       "request_id": adapter.request_id, "raw": text,
                                       "displayed": ""})
                message = message.model_copy(update={"content": ""})
        return original_format(self, message, agent_name=agent_name)

    async def agent_text(self, *, content, delegation_id, delegation_name):
        return await original_agent_text(self, content=adapter.project(content, "worker_agent_text"),
                                         delegation_id=delegation_id,
                                         delegation_name=delegation_name)

    async def open_delegation(self, agent_name, instruction, source_seq, title=""):
        safe_title = f"Review with {agent_name}"
        adapter.events.append({"surface": "delegation_title", "request_id": adapter.request_id,
                               "raw": title, "displayed": safe_title, "format_valid": True})
        return await original_open(self, agent_name, instruction, source_seq, title=safe_title)

    async def close(self, agent_name, content="", status="finished"):
        delegation = getattr(self, "_active_delegations", {}).get(agent_name)
        if isinstance(delegation, dict):
            for key in ("final_summary", "last_text"):
                value = delegation.get(key)
                if isinstance(value, str) and value.strip():
                    delegation[key] = adapter.project(value, f"delegation_{key}")
            chunks = delegation.get("streamed_text_chunks")
            if isinstance(chunks, list):
                delegation["streamed_text_chunks"] = [adapter.project(v, "delegation_chunk")
                                                       if isinstance(v, str) and v.strip() else v
                                                       for v in chunks]
        if content.strip():
            content = adapter.project(content, "delegation_close")
        return await original_close(self, agent_name, content=content, status=status)

    def service_final(model, content):
        return original_service(model, adapter.project(content, "service_final"))

    async def transport(self, request):
        try:
            body = copy.deepcopy(json.loads(request.content))
        except (TypeError, ValueError):
            return await original_transport(self, request)
        messages = body.get("messages")
        if isinstance(messages, list):
            system = next((m for m in messages if isinstance(m, dict) and m.get("role") == "system"
                           and isinstance(m.get("content"), str)), None)
            if system is not None:
                system["content"] += "\n\n" + PROTOCOL
                wire = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
                headers = dict(request.headers)
                headers.pop("content-length", None)
                request = httpx.Request(request.method, request.url, headers=headers, content=wire,
                                        extensions=request.extensions)
                adapter.protocol_requests.append({"request_id": adapter.request_id,
                                                  "body": copy.deepcopy(body)})
        return await original_transport(self, request)

    with (patch.object(model_module.Model, "install_request_contract", contract),
          patch.object(model_module.MessageCaptureCallback, "on_tool_end", tool_end),
          patch.object(model_module.Model, "_format_message_for_streaming", formatted),
          patch.object(model_module.Model, "_emit_agent_text", agent_text),
          patch.object(model_module.Model, "_open_delegation", open_delegation),
          patch.object(model_module.Model, "_close_delegation", close),
          patch.object(service, "_with_task_provenance", service_final),
          patch.object(matrix.MatrixTransport, "handle_async_request", transport)):
        yield adapter
