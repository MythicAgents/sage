"""Pure branch replay labels for development measurement."""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Mapping, Sequence

from .tradecraft_evidence import canonical_sha256


VERSION = "m3-branch-label-map-v1"
_OBSERVATION_FIELDS = {
    "episode_id", "branch_key", "safe_success", "opsec", "source_sha256", "scorer_sha256"
}


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _digest(value: Any, name: str) -> str:
    value = _text(value, name)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a string list")
    return sorted(value)


def branch_key(
    family_id: str,
    packet: Mapping[str, Any],
    selected_capability: str,
    selected_target: str,
) -> str:
    """Hash exact action semantics and only the relevant visible prior state."""
    family = _text(family_id, "family_id")
    capability = _text(selected_capability, "selected_capability")
    target = _text(selected_target, "selected_target")
    if not isinstance(packet, Mapping):
        raise ValueError("packet must be a mapping")
    objective = _text(packet.get("objective"), "packet.objective")
    frontier = packet.get("admissible_frontier")
    normalized = packet.get("normalized_state")
    if not isinstance(frontier, list) or not isinstance(normalized, Mapping):
        raise ValueError("packet requires admissible_frontier and normalized_state")
    matches = [
        item for item in frontier
        if isinstance(item, Mapping) and item.get("name") == capability and item.get("target") == target
    ]
    if len(matches) != 1:
        raise ValueError("selected capability and target must match exactly one admissible frontier item")
    item = matches[0]
    if not all(name in normalized for name in ("achieved_effects", "graph_facts", "footholds")):
        raise ValueError("normalized_state omits required branch context")
    footholds = normalized["footholds"]
    if not isinstance(footholds, list):
        raise ValueError("normalized_state.footholds must be a list")
    foothold_state = []
    for foothold in footholds:
        if not isinstance(foothold, Mapping) or any(
            name not in foothold or not isinstance(foothold[name], str)
            for name in ("host", "identity", "integrity")
        ):
            raise ValueError("each foothold requires string host, identity, and integrity")
        foothold_state.append((foothold["host"], foothold["identity"], foothold["integrity"]))
    material = {
        "family_id": family,
        "objective_digest": canonical_sha256({"objective": objective}),
        "capability": capability,
        "target": target,
        "preconditions": _string_list(item.get("preconditions"), "frontier.preconditions"),
        "effects": _string_list(item.get("effects"), "frontier.effects"),
        "achieved_effects": _string_list(normalized["achieved_effects"], "normalized_state.achieved_effects"),
        "graph_facts": _string_list(normalized["graph_facts"], "normalized_state.graph_facts"),
        "footholds": sorted(foothold_state),
    }
    return canonical_sha256(material)


def build_label_map(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Reduce separately admitted outcomes without authenticating or admitting them."""
    if not isinstance(observations, (list, tuple)) or not observations:
        raise ValueError("observations must be a non-empty sequence")
    retained, grouped, seen = [], defaultdict(list), set()
    for raw in observations:
        if not isinstance(raw, Mapping) or set(raw) != _OBSERVATION_FIELDS:
            raise ValueError("observation fields differ from schema")
        row = dict(raw)
        row["episode_id"] = _text(row["episode_id"], "episode_id")
        row["branch_key"] = _text(row["branch_key"], "branch_key")
        row["source_sha256"] = _digest(row["source_sha256"], "source_sha256")
        row["scorer_sha256"] = _digest(row["scorer_sha256"], "scorer_sha256")
        for field in ("safe_success", "opsec"):
            if row[field] is not None and type(row[field]) is not bool:
                raise ValueError(f"{field} must be Boolean or null")
        identity = (row["branch_key"], row["episode_id"])
        if identity in seen:
            raise ValueError("an episode cannot count twice for the same branch key")
        seen.add(identity)
        row["observation_hash"] = canonical_sha256(row)
        retained.append(row)
        grouped[row["branch_key"]].append(row)

    labels = {}
    for key, rows in sorted(grouped.items()):
        reasons = []
        if len(rows) < 2:
            reasons.append("insufficient_independent_episodes")
        values = {}
        for field in ("safe_success", "opsec"):
            field_values = [row[field] for row in rows]
            if any(value is None for value in field_values):
                reasons.append(f"{field}_unknown")
                values[field] = None
            elif len(set(field_values)) != 1:
                reasons.append(f"{field}_conflict")
                values[field] = None
            else:
                values[field] = field_values[0]
        if len(rows) < 2:
            values = {"safe_success": None, "opsec": None}
        labels[key] = {
            **values,
            "independent_episodes": len(rows),
            "reason_codes": reasons,
            "observation_hashes": [row["observation_hash"] for row in rows],
        }
    payload = {
        "schema": VERSION,
        "labels": labels,
        "observations": retained,
        "source_admission_required": True,
        "live_authorized": False,
    }
    return {**payload, "label_map_hash": canonical_sha256(payload)}


def score_branch(label_map: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Return a validated known label or an explicit unknown."""
    if not isinstance(label_map, Mapping) or set(label_map) != {
        "schema", "labels", "observations", "source_admission_required", "live_authorized", "label_map_hash"
    }:
        raise ValueError("label map fields differ from schema")
    payload = {name: label_map[name] for name in label_map if name != "label_map_hash"}
    if label_map["schema"] != VERSION or label_map["label_map_hash"] != canonical_sha256(payload):
        raise ValueError("label map hash is invalid")
    key = _text(key, "branch_key")
    label = label_map["labels"].get(key)
    if not isinstance(label, Mapping):
        return {"safe_success": None, "opsec": None}
    safe, opsec = label.get("safe_success"), label.get("opsec")
    if type(safe) is not bool or type(opsec) is not bool:
        return {"safe_success": None, "opsec": None}
    return {"safe_success": safe, "opsec": opsec}


def compare_labels(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    practical_delta: float = 0.1,
    noninferiority_margin: float = 0.1,
) -> dict[str, Any]:
    """Compare two labels without supplying decision authority."""
    for value, name in ((practical_delta, "practical_delta"), (noninferiority_margin, "noninferiority_margin")):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 1:
            raise ValueError(f"{name} must be finite and strictly between zero and one")
    fields = {"safe_success", "opsec"}
    if not isinstance(left, Mapping) or not isinstance(right, Mapping) or set(left) != fields or set(right) != fields:
        raise ValueError("labels require exactly safe_success and opsec")
    values = (left["safe_success"], right["safe_success"], left["opsec"], right["opsec"])
    if any(value is not None and type(value) is not bool for value in values):
        raise ValueError("label values must be Boolean or null")
    safe_delta = None if any(value is None for value in values[:2]) else int(values[0]) - int(values[1])
    opsec_delta = None if any(value is None for value in values[2:]) else int(values[2]) - int(values[3])
    if safe_delta is None or opsec_delta is None:
        safe_delta = opsec_delta = None
        decision = "inconclusive"
    elif safe_delta > practical_delta and opsec_delta >= -noninferiority_margin:
        decision = "promote"
    elif safe_delta < -practical_delta or opsec_delta < -noninferiority_margin:
        decision = "reject"
    else:
        decision = "inconclusive"
    return {
        "gauge_decision": decision,
        "safe_success_delta": safe_delta,
        "opsec_delta": opsec_delta,
        "decision_authorized": False,
    }
