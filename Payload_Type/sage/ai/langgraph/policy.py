"""Explicit semantic policy backends for Sage's deterministic execution kernel."""
from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4


POLICY_LLM = "llm"
POLICY_SYMBOLIC = "symbolic"
POLICY_MODES = frozenset((POLICY_LLM, POLICY_SYMBOLIC))


def normalize_policy_mode(value: Any, default: str = POLICY_LLM) -> str:
    mode = str(value or "").strip().casefold()
    return mode if mode in POLICY_MODES else default


def new_episode_id() -> str:
    return f"episode-{uuid4().hex}"


def _candidate_payload(candidate: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(candidate, "name", "") or ""),
        "target": str(getattr(candidate, "target", "") or ""),
        "preconditions": [str(v) for v in (getattr(candidate, "preconditions", None) or [])],
        "effects": [str(v) for v in (getattr(candidate, "effects", None) or [])],
        "reason": str(getattr(candidate, "reason", "") or ""),
    }


def candidate_hash(candidates: list[Any]) -> str:
    raw = json.dumps(
        [_candidate_payload(candidate) for candidate in candidates],
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True)
class PolicyDecision:
    episode_id: str
    decision_id: str
    policy_mode: str
    candidate_hash: str
    disposition: str
    selected_index: int | None = None
    selected_capability: str = ""
    selected_target: str = ""
    rationale: str = ""
    confidence: float | None = None
    expected_evidence: str = ""
    model_provider: str = ""
    model_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SymbolicPolicy:
    """Behavior-preserving baseline: choose the first admissible candidate."""

    mode = POLICY_SYMBOLIC

    async def select(
        self,
        *,
        episode_id: str,
        objective: str,
        state: Any,
        candidates: list[Any],
        history: list[PolicyDecision],
        budgets: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        del objective, state, history, budgets
        selected = candidates[0] if candidates else None
        return PolicyDecision(
            episode_id=episode_id,
            decision_id=f"decision-{uuid4().hex}",
            policy_mode=self.mode,
            candidate_hash=candidate_hash(candidates),
            disposition="select" if selected is not None else "stop",
            selected_index=0 if selected is not None else None,
            selected_capability=str(getattr(selected, "name", "") or ""),
            selected_target=str(getattr(selected, "target", "") or ""),
            rationale="first admissible candidate in the symbolic baseline",
        )


class LLMPolicy:
    """Model-mediated capability selector with no symbolic fallback."""

    mode = POLICY_LLM

    def __init__(
        self,
        decide: Callable[[dict[str, Any]], Any | Awaitable[Any]] | None,
        *,
        provider: str = "",
        model_id: str = "",
    ):
        self._decide = decide
        self.provider = str(provider or "")
        self.model_id = str(model_id or "")

    @staticmethod
    def request_payload(
        objective: str,
        state: Any,
        candidates: list[Any],
        history: list[PolicyDecision],
        budgets: dict[str, Any] | None,
    ) -> dict[str, Any]:
        achieved = []
        try:
            achieved = sorted(str(v) for v in (state.achieved_effects() or []))
        except Exception:
            pass
        return {
            "objective": str(objective or ""),
            "achieved_effects": achieved,
            "candidates": [
                {"index": index, **_candidate_payload(candidate)}
                for index, candidate in enumerate(candidates)
            ],
            "prior_decisions": [
                {
                    "decision_id": item.decision_id,
                    "disposition": item.disposition,
                    "capability": item.selected_capability,
                    "target": item.selected_target,
                }
                for item in history[-8:]
            ],
            "budgets": dict(budgets or {}),
            "response_schema": {
                "disposition": "select|stop|ask",
                "candidate_index": "integer required for select",
                "rationale": "short string",
                "confidence": "number 0..1",
                "expected_evidence": "short string",
            },
        }

    @staticmethod
    def _response_dict(value: Any) -> dict[str, Any]:
        if hasattr(value, "content"):
            value = value.content
        if isinstance(value, list):
            value = "\n".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in value
            )
        if isinstance(value, dict):
            return value
        text = str(value or "").strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.casefold().startswith("json"):
                text = text[4:].strip()
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(text[start:end + 1])
                    return parsed if isinstance(parsed, dict) else {}
                except Exception:
                    pass
        return {}

    async def select(
        self,
        *,
        episode_id: str,
        objective: str,
        state: Any,
        candidates: list[Any],
        history: list[PolicyDecision],
        budgets: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        c_hash = candidate_hash(candidates)
        if not candidates:
            return self._stop(episode_id, c_hash, "no admissible candidates")
        if self._decide is None:
            return self._stop(episode_id, c_hash, "LLM policy has no model decision seam")

        request = self.request_payload(objective, state, candidates, history, budgets)
        try:
            response = self._decide(request)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:
            return self._stop(episode_id, c_hash, f"LLM policy call failed: {type(exc).__name__}: {exc}")

        parsed = self._response_dict(response)
        disposition = str(parsed.get("disposition") or "").strip().casefold()
        rationale = str(parsed.get("rationale") or "").strip()
        if disposition in {"stop", "ask"}:
            return PolicyDecision(
                episode_id=episode_id,
                decision_id=f"decision-{uuid4().hex}",
                policy_mode=self.mode,
                candidate_hash=c_hash,
                disposition=disposition,
                rationale=rationale or f"model requested {disposition}",
                model_provider=self.provider,
                model_id=self.model_id,
            )
        try:
            index = int(parsed.get("candidate_index"))
        except (TypeError, ValueError):
            return self._stop(episode_id, c_hash, "LLM policy returned no valid candidate index")
        if disposition != "select" or index < 0 or index >= len(candidates):
            return self._stop(episode_id, c_hash, "LLM policy returned an invalid selection")

        selected = candidates[index]
        confidence = parsed.get("confidence")
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = None
        return PolicyDecision(
            episode_id=episode_id,
            decision_id=f"decision-{uuid4().hex}",
            policy_mode=self.mode,
            candidate_hash=c_hash,
            disposition="select",
            selected_index=index,
            selected_capability=str(getattr(selected, "name", "") or ""),
            selected_target=str(getattr(selected, "target", "") or ""),
            rationale=rationale,
            confidence=confidence,
            expected_evidence=str(parsed.get("expected_evidence") or "").strip(),
            model_provider=self.provider,
            model_id=self.model_id,
        )

    def _stop(self, episode_id: str, c_hash: str, rationale: str) -> PolicyDecision:
        return PolicyDecision(
            episode_id=episode_id,
            decision_id=f"decision-{uuid4().hex}",
            policy_mode=self.mode,
            candidate_hash=c_hash,
            disposition="stop",
            rationale=rationale,
            model_provider=self.provider,
            model_id=self.model_id,
        )
