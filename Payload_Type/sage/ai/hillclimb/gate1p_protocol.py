"""Typed Gate 1P protocol selection; prose and effect prefixes grant no authority."""
from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class Gate1PProtocol:
    module: ModuleType

    @property
    def protocol_id(self) -> str: return str(self.module.PROTOCOL_ID)

    @property
    def matrix_id(self) -> str: return str(getattr(self.module, "MATRIX_ID", "gate1p-stagea-child-domain-control-matrix-v1"))

    @property
    def pair_id(self) -> str | None:
        value = self.module.PAIR_ID
        return str(value) if value is not None else None

    @property
    def pair_allocated(self) -> bool: return self.module.PAIR_ALLOCATED is True

    @property
    def scenario(self) -> str: return str(self.module.SCENARIO)

    @property
    def objective(self) -> str: return str(self.module.OBJECTIVE)

    @property
    def arms(self) -> tuple[str, ...]: return tuple(self.module.ARMS)

    @property
    def proof_classes(self) -> tuple[str, ...]: return tuple(self.module.OBJECTIVE_PROOF_CLASSES)

    @property
    def storage_namespace(self) -> str: return str(getattr(self.module, "STORAGE_NAMESPACE", f"gate1p-stagea/{self.pair_id}"))

    def validate_request(self, *, require_allocation: bool, **request: Any) -> str:
        arm = self.module.validate_request(**request)
        if require_allocation and not self.pair_allocated:
            raise self.module.ProtocolError(
                f"protocol {self.protocol_id!r} is prospective and has no allocated live execution identity"
            )
        return arm

    def build_objective_oracle(self, *args: Any, **kwargs: Any):
        factory = getattr(self.module, "build_objective_oracle", None) or self.module.StageAObjectiveOracle
        return factory(*args, **kwargs)

    def build_episode_observer(self, *args: Any, **kwargs: Any):
        factory = getattr(self.module, "build_episode_observer", None) or self.module.LiveEpisodeObserver
        return factory(*args, **kwargs)

    def observer_facts_disposition(self, observer: Mapping[str, Any], *, arm: str) -> dict[str, Any]:
        return self.module.observer_facts_disposition(observer, arm=arm)

    def episode_outcome_disposition(self, observer: Mapping[str, Any], *, arm: str,
                                    total_tokens: object, token_ceiling: object) -> dict[str, Any]:
        return self.module.episode_outcome_disposition(
            observer, arm=arm, total_tokens=total_tokens, token_ceiling=token_ceiling,
        )

    def comparison_disposition(self, records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        return self.module.pair_disposition(records)

    def assembled_contract(self) -> dict[str, Any]:
        return {
            "protocol_id": self.protocol_id, "matrix_id": self.matrix_id,
            "pair_id": self.pair_id, "pair_allocated": self.pair_allocated,
            "scenario": self.scenario, "objective": self.objective,
            "arms": list(self.arms),
            "proof_classes": list(self.proof_classes),
            "storage_namespace": self.storage_namespace,
            "observer_version": str(self.module.OBSERVER_VERSION),
            "comparator": "pair_disposition",
        }


def _protocols() -> tuple[Gate1PProtocol, ...]:
    try:
        from . import gate1p_stagea, gate1p_stageb
    except Exception:
        import gate1p_stagea  # type: ignore
        import gate1p_stageb  # type: ignore

    return Gate1PProtocol(gate1p_stagea), Gate1PProtocol(gate1p_stageb)


def primary_scenarios() -> frozenset[str]:
    return frozenset(protocol.scenario for protocol in _protocols())


def protocol_for_scenario(scenario: str) -> Gate1PProtocol:
    matches = [protocol for protocol in _protocols() if protocol.scenario == scenario]
    if len(matches) != 1:
        error_type = _protocols()[0].module.ProtocolError
        raise error_type(f"no unique Gate 1P protocol is declared for scenario {scenario!r}")
    return matches[0]


def protocol_for_request(*, protocol_id: str | None, scenario: str) -> Gate1PProtocol:
    if protocol_id is None:
        return protocol_for_scenario(scenario)
    by_id = [protocol for protocol in _protocols() if protocol.protocol_id == protocol_id]
    error_type = _protocols()[0].module.ProtocolError
    if len(by_id) != 1:
        raise error_type(f"unknown Gate 1P protocol_id {protocol_id!r}")
    protocol = by_id[0]
    if protocol.scenario != scenario:
        raise error_type(
            f"protocol {protocol.protocol_id!r} requires scenario {protocol.scenario!r}, not {scenario!r}"
        )
    return protocol
