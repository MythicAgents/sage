from copy import deepcopy

import pytest

from ai.hillclimb import branch_replay as replay


def _packet(target="prod"):
    return {
        "objective": "bound objective",
        "admissible_frontier": [
            {"name": "execute", "target": "prod", "preconditions": ["b", "a"], "effects": ["y", "x"]},
            {"name": "execute", "target": "prod-copy", "preconditions": [], "effects": []},
        ],
        "normalized_state": {
            "achieved_effects": ["effect:b", "effect:a"],
            "graph_facts": ["fact:b", "fact:a"],
            "footholds": [
                {"callback_id": "7", "host": "h", "identity": "i", "integrity": "high", "timestamp": "one"},
            ],
        },
        "timestamp": "packet-one",
    }


def _observations(key, left=True, opsec=True):
    return [
        {"episode_id": f"episode-{index}", "branch_key": key, "safe_success": left, "opsec": opsec,
         "source_sha256": "a" * 64, "scorer_sha256": "b" * 64}
        for index in range(2)
    ]


def test_branch_key_requires_exact_target_and_hashes_only_relevant_state():
    packet = _packet()
    key = replay.branch_key("family", packet, "execute", "prod")
    near = replay.branch_key("family", packet, "execute", "prod-copy")
    assert key != near
    callback_change = deepcopy(packet)
    callback_change["normalized_state"]["footholds"][0]["callback_id"] = "99"
    callback_change["normalized_state"]["footholds"][0]["timestamp"] = "two"
    callback_change["timestamp"] = "packet-two"
    assert replay.branch_key("family", callback_change, "execute", "prod") == key
    state_change = deepcopy(packet)
    state_change["normalized_state"]["graph_facts"].append("fact:c")
    assert replay.branch_key("family", state_change, "execute", "prod") != key
    with pytest.raises(ValueError, match="exactly one"):
        replay.branch_key("family", packet, "execute", "pro")
    with pytest.raises(ValueError, match="exactly one"):
        replay.branch_key("family", packet, "execute", " prod ")
    duplicate = deepcopy(packet)
    duplicate["admissible_frontier"].append(deepcopy(packet["admissible_frontier"][0]))
    with pytest.raises(ValueError, match="exactly one"):
        replay.branch_key("family", duplicate, "execute", "prod")


def test_label_map_requires_two_unanimous_independent_episodes_and_retains_sources():
    key = replay.branch_key("family", _packet(), "execute", "prod")
    label_map = replay.build_label_map(_observations(key))
    assert label_map["source_admission_required"] is True
    assert label_map["live_authorized"] is False
    assert len(label_map["observations"]) == 2
    assert all(row["observation_hash"].startswith("sha256:") for row in label_map["observations"])
    assert replay.score_branch(label_map, key) == {"safe_success": True, "opsec": True}
    assert replay.score_branch(label_map, "sha256:" + "f" * 64) == {"safe_success": None, "opsec": None}


def test_label_map_rejects_duplicates_and_invalid_observations_and_marks_unknowns():
    key = replay.branch_key("family", _packet(), "execute", "prod")
    with pytest.raises(ValueError, match="twice"):
        replay.build_label_map(_observations(key)[:1] * 2)
    for mutation in (
        {"safe_success": 1}, {"source_sha256": "bad"}, {"scorer_sha256": "g" * 64},
    ):
        rows = _observations(key)
        rows[0].update(mutation)
        with pytest.raises(ValueError):
            replay.build_label_map(rows)

    missing = replay.build_label_map(_observations(key)[:1])
    assert replay.score_branch(missing, key) == {"safe_success": None, "opsec": None}
    conflict_rows = _observations(key)
    conflict_rows[1]["safe_success"] = False
    conflict = replay.build_label_map(conflict_rows)
    assert "safe_success_conflict" in conflict["labels"][key]["reason_codes"]
    assert replay.score_branch(conflict, key) == {"safe_success": None, "opsec": None}
    conflict_rows[1]["safe_success"] = True
    conflict_rows[1]["opsec"] = None
    unknown = replay.build_label_map(conflict_rows)
    assert replay.score_branch(unknown, key) == {"safe_success": None, "opsec": None}


def test_label_map_hash_tamper_fails_and_label_comparison_is_conservative():
    key = replay.branch_key("family", _packet(), "execute", "prod")
    label_map = replay.build_label_map(_observations(key))
    tampered = deepcopy(label_map)
    tampered["labels"][key]["safe_success"] = False
    with pytest.raises(ValueError, match="hash"):
        replay.score_branch(tampered, key)

    assert replay.compare_labels(
        {"safe_success": True, "opsec": False}, {"safe_success": False, "opsec": True}
    )["gauge_decision"] == "reject"
    unknown = replay.compare_labels(
        {"safe_success": True, "opsec": None}, {"safe_success": False, "opsec": True}
    )
    assert unknown["gauge_decision"] == "inconclusive"
    assert unknown["opsec_delta"] is None
    assert unknown["safe_success_delta"] is None
    assert replay.compare_labels(
        {"safe_success": True, "opsec": True}, {"safe_success": False, "opsec": True}
    )["gauge_decision"] == "promote"
    for invalid in (0, 1, float("inf")):
        with pytest.raises(ValueError):
            replay.compare_labels(
                {"safe_success": True, "opsec": True}, {"safe_success": False, "opsec": True},
                practical_delta=invalid,
            )
