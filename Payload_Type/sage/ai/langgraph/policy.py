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


def capability_family(value: Any) -> str:
    """Return the strategic family for one semantic capability name.

    This is intentionally coarser than the execution capability name. The eval
    layer uses it to distinguish a real branch (for example GPO versus LAPS)
    from several targets in the same lane.
    """
    name = _selection_key(value)
    if name == "collect-graph":
        return "collection"
    if name in {"gpo-controlled-system-exec", "grant-directory-rights"}:
        return "gpo-directory"
    if name in {
        "dcsync-krbtgt",
        "dcsync-account",
        "forge-golden-ticket",
        "ensure-kerberos-context",
        "ensure-account-kerberos-context",
    }:
        return "replication-kerberos"
    if name in {
        "read-managed-local-admin-secret",
        "use-managed-local-admin-secret",
        "execute-as-local-admin",
        "endpoint-protection-adjustment",
    }:
        return "managed-local-admin"
    if name in {
        "adcs-ca-private-key-export",
        "adcs-esc-certificate-enroll",
        "adcs-certificate-auth",
    }:
        return "adcs"
    return name or "unknown"


def _response_text(value: Any) -> str:
    """Return a bounded, serializable copy of the raw model response content."""
    content = getattr(value, "content", value)
    if isinstance(content, list):
        content = "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    if isinstance(content, dict):
        try:
            content = json.dumps(content, sort_keys=True, separators=(",", ":"))
        except Exception:
            content = str(content)
    return str(content or "").strip()[:8000]


def _response_backend_provenance(value: Any) -> dict[str, Any]:
    """Extract only response-derived backend identifiers safe to persist.

    Configured provider/model labels describe intent, not what handled a
    request. LangChain response metadata is the closest available runtime
    evidence, so preserve only stable identifier fields from that response and
    leave the observed backend empty when the provider did not return one.
    """
    safe_metadata: dict[str, str] = {}
    sources = (
        ("response_metadata", getattr(value, "response_metadata", None)),
        ("additional_kwargs", getattr(value, "additional_kwargs", None)),
    )
    allowed_keys = (
        "model_name",
        "model_id",
        "model",
        "model_provider",
        "provider",
        "provider_name",
        "system_fingerprint",
        "request_id",
        "id",
        "finish_reason",
        "stop_reason",
    )
    value_sources: dict[str, str] = {}
    for source_name, mapping in sources:
        if not isinstance(mapping, dict):
            continue
        for key in allowed_keys:
            raw = mapping.get(key)
            if isinstance(raw, (str, int, float, bool)) and str(raw).strip():
                safe_metadata.setdefault(key, str(raw).strip())
                value_sources.setdefault(key, f"{source_name}.{key}")

    provider = next(
        (safe_metadata[key] for key in ("model_provider", "provider_name", "provider") if key in safe_metadata),
        "",
    )
    model_id = next(
        (safe_metadata[key] for key in ("model_name", "model_id", "model") if key in safe_metadata),
        "",
    )
    backend = f"{provider}:{model_id}" if provider and model_id else (model_id or provider)
    source_key = next(
        (key for key in ("model_name", "model_id", "model", "model_provider", "provider_name", "provider")
         if key in value_sources),
        "",
    )
    return {
        "effective_backend": backend,
        "effective_model_provider": provider,
        "effective_model_id": model_id,
        "backend_provenance_source": value_sources.get(source_key, "unavailable"),
        "response_metadata": safe_metadata,
    }


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
    candidate_count: int = 0
    selected_family: str = ""
    selected_is_first_admissible: bool | None = None
    raw_response: str = ""
    raw_disposition: str = ""
    raw_rationale: str = ""
    model_response_observed: bool = False
    effective_backend: str = ""
    effective_model_provider: str = ""
    effective_model_id: str = ""
    backend_provenance_source: str = ""
    response_metadata: dict[str, str] = field(default_factory=dict)
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
            candidate_count=len(candidates),
            selected_family=capability_family(getattr(selected, "name", "")) if selected is not None else "",
            selected_is_first_admissible=True if selected is not None else None,
            backend_provenance_source="symbolic",
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
        return {
            "objective": str(objective or ""),
            "normalized_state": _normalized_state_payload(state),
            "capability_catalog": [dict(item) for item in self.catalog],
            "current_admissible_actions": [
                _candidate_payload(candidate)
                for candidate in candidates
            ],
            "prior_decisions": _prior_decisions_payload(history),
            "budgets": dict(budgets or {}),
            "selection_contract": "semantic_catalog",
            "response_schema": {
                "disposition": "select|stop|ask",
                "capability": "catalog capability name required for select; must also appear in current_admissible_actions",
                "target": "semantic target from current_admissible_actions; omit only when capability identifies one admissible action",
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
            return self._stop(
                episode_id,
                c_hash,
                f"{self.mode} policy has no model decision seam",
                candidate_count=len(candidates),
            )
        if self.mode == POLICY_LLM and not self.catalog:
            return self._stop(
                episode_id,
                c_hash,
                "LLM policy has no capability catalog",
                candidate_count=len(candidates),
            )

        request = self.request_payload(objective, state, candidates, history, budgets)
        try:
            response = self._decide(request)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:
            return self._stop(
                episode_id,
                c_hash,
                f"{self.mode} policy call failed: {type(exc).__name__}: {exc}",
                candidate_count=len(candidates),
            )

        raw_response = _response_text(response)
        provenance = _response_backend_provenance(response)
        parsed = self._response_dict(response)
        disposition = str(parsed.get("disposition") or "").strip().casefold()
        rationale = str(parsed.get("rationale") or "").strip()
        model_response_observed = parsed.get("_policy_model_response_observed") is not False
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
                candidate_count=len(candidates),
                raw_response=raw_response,
                raw_disposition=disposition,
                raw_rationale=rationale,
                model_response_observed=model_response_observed,
                **provenance,
            )
        if disposition != "select":
            return self._stop(
                episode_id,
                c_hash,
                f"{self.mode} policy returned an invalid selection",
                candidate_count=len(candidates),
                raw_response=raw_response,
                raw_disposition=disposition,
                raw_rationale=rationale,
                model_response_observed=model_response_observed,
                provenance=provenance,
            )
        index, error = self._resolve_selection(parsed, candidates)
        if index is None:
            return self._stop(
                episode_id,
                c_hash,
                error,
                candidate_count=len(candidates),
                raw_response=raw_response,
                raw_disposition=disposition,
                raw_rationale=rationale,
                model_response_observed=model_response_observed,
                provenance=provenance,
            )

        selected = candidates[index]
        replay_decision = parsed.get("_policy_replay_decision")
        if isinstance(replay_decision, dict) and replay_decision:
            return self._replayed_decision(
                replay_decision,
                episode_id=episode_id,
                c_hash=c_hash,
                candidates=candidates,
                index=index,
            )
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
            candidate_count=len(candidates),
            selected_family=capability_family(getattr(selected, "name", "")),
            selected_is_first_admissible=index == 0,
            raw_response=raw_response,
            raw_disposition=disposition,
            raw_rationale=rationale,
            model_response_observed=model_response_observed,
            **provenance,
        )

    def _replayed_decision(
        self,
        replay: dict[str, Any],
        *,
        episode_id: str,
        c_hash: str,
        candidates: list[Any],
        index: int,
    ) -> PolicyDecision:
        """Revalidate an operator-approved move without inventing a second model decision."""
        selected = candidates[index]
        confidence = replay.get("confidence")
        try:
            confidence = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = None
        response_metadata = replay.get("response_metadata")
        if not isinstance(response_metadata, dict):
            response_metadata = {}
        return PolicyDecision(
            episode_id=episode_id,
            decision_id=str(replay.get("decision_id") or f"decision-{uuid4().hex}"),
            policy_mode=self.mode,
            candidate_hash=c_hash,
            disposition="select",
            selected_index=index,
            selected_capability=str(getattr(selected, "name", "") or ""),
            selected_target=str(getattr(selected, "target", "") or ""),
            rationale=str(replay.get("rationale") or "").strip(),
            confidence=confidence,
            expected_evidence=str(replay.get("expected_evidence") or "").strip(),
            model_provider=str(replay.get("model_provider") or self.provider),
            model_id=str(replay.get("model_id") or self.model_id),
            candidate_count=len(candidates),
            selected_family=capability_family(getattr(selected, "name", "")),
            selected_is_first_admissible=index == 0,
            raw_response=str(replay.get("raw_response") or ""),
            raw_disposition=str(replay.get("raw_disposition") or replay.get("disposition") or "select"),
            raw_rationale=str(replay.get("raw_rationale") or replay.get("rationale") or ""),
            model_response_observed=False,
            effective_backend=str(replay.get("effective_backend") or ""),
            effective_model_provider=str(replay.get("effective_model_provider") or ""),
            effective_model_id=str(replay.get("effective_model_id") or ""),
            backend_provenance_source=str(replay.get("backend_provenance_source") or ""),
            response_metadata={
                str(key): str(value)
                for key, value in response_metadata.items()
                if isinstance(value, (str, int, float, bool)) and str(value).strip()
            },
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

    def _stop(
        self,
        episode_id: str,
        c_hash: str,
        rationale: str,
        *,
        candidate_count: int = 0,
        raw_response: str = "",
        raw_disposition: str = "",
        raw_rationale: str = "",
        model_response_observed: bool = False,
        provenance: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        return PolicyDecision(
            episode_id=episode_id,
            decision_id=f"decision-{uuid4().hex}",
            policy_mode=self.mode,
            candidate_hash=c_hash,
            disposition="stop",
            rationale=rationale,
            model_provider=self.provider,
            model_id=self.model_id,
            candidate_count=candidate_count,
            raw_response=raw_response,
            raw_disposition=raw_disposition,
            raw_rationale=raw_rationale,
            model_response_observed=model_response_observed,
            **dict(provenance or {}),
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
