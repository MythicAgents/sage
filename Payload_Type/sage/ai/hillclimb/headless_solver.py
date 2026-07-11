"""Run a full autonomous Sage solve IN-PROCESS — the Option-A replacement for the PayloadType path.

The eval/gauge harness used to run a solve by tasking the PayloadType `query` command on a virtual Sage
callback (`issue_task(command_name="query", callback_display_id=sage_cb)`). Phase 4 removes the PayloadType,
so the harness instead runs the SAME Model the chat service builds, in-process, here.

Auth is option (a): inject a pre-authenticated `mythic` client (the harness's admin login) so the Model's
tools task real callbacks + query BloodHound with no chat channel and no Mythic task; pin the engagement
key (`SAGE_ENGAGEMENT_ID`) to the run's operation so the durable ledger doesn't misfile (an admin client
can see many operations). See `MythicTools.__init__(preauth_client=...)`.

Scoring is unchanged — it stays out-of-band (BloodHound/Mythic ground-truth probes); this only replaces
"how the solve is driven", returning the terminal status string the harness already expects
("completed" / "timeout" / "error: ...").
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or "").strip() or default


async def _noop_emitter(_formatted_message: str) -> bool:
    # In-process solves have no chat channel; streamed assistant text is irrelevant to out-of-band scoring.
    # A no-op emitter keeps Model streaming a clean no-op instead of failing into the PayloadType task RPC.
    return True


async def run_headless_solve(
    objective: str,
    *,
    client: Any,
    operation_id: int,
    engagement_id: str,
    provider: str | None = None,
    model: str | None = None,
    mode: str = "auto",
    autonomous_solve: bool = True,
    policy_mode: str | None = None,
    max_steps: int = 0,
    config: dict | None = None,
    timeout: int = 1800,
    null_model: bool = False,
    return_details: bool = False,
) -> str | dict[str, Any]:
    """Run one full autonomous solve in-process; return the terminal status string.

    :param objective: the solve objective (the prompt the PayloadType `query` command used to receive).
    :param client: a pre-authenticated ``mythic`` client (the harness admin login) — injected into the
        Model's tools; no token mint, no channel/task context needed.
    :param operation_id: the Mythic operation id for this run.
    :param engagement_id: durable engagement/ledger key to pin (``SAGE_ENGAGEMENT_ID``).
    :param provider / model: LLM route; default to the eval env (same keys ``build_model_kwargs`` reads).
    :param max_steps: 0 = unlimited (the central graph recursion budget governs autonomous solves).
    :param timeout: wall-clock budget; on exceed, cooperatively stop and return ``"timeout"`` (mirrors the
        old harness's ``stop`` task).
    :returns: ``"completed"`` | ``"timeout"`` | ``"error: <Type>: <msg>"``.
    """
    try:
        from ai.langgraph.model import Model
    except ModuleNotFoundError:
        # run_gauge_live.py is also invoked as a script, which puts ai/hillclimb (not Payload_Type/sage)
        # on sys.path. Restore the package root so the same headless solver works in both invocation modes.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from ai.langgraph.model import Model

    # Pin the ledger key up front so the engagement ledger is keyed to this run's operation regardless of
    # what an admin-scoped client can see (mirrors the PayloadType path's engagement resolution).
    if engagement_id:
        os.environ["SAGE_ENGAGEMENT_ID"] = engagement_id

    provider = (provider or _env("provider", "openai")).lower()
    model = (model or _env("model", "")).lower()
    policy_mode = (policy_mode or _env("SAGE_POLICY_MODE", "llm")).lower()

    model_class = Model
    if null_model:
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        class NullModel(Model):
            """Build the controller graph without constructing a provider-backed chat model."""

            def _get_base_chat_model(self):
                return FakeListChatModel(responses=[""])

        model_class = NullModel

    llm = model_class(
        provider=provider,
        model=model,
        system_prompt=_env("system_prompt", ""),
        config=config or {},
        task_id=0,
        agent_task_id="",
        mode=mode,
        autonomous_solve=autonomous_solve,
        policy_mode=policy_mode,
        max_steps=max_steps,
        response_emitter=_noop_emitter,
        operation_id=operation_id,
        mythic_preauth_client=client,
    )
    await llm.initialize()
    if null_model:
        # Eval-only ablation: the inert model exists only long enough to build the normal graph shell. Remove
        # the policy inference seam before the controller starts; symbolic is unaffected, learned policies fail closed.
        llm.llm = None

    status: str
    try:
        await asyncio.wait_for(llm.invoke(objective), timeout=timeout)
        status = "completed"
    except asyncio.TimeoutError:
        try:
            llm.request_stop()
        except Exception:
            pass
        status = "timeout"
    except Exception as e:  # a solve error is a terminal status, not a harness crash (mirrors task status)
        status = f"error: {type(e).__name__}: {e}"
    if return_details:
        return {
            "status": status,
            "runtime_telemetry": llm.controller_runtime_telemetry(),
        }
    return status


def solve_headless(objective: str, **kwargs: Any) -> str:
    """Blocking wrapper matching the old ``make_harness_solver`` return contract (`solve(objective)->str`)."""
    return asyncio.run(run_headless_solve(objective, **kwargs))
