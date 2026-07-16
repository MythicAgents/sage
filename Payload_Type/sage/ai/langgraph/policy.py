"""Explicit semantic policy backends for Sage's deterministic execution kernel."""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4


POLICY_LLM = "llm"
POLICY_SYMBOLIC = "symbolic"
POLICY_HYBRID = "hybrid"
POLICY_DEFAULT = POLICY_HYBRID
POLICY_MODES = frozenset((POLICY_LLM, POLICY_SYMBOLIC, POLICY_HYBRID))
POLICY_VERSION_LLM = "llm-v1"
POLICY_VERSION_SYMBOLIC = "symbolic-v1"
POLICY_VERSION_HYBRID = "hybrid-full-frontier-v2"
SELECTION_CONTRACT_LLM = "semantic_catalog"
SELECTION_CONTRACT_SYMBOLIC = "symbolic_frontier"
SELECTION_CONTRACT_HYBRID = POLICY_VERSION_HYBRID
_MAX_OPERATIONAL_WAIT_SECONDS = 600
_CAPTURE_DECISION_PACKETS_ENV = "SAGE_EVAL_CAPTURE_POLICY_DECISION_PACKETS"


def resolve_policy_mode(value: Any, default: str = POLICY_DEFAULT) -> tuple[str, str]:
    mode = str(value or "").strip().casefold()
    fallback = str(default or "").strip().casefold()
    if fallback not in POLICY_MODES:
        fallback = POLICY_DEFAULT
    if mode in POLICY_MODES:
        return mode, "explicit_valid"
    if not mode:
        return fallback, "default_missing"
    return fallback, "default_invalid"


def normalize_policy_mode(value: Any, default: str = POLICY_DEFAULT) -> str:
    return resolve_policy_mode(value, default=default)[0]


def new_episode_id() -> str:
    return f"episode-{uuid4().hex}"


def _default_operational_cost() -> dict[str, Any]:
    return {
        "interaction_class": "direct",
        "execution_scope": "direct",
        "requires_propagation_wait": False,
        "expected_wait_seconds": 0,
        "wait_reasons": [],
    }


def _candidate_operational_cost(candidate: Any) -> dict[str, Any]:
    raw = getattr(candidate, "operational_cost", None)
    if not isinstance(raw, dict):
        return _default_operational_cost()
    raw_reasons = raw.get("wait_reasons")
    if isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    elif not isinstance(raw_reasons, (list, tuple, set)):
        raw_reasons = []
    try:
        wait_seconds = int(raw.get("expected_wait_seconds", 0) or 0)
    except (TypeError, ValueError):
        wait_seconds = 0
    wait_seconds = max(0, min(wait_seconds, _MAX_OPERATIONAL_WAIT_SECONDS))
    requires_wait = raw.get("requires_propagation_wait")
    if isinstance(requires_wait, str):
        requires_wait = requires_wait.strip().casefold() in {"1", "true", "yes", "on"}
    else:
        requires_wait = bool(requires_wait)
    requires_wait = requires_wait or wait_seconds > 0
    return {
        "interaction_class": str(
            raw.get("interaction_class") or ("propagation-bound" if requires_wait else "direct")
        ),
        "execution_scope": str(
            raw.get("execution_scope") or ("domain-policy" if requires_wait else "direct")
        ),
        "requires_propagation_wait": requires_wait,
        "expected_wait_seconds": wait_seconds,
        "wait_reasons": [str(reason) for reason in raw_reasons if str(reason)],
    }


def _candidate_payload(candidate: Any) -> dict[str, Any]:
    return {
        "name": str(getattr(candidate, "name", "") or ""),
        "target": str(getattr(candidate, "target", "") or ""),
        "preconditions": [str(v) for v in (getattr(candidate, "preconditions", None) or [])],
        "effects": [str(v) for v in (getattr(candidate, "effects", None) or [])],
        "operational_cost": _candidate_operational_cost(candidate),
        "reason": str(getattr(candidate, "reason", "") or ""),
    }


def _semantic_candidate_payload(candidate: Any) -> dict[str, Any]:
    """Return the stable action identity payload used by Phase 1 policy evidence.

    `reason` is intentionally excluded: it is explanatory text, not part of the
    selected action semantics. Preconditions and effects are sets semantically, so
    identity must not change when their source order changes.
    """
    return {
        "name": str(getattr(candidate, "name", "") or ""),
        "target": str(getattr(candidate, "target", "") or ""),
        "preconditions": sorted(str(v) for v in (getattr(candidate, "preconditions", None) or [])),
        "effects": sorted(str(v) for v in (getattr(candidate, "effects", None) or [])),
        "operational_cost": _candidate_operational_cost(candidate),
    }


def _sha256_json(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def semantic_candidate_id(candidate: Any) -> str:
    return f"candidate:sha256:{_sha256_json(_semantic_candidate_payload(candidate))}"


def candidate_set_hash(candidates: list[Any]) -> str:
    return f"sha256:{_sha256_json(sorted(semantic_candidate_id(candidate) for candidate in candidates))}"


def ordered_frontier_hash(candidates: list[Any]) -> str:
    return f"sha256:{_sha256_json([semantic_candidate_id(candidate) for candidate in candidates])}"


def selection_contract_hash(selection_contract: str) -> str:
    return f"sha256:{_sha256_json({'selection_contract': str(selection_contract or '')})}"


def candidate_hash(candidates: list[Any]) -> str:
    """Legacy ordered payload hash retained for v1 replay packet compatibility."""
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


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().casefold() in {"1", "true", "yes", "on"}


def _transport_stable_json_value(value: Any) -> Any:
    """Normalize values whose JSON spelling can drift across the native chat transport."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return value
    if isinstance(value, dict):
        return {
            str(key): _transport_stable_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_transport_stable_json_value(item) for item in value]
    return value


def _decision_packet(
    *,
    objective: str,
    state: Any,
    candidates: list[Any],
    history: list["PolicyDecision"],
    budgets: dict[str, Any] | None,
    selection_contract: str,
) -> dict[str, Any]:
    """Return the common replay packet shared by every policy mode.

    This is intentionally policy-contract-neutral. Learned modes may ask the model to
    emit a semantic capability or a candidate index, but offline replay needs the same
    normalized state and admissible frontier regardless of that output contract.
    """
    return _transport_stable_json_value({
        "schema_version": 1,
        "objective": str(objective or ""),
        "normalized_state": _normalized_state_payload(state),
        "admissible_frontier": [_candidate_payload(candidate) for candidate in candidates],
        "semantic_candidate_ids": [semantic_candidate_id(candidate) for candidate in candidates],
        "prior_decisions": _prior_decisions_payload(history),
        "budgets": dict(budgets or {}),
        "policy_version": {
            SELECTION_CONTRACT_SYMBOLIC: POLICY_VERSION_SYMBOLIC,
            SELECTION_CONTRACT_LLM: POLICY_VERSION_LLM,
            SELECTION_CONTRACT_HYBRID: POLICY_VERSION_HYBRID,
        }.get(str(selection_contract or ""), ""),
        "selection_contract": str(selection_contract or ""),
        "selection_contract_hash": selection_contract_hash(selection_contract),
        "candidate_hash": candidate_hash(candidates),
        "candidate_set_hash": candidate_set_hash(candidates),
        "ordered_frontier_hash": ordered_frontier_hash(candidates),
    })


def _captured_decision_packet(
    *,
    objective: str,
    state: Any,
    candidates: list[Any],
    history: list["PolicyDecision"],
    budgets: dict[str, Any] | None,
    selection_contract: str,
) -> tuple[dict[str, Any], str]:
    if not _env_truthy(_CAPTURE_DECISION_PACKETS_ENV):
        return {}, ""
    packet = _decision_packet(
        objective=objective,
        state=state,
        candidates=candidates,
        history=history,
        budgets=budgets,
        selection_contract=selection_contract,
    )
    raw = json.dumps(packet, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return packet, f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


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


def _policy_version(mode: str) -> str:
    return {
        POLICY_SYMBOLIC: POLICY_VERSION_SYMBOLIC,
        POLICY_LLM: POLICY_VERSION_LLM,
        POLICY_HYBRID: POLICY_VERSION_HYBRID,
    }.get(str(mode or ""), "")


def _forced_intervention_metadata(candidate: Any | None) -> dict[str, Any]:
    intent = getattr(candidate, "intent", {}) if candidate is not None else {}
    intent = intent if isinstance(intent, dict) else {}
    value = intent.get("eval_intervention")
    value = value if isinstance(value, dict) else {}
    if value.get("forced") is not True or value.get("credit_policy_win") is not False:
        return {}
    exact_target = str(value.get("exact_target") or "")
    if not exact_target or exact_target != str(getattr(candidate, "target", "") or ""):
        return {}
    return {
        "forced_intervention": True,
        "intervention_id": str(value.get("intervention_id") or ""),
        "forced_policy_win_credit": False,
    }


def _decision_identity_fields(
    *,
    mode: str,
    selection_contract: str,
    candidates: list[Any],
    selected_index: int | None,
    decision_owner: str,
) -> dict[str, Any]:
    ids = [semantic_candidate_id(candidate) for candidate in candidates]
    selected_id = ids[selected_index] if isinstance(selected_index, int) and 0 <= selected_index < len(ids) else ""
    selected_candidate = candidates[selected_index] if isinstance(selected_index, int) and 0 <= selected_index < len(candidates) else None
    intervention = _forced_intervention_metadata(selected_candidate)
    if intervention:
        decision_owner = "forced_intervention"
    branch_opportunities = 1 if len(candidates) > 1 else 0
    model_owned = 1 if decision_owner == "model_branch" else 0
    return {
        "policy_version": _policy_version(mode),
        "selection_contract": str(selection_contract or ""),
        "selection_contract_hash": selection_contract_hash(selection_contract),
        "decision_owner": str(decision_owner or ""),
        # `actions_from_state()` is the current safe/admissible policy seam. Until Phase 2 adds
        # rejection-reason instrumentation, raw and admissible counts are identical at this seam.
        "raw_candidate_count": len(candidates),
        "admissible_candidate_count": len(candidates),
        "semantic_candidate_ids": ids,
        "candidate_set_hash": candidate_set_hash(candidates),
        "ordered_frontier_hash": ordered_frontier_hash(candidates),
        "selected_candidate_id": selected_id,
        "symbolic_counterfactual_candidate_id": ids[0] if ids else "",
        "branch_opportunity_count": branch_opportunities,
        "model_owned_decision_count": model_owned,
        "kernel_singleton_count": 1 if decision_owner == "kernel_singleton" else 0,
        "model_branch_coverage": (model_owned / branch_opportunities) if branch_opportunities else 0.0,
        "causally_decisive_decision_count": 0,
        "forced_intervention": bool(intervention),
        "intervention_id": str(intervention.get("intervention_id") or ""),
        "forced_policy_win_credit": intervention.get("forced_policy_win_credit"),
    }


def _request_schema_hash(request: dict[str, Any]) -> str:
    return f"sha256:{_sha256_json({
        'selection_contract': str(request.get('selection_contract') or ''),
        'response_schema': request.get('response_schema') if isinstance(request.get('response_schema'), dict) else {},
    })}"


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
    decision_packet: dict[str, Any] = field(default_factory=dict)
    decision_packet_hash: str = ""
    policy_version: str = ""
    selection_contract: str = ""
    selection_contract_hash: str = ""
    decision_owner: str = ""
    raw_candidate_count: int = 0
    admissible_candidate_count: int = 0
    semantic_candidate_ids: list[str] = field(default_factory=list)
    candidate_set_hash: str = ""
    ordered_frontier_hash: str = ""
    selected_candidate_id: str = ""
    symbolic_counterfactual_candidate_id: str = ""
    branch_opportunity_count: int = 0
    model_owned_decision_count: int = 0
    kernel_singleton_count: int = 0
    model_branch_coverage: float = 0.0
    causally_decisive_decision_count: int = 0
    forced_intervention: bool = False
    intervention_id: str = ""
    forced_policy_win_credit: bool | None = None
    request_schema_hash: str = ""
    prompt_hash: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SymbolicPolicy:
    """Behavior-preserving baseline: choose the first admissible candidate."""

    mode = POLICY_SYMBOLIC
    selection_contract = SELECTION_CONTRACT_SYMBOLIC

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
        decision_packet, decision_packet_hash = _captured_decision_packet(
            objective=objective,
            state=state,
            candidates=candidates,
            history=history,
            budgets=budgets,
            selection_contract=self.selection_contract,
        )
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
            decision_packet=decision_packet,
            decision_packet_hash=decision_packet_hash,
            **_decision_identity_fields(
                mode=self.mode,
                selection_contract=self.selection_contract,
                candidates=candidates,
                selected_index=0 if selected is not None else None,
                decision_owner="symbolic_control",
            ),
        )


class LLMPolicy:
    """Model-mediated capability selector with no symbolic fallback."""

    mode = POLICY_LLM
    selection_contract = SELECTION_CONTRACT_LLM

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
            "policy_version": _policy_version(self.mode),
            "selection_contract": self.selection_contract,
            "selection_contract_hash": selection_contract_hash(self.selection_contract),
            "semantic_candidate_ids": [semantic_candidate_id(candidate) for candidate in candidates],
            "candidate_set_hash": candidate_set_hash(candidates),
            "ordered_frontier_hash": ordered_frontier_hash(candidates),
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
        decision_owner = "model_branch" if candidates else ""
        decision_packet, decision_packet_hash = _captured_decision_packet(
            objective=objective,
            state=state,
            candidates=candidates,
            history=history,
            budgets=budgets,
            selection_contract=self.selection_contract,
        )
        if not candidates:
            return self._stop(
                episode_id,
                c_hash,
                "no admissible candidates",
                candidates=candidates,
                decision_owner=decision_owner,
                decision_packet=decision_packet,
                decision_packet_hash=decision_packet_hash,
            )
        if self._decide is None:
            return self._stop(
                episode_id,
                c_hash,
                f"{self.mode} policy has no model decision seam",
                candidate_count=len(candidates),
                candidates=candidates,
                decision_owner=decision_owner,
                decision_packet=decision_packet,
                decision_packet_hash=decision_packet_hash,
            )
        if self.mode == POLICY_LLM and not self.catalog:
            return self._stop(
                episode_id,
                c_hash,
                "LLM policy has no capability catalog",
                candidate_count=len(candidates),
                candidates=candidates,
                decision_owner=decision_owner,
                decision_packet=decision_packet,
                decision_packet_hash=decision_packet_hash,
            )

        request = self.request_payload(objective, state, candidates, history, budgets)
        request_schema_hash = _request_schema_hash(request)
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
                candidates=candidates,
                decision_owner=decision_owner,
                request_schema_hash=request_schema_hash,
                decision_packet=decision_packet,
                decision_packet_hash=decision_packet_hash,
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
                decision_packet=decision_packet,
                decision_packet_hash=decision_packet_hash,
                request_schema_hash=request_schema_hash,
                **_decision_identity_fields(
                    mode=self.mode,
                    selection_contract=self.selection_contract,
                    candidates=candidates,
                    selected_index=None,
                    decision_owner=decision_owner,
                ),
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
                candidates=candidates,
                decision_owner=decision_owner,
                request_schema_hash=request_schema_hash,
                decision_packet=decision_packet,
                decision_packet_hash=decision_packet_hash,
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
                candidates=candidates,
                decision_owner=decision_owner,
                request_schema_hash=request_schema_hash,
                decision_packet=decision_packet,
                decision_packet_hash=decision_packet_hash,
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
                decision_owner=decision_owner,
                request_schema_hash=request_schema_hash,
                decision_packet=decision_packet,
                decision_packet_hash=decision_packet_hash,
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
            decision_packet=decision_packet,
            decision_packet_hash=decision_packet_hash,
            request_schema_hash=request_schema_hash,
            **_decision_identity_fields(
                mode=self.mode,
                selection_contract=self.selection_contract,
                candidates=candidates,
                selected_index=index,
                decision_owner=decision_owner,
            ),
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
        decision_owner: str,
        request_schema_hash: str,
        decision_packet: dict[str, Any],
        decision_packet_hash: str,
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
            decision_packet=decision_packet,
            decision_packet_hash=decision_packet_hash,
            request_schema_hash=request_schema_hash,
            response_metadata={
                str(key): str(value)
                for key, value in response_metadata.items()
                if isinstance(value, (str, int, float, bool)) and str(value).strip()
            },
            **_decision_identity_fields(
                mode=self.mode,
                selection_contract=self.selection_contract,
                candidates=candidates,
                selected_index=index,
                decision_owner=decision_owner,
            ),
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
        candidates: list[Any] | None = None,
        decision_owner: str = "",
        request_schema_hash: str = "",
        decision_packet: dict[str, Any] | None = None,
        decision_packet_hash: str = "",
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
            decision_packet=dict(decision_packet or {}),
            decision_packet_hash=str(decision_packet_hash or ""),
            request_schema_hash=str(request_schema_hash or ""),
            **_decision_identity_fields(
                mode=self.mode,
                selection_contract=self.selection_contract,
                candidates=list(candidates or []),
                selected_index=None,
                decision_owner=decision_owner,
            ),
            **dict(provenance or {}),
        )


class HybridPolicy(LLMPolicy):
    """Model-mediated selection over the deterministic admissible frontier.

    The deterministic layer generates and constrains candidates. The model must still select the semantic
    capability; missing, failed, or invalid model output stops instead of falling back to symbolic priority.
    """

    mode = POLICY_HYBRID
    selection_contract = SELECTION_CONTRACT_HYBRID

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
        if len(candidates) != 1:
            return await super().select(
                episode_id=episode_id,
                objective=objective,
                state=state,
                candidates=candidates,
                history=history,
                budgets=budgets,
            )
        decision_packet, decision_packet_hash = _captured_decision_packet(
            objective=objective,
            state=state,
            candidates=candidates,
            history=history,
            budgets=budgets,
            selection_contract=self.selection_contract,
        )
        selected = candidates[0]
        return PolicyDecision(
            episode_id=episode_id,
            decision_id=f"decision-{uuid4().hex}",
            policy_mode=self.mode,
            candidate_hash=candidate_hash(candidates),
            disposition="select",
            selected_index=0,
            selected_capability=str(getattr(selected, "name", "") or ""),
            selected_target=str(getattr(selected, "target", "") or ""),
            rationale="only admissible candidate; kernel-owned singleton selection",
            candidate_count=1,
            selected_family=capability_family(getattr(selected, "name", "")),
            selected_is_first_admissible=True,
            model_provider=self.provider,
            model_id=self.model_id,
            model_response_observed=False,
            backend_provenance_source="kernel_singleton",
            decision_packet=decision_packet,
            decision_packet_hash=decision_packet_hash,
            **_decision_identity_fields(
                mode=self.mode,
                selection_contract=self.selection_contract,
                candidates=candidates,
                selected_index=0,
                decision_owner="kernel_singleton",
            ),
        )

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
                {"candidate_id": semantic_candidate_id(candidate), **_candidate_payload(candidate)}
                for candidate in candidates
            ],
            "prior_decisions": _prior_decisions_payload(history),
            "budgets": dict(budgets or {}),
            "policy_version": _policy_version(self.mode),
            "selection_contract": self.selection_contract,
            "selection_contract_hash": selection_contract_hash(self.selection_contract),
            "semantic_candidate_ids": [semantic_candidate_id(candidate) for candidate in candidates],
            "candidate_set_hash": candidate_set_hash(candidates),
            "ordered_frontier_hash": ordered_frontier_hash(candidates),
            "response_schema": {
                "disposition": "select|stop|ask",
                "candidate_id": "stable semantic candidate ID required for select",
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
        candidate_id = str(parsed.get("candidate_id") or "").strip()
        if not candidate_id:
            return None, "hybrid policy returned no valid candidate ID"
        matches = [
            index
            for index, candidate in enumerate(candidates)
            if semantic_candidate_id(candidate) == candidate_id
        ]
        if len(matches) == 1:
            return matches[0], ""
        return None, "hybrid policy returned an invalid frontier selection"
