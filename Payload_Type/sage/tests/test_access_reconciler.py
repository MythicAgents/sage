import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import access_reconciler  # noqa: E402
import engagement_state  # noqa: E402


NOW = "2026-06-06T12:00:00Z"


def test_project_access_maps_live_apollo_and_merlin_to_north_footholds():
    raw_callbacks = [
        {
            "id": 10,
            "agent": "Apollo",
            "host": "CASTLEBLACK",
            "domain": "NORTH",
            "user": "samwell.tarly@CASTELBLACK",
            "integrity_level": 2,
        },
        {
            "id": "11",
            "payloadtype": "Merlin",
            "host": "CASTLEBLACK",
            "domain": "NORTH",
            "user": "NORTH\\samwell.tarly@CASTELBLACK",
            "integrity_level": "3",
        },
    ]

    footholds = access_reconciler.project_access(raw_callbacks, NOW, {"10": True, "11": True})

    assert [foothold.callback_id for foothold in footholds] == ["10", "11"]
    assert [foothold.agent for foothold in footholds] == ["Apollo", "Merlin"]
    assert [foothold.forest for foothold in footholds] == [
        "north.sevenkingdoms.local",
        "north.sevenkingdoms.local",
    ]
    assert all(foothold.alive is True for foothold in footholds)


def test_dead_winterfell_system_callback_does_not_satisfy_host_predicates_until_live():
    raw_callback = {
        "id": 50,
        "agent": "Apollo",
        "host": "WINTERFELL",
        "domain": "NORTH",
        "user": "SYSTEM",
        "integrity_level": 4,
    }

    dead_foothold = access_reconciler.project_access([raw_callback], NOW, {"50": False})[0]
    dead_predicates = engagement_state.EngagementState(
        objective="x",
        footholds=[dead_foothold],
    ).satisfied_predicates()

    assert dead_foothold.alive is False
    assert "system:winterfell" not in dead_predicates
    assert "live-host:winterfell" not in dead_predicates

    live_foothold = access_reconciler.project_access([raw_callback], NOW, {"50": True})[0]
    live_predicates = engagement_state.EngagementState(
        objective="x",
        footholds=[live_foothold],
    ).satisfied_predicates()

    assert live_foothold.alive is True
    assert "system:winterfell" in live_predicates
    assert "live-host:winterfell" in live_predicates


def test_forest_scoping_keeps_north_access_from_satisfying_essos_access():
    foothold = access_reconciler.project_access(
        [
            {
                "id": 20,
                "agent": "Apollo",
                "host": "WINTERFELL",
                "domain": "NORTH",
                "user": "NORTH\\arya",
                "integrity_level": 3,
            }
        ],
        NOW,
        {"20": True},
    )[0]

    predicates = engagement_state.EngagementState(
        objective="essos DA",
        footholds=[foothold],
    ).satisfied_predicates()

    assert "live-foothold:north.sevenkingdoms.local" in predicates
    assert "authenticated:north.sevenkingdoms.local" in predicates
    assert "live-foothold:essos.local" not in predicates


def test_forest_normalization_maps_netbios_north_to_fqdn():
    assert access_reconciler.normalize_forest("NORTH") == "north.sevenkingdoms.local"


def test_integrity_normalization_maps_system_identity_to_system():
    foothold = access_reconciler.project_access(
        [
            {
                "id": 30,
                "agent": "Apollo",
                "host": "WINTERFELL",
                "domain": "NORTH",
                "user": "NT AUTHORITY\\SYSTEM",
                "integrity_level": 2,
            }
        ],
        NOW,
        {"30": True},
    )[0]

    assert foothold.integrity == "system"


def test_project_access_sets_provenance():
    foothold = access_reconciler.project_access(
        [{"id": 40, "domain": "UNKNOWN", "user": None, "host": None, "integrity_level": None}],
        NOW,
        {"40": True},
    )[0]

    assert foothold.source
    assert foothold.timestamp == NOW
    assert foothold.forest == "unknown"


def test_is_stale_returns_true_past_ttl_and_false_within_ttl():
    foothold = engagement_state.Foothold(
        callback_id="cb1",
        agent="Apollo",
        host="WINTERFELL",
        forest="north.sevenkingdoms.local",
        identity="NORTH\\arya",
        integrity="high",
        alive=True,
        source="test",
        timestamp="2026-06-06T12:00:00Z",
    )

    assert access_reconciler.is_stale(foothold, "2026-06-06T12:05:01Z", 300) is True
    assert access_reconciler.is_stale(foothold, "2026-06-06T12:05:00Z", 300) is False
