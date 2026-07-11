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
POLICY_HYBRID = "hybrid"
POLICY_MODES = frozenset((POLICY_LLM, POLICY_SYMBOLIC, POLICY_HYBRID))


def normalize_policy_mode(value: Any, default: str = POLICY_LLM) -> str:
    mode = str(value or "").strip().casefold()
    if not mode:
        mode = str(default or "").strip().casefold()
    if mode not in POLICY_MODES:
        raise ValueError(f"unsupported policy mode: {mode or value!r}")
    return mode


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


def _normalized_state_payload(state: Any) -> dict[str, Any]:
    def values(name: str) -> list[Any]:
        return list(getattr(state, name, None) or [])

    achieved = []
    try:
        achieved = sorted(str(v) for v in (state.achieved_effects() or []))
    except Exception:
        pass
    return {
        "achieved_effects": achieved,
        "footholds": [
            {
                "callback_id": str(getattr(item, "callback_id", "") or ""),
                "agent": str(getattr(item, "agent", "") or ""),
                "host": str(getattr(item, "host", "") or ""),
                "forest": str(getattr(item, "forest", "") or ""),
                "identity": str(getattr(item, "identity", "") or ""),
                "integrity": str(getattr(item, "integrity", "") or ""),
                "alive": bool(getattr(item, "alive", False)),
            }
            for item in values("footholds")
        ],
        "graph_facts": sorted(
            str(getattr(item, "predicate", item) or "")
            for item in values("graph_facts")
            if str(getattr(item, "predicate", item) or "")
        ),
        "recent_outcomes": [
            {
                "capability": str(getattr(item, "technique", "") or ""),
                "target": str(getattr(item, "target", "") or ""),
                "effect": str(getattr(item, "effect", "") or ""),
                "status": str(getattr(item, "status", "") or ""),
            }
            for item in values("hops")[-12:]
        ],
    }


def _prior_decisions_payload(history: list["PolicyDecision"]) -> list[dict[str, Any]]:
    return [
        {
            "decision_id": item.decision_id,
            "disposition": item.disposition,
            "capability": item.selected_capability,
            "target": item.selected_target,
        }
        for item in history[-8:]
    ]


def _selection_key(value: Any) -> str:
    return str(value or "").strip().casefold()


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
        catalog: list[dict[str, Any]] | None = None,
    ):
        self._decide = decide
        self.provider = str(provider or "")
        self.model_id = str(model_id or "")
        self.catalog = [dict(item) for item in (catalog or []) if isinstance(item, dict)]

    def request_payload(
        self,
        objective: str,
        state: Any,
        candidates: list[Any],
        history: list[PolicyDecision],
        budgets: dict[str, Any] | None,
    ) -> dict[str, Any]:
        del candidates
        return {
            "objective": str(objective or ""),
            "normalized_state": _normalized_state_payload(state),
            "capability_catalog": [dict(item) for item in self.catalog],
            "prior_decisions": _prior_decisions_payload(history),
            "budgets": dict(budgets or {}),
            "selection_contract": "semantic_catalog",
            "response_schema": {
                "disposition": "select|stop|ask",
                "capability": "catalog capability name required for select",
                "target": "semantic target; omit only when capability identifies one admissible action",
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
            return self._stop(episode_id, c_hash, f"{self.mode} policy has no model decision seam")
        if self.mode == POLICY_LLM and not self.catalog:
            return self._stop(episode_id, c_hash, "LLM policy has no capability catalog")

        request = self.request_payload(objective, state, candidates, history, budgets)
        try:
            response = self._decide(request)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:
            return self._stop(episode_id, c_hash, f"{self.mode} policy call failed: {type(exc).__name__}: {exc}")

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
        if disposition != "select":
            return self._stop(episode_id, c_hash, f"{self.mode} policy returned an invalid selection")
        index, error = self._resolve_selection(parsed, candidates)
        if index is None:
            return self._stop(episode_id, c_hash, error)

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

    def _resolve_selection(
        self,
        parsed: dict[str, Any],
        candidates: list[Any],
    ) -> tuple[int | None, str]:
        capability = _selection_key(parsed.get("capability"))
        target = _selection_key(parsed.get("target"))
        if not capability:
            return None, "LLM policy returned no semantic capability"
        catalog_names = {
            _selection_key(item.get("name"))
            for item in self.catalog
            if _selection_key(item.get("name"))
        }
        if capability not in catalog_names:
            return None, "LLM policy proposed a capability outside the catalog"
        matches = [
            index
            for index, candidate in enumerate(candidates)
            if _selection_key(getattr(candidate, "name", "")) == capability
            and (not target or _selection_key(getattr(candidate, "target", "")) == target)
        ]
        if len(matches) == 1:
            return matches[0], ""
        if not matches:
            return None, "LLM policy proposed a capability that is not currently admissible"
        return None, "LLM policy proposal is ambiguous without an exact target"

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


class HybridPolicy(LLMPolicy):
    """Model-mediated selection over the deterministic admissible frontier.

    The deterministic layer generates and constrains candidates. The model must still select the semantic
    capability; missing, failed, or invalid model output stops instead of falling back to symbolic priority.
    """

    mode = POLICY_HYBRID

    def request_payload(
        self,
        objective: str,
        state: Any,
        candidates: list[Any],
        history: list[PolicyDecision],
        budgets: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "objective": str(objective or ""),
            "normalized_state": _normalized_state_payload(state),
            "candidates": [
                {"index": index, **_candidate_payload(candidate)}
                for index, candidate in enumerate(candidates)
            ],
            "prior_decisions": _prior_decisions_payload(history),
            "budgets": dict(budgets or {}),
            "selection_contract": "admissible_frontier",
            "response_schema": {
                "disposition": "select|stop|ask",
                "candidate_index": "integer required for select",
                "rationale": "short string",
                "confidence": "number 0..1",
                "expected_evidence": "short string",
            },
        }

    def _resolve_selection(
        self,
        parsed: dict[str, Any],
        candidates: list[Any],
    ) -> tuple[int | None, str]:
        try:
            index = int(parsed.get("candidate_index"))
        except (TypeError, ValueError):
            return None, "hybrid policy returned no valid candidate index"
        if index < 0 or index >= len(candidates):
            return None, "hybrid policy returned an invalid frontier selection"
        return index, ""
