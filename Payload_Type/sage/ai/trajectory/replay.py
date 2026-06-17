"""Offline replay scoring for trajectory repair policies."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable

from .schema import TransitionRecord


@dataclass(frozen=True)
class RepairDecision:
    repair_kind: str
    score: float
    evidence_count: int
    label: str


@dataclass(frozen=True)
class ReplayResult:
    total: int
    exact_repair_matches: int
    label_matches: int

    @property
    def exact_repair_rate(self) -> float:
        return self.exact_repair_matches / self.total if self.total else 0.0

    @property
    def label_match_rate(self) -> float:
        return self.label_matches / self.total if self.total else 0.0


class FrequencyRepairPolicy:
    """Simple retrieval/ranking baseline keyed by normalized failure label."""

    def __init__(self, records: Iterable[TransitionRecord]):
        self._by_label: dict[str, Counter[str]] = defaultdict(Counter)
        for record in records:
            if not record.failure_label or not record.repair:
                continue
            self._by_label[record.failure_label][record.repair.kind] += 1

    def choose(self, record: TransitionRecord) -> RepairDecision | None:
        counter = self._by_label.get(record.failure_label)
        if not counter:
            return None
        repair, count = counter.most_common(1)[0]
        total = sum(counter.values())
        return RepairDecision(
            repair_kind=repair,
            score=count / total if total else 0.0,
            evidence_count=count,
            label=record.failure_label,
        )


def replay_score(train: Iterable[TransitionRecord], eval_records: Iterable[TransitionRecord]) -> ReplayResult:
    policy = FrequencyRepairPolicy(train)
    total = 0
    exact = 0
    label_matches = 0
    for record in eval_records:
        if not record.failure_label or not record.repair:
            continue
        total += 1
        decision = policy.choose(record)
        if decision is None:
            continue
        if decision.label == record.failure_label:
            label_matches += 1
        if decision.repair_kind == record.repair.kind:
            exact += 1
    return ReplayResult(total=total, exact_repair_matches=exact, label_matches=label_matches)
