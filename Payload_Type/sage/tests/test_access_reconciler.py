import sys

import pytest
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
            "domain": "",
            "user": "NORTH\\samwell.tarly",
            "integrity_level": "3",
        },
    ]

    footholds = access_reconciler.project_access(
        raw_callbacks, NOW, {"10": True, "11": True},
        netbios_to_fqdn={"NORTH": "north.sevenkingdoms.local"},
    )

    assert [foothold.callback_id for foothold in footholds] == ["10", "11"]
    assert [foothold.agent for foothold in footholds] == ["Apollo", "Merlin"]
    assert [foothold.forest for foothold in footholds] == [
        "north.sevenkingdoms.local",
        "north.sevenkingdoms.local",
    ]
    assert footholds[1].identity == "NORTH\\samwell.tarly"
    assert all(foothold.alive is True for foothold in footholds)


def test_project_access_reads_nested_payloadtype_name_for_sage_filtering():
    foothold = access_reconciler.project_access(
        [
            {
                "display_id": 1,
                "payload": {"payloadtype": {"name": "sage"}},
                "host": "SAGE",
                "user": "Sage",
                "integrity_level": 2,
            }
        ],
        NOW,
        {"1": True},
    )[0]

    assert foothold.agent == "sage"
    rendered = engagement_state.render_engagement_state(
        engagement_state.EngagementState(objective="x", footholds=[foothold])
    )
    assert "SAGE | forest=sage" not in rendered


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
        netbios_to_fqdn={"NORTH": "north.sevenkingdoms.local"},
    )[0]

    predicates = engagement_state.EngagementState(
        objective="essos DA",
        footholds=[foothold],
    ).satisfied_predicates()

    assert "live-foothold:north.sevenkingdoms.local" in predicates
    assert "authenticated:north.sevenkingdoms.local" in predicates
    assert "live-foothold:essos.local" not in predicates


def test_forest_normalization_maps_netbios_via_engagement_config():
    # An engagement-supplied map resolves NetBIOS -> FQDN (no hardcoded lab topology).
    assert access_reconciler.normalize_forest(
        "NORTH", netbios_to_fqdn={"NORTH": "north.sevenkingdoms.local"}
    ) == "north.sevenkingdoms.local"
    # A generic, non-lab map works identically — proves environment-agnosticism.
    assert access_reconciler.normalize_forest(
        "CORP", netbios_to_fqdn={"CORP": "corp.example.com"}
    ) == "corp.example.com"


def test_forest_normalization_passes_netbios_through_without_config():
    # With NO engagement map, a NetBIOS hint passes through casefolded — no fabricated FQDN,
    # and no hardcoded GOAD default (the old DEFAULT_NETBIOS_TO_FQDN is gone).
    assert access_reconciler.normalize_forest("NORTH") == "north"
    assert access_reconciler.normalize_forest("CORP") == "corp"
    assert not hasattr(access_reconciler, "DEFAULT_NETBIOS_TO_FQDN")


def test_engagement_netbios_map_reads_env_json(monkeypatch):
    monkeypatch.setenv("SAGE_ENGAGEMENT_NETBIOS_MAP", '{"CORP": "corp.example.com"}')
    assert access_reconciler._engagement_netbios_map() == {"corp": "corp.example.com"}


def test_engagement_netbios_map_failsoft_on_unset_or_malformed(monkeypatch):
    monkeypatch.delenv("SAGE_ENGAGEMENT_NETBIOS_MAP", raising=False)
    assert access_reconciler._engagement_netbios_map() == {}
    monkeypatch.setenv("SAGE_ENGAGEMENT_NETBIOS_MAP", "not json{{")
    assert access_reconciler._engagement_netbios_map() == {}
    monkeypatch.setenv("SAGE_ENGAGEMENT_NETBIOS_MAP", '["not", "a", "dict"]')
    assert access_reconciler._engagement_netbios_map() == {}


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


def _named_hop(netbios, fqdn, *, status="achieved"):
    evidence = {"dc_domain": fqdn, "callback_netbios_domain": netbios}
    return engagement_state.Hop(
        id=f"h-{netbios}-{fqdn}", technique="domain-admin-membership-check", target=fqdn, effect=f"da:{fqdn}",
        status=status, evidence=evidence, preconditions=[], satisfied_effects=[f"da:{fqdn}"], source="test",
        timestamp=NOW,
    )


def _name_fact(netbios, fqdn):
    return engagement_state.GraphFact(
        predicate=f"domain-netbios:{netbios}:{fqdn}", source="bloodhound:cypher", timestamp=NOW, ttl_seconds=3600,
    )


def _state(hops=(), facts=()):
    return engagement_state.EngagementState(objective="", footholds=[], hops=list(hops), graph_facts=list(facts))


def test_learned_netbios_map_pairs_names_from_evidence_not_spelling():
    state = _state([_named_hop("corphq", "corp.example.com")], [_name_fact("north", "north.sevenkingdoms.local")])
    assert engagement_state.learned_netbios_map(state) == {
        "corphq": "corp.example.com",
        "north": "north.sevenkingdoms.local",
    }
    # A request attempt that did not achieve its effect teaches nothing.
    assert engagement_state.learned_netbios_map(_state([_named_hop("corphq", "corp.example.com", status="failed")])) == {}


@pytest.mark.parametrize("hops, facts", [
    ([_named_hop("corp", "corp.example.com"), _named_hop("corp", "corp.example.net")], []),  # one name, two FQDNs
    ([_named_hop("corp", "corp.example.com")], [_name_fact("corphq", "corp.example.com")]),  # one FQDN, two names
])
def test_learned_netbios_map_drops_ambiguous_pairs(hops, facts):
    assert engagement_state.learned_netbios_map(_state(hops, facts)) == {}


def test_learned_netbios_map_ignores_malformed_names():
    hops = [_named_hop("corp.example.com", "corp.example.com"), _named_hop("corp", "corp")]
    assert engagement_state.learned_netbios_map(_state(hops)) == {}


def test_engagement_netbios_map_learns_by_default_and_can_be_switched_off_or_pinned(monkeypatch):
    state = _state([_named_hop("corphq", "corp.example.com"), _named_hop("north", "north.sevenkingdoms.local")])
    monkeypatch.delenv("SAGE_ENGAGEMENT_NETBIOS_MAP", raising=False)
    assert access_reconciler._engagement_netbios_map(state) == {
        "corphq": "corp.example.com", "north": "north.sevenkingdoms.local",
    }
    for off in ("off", "OFF", "0", "false", "none"):
        monkeypatch.setenv("SAGE_ENGAGEMENT_NETBIOS_MAP", off)
        assert access_reconciler._engagement_netbios_map(state) == {}
    monkeypatch.setenv("SAGE_ENGAGEMENT_NETBIOS_MAP", '{"CORPHQ": "hq.example.com"}')
    assert access_reconciler._engagement_netbios_map(state) == {
        "corphq": "hq.example.com", "north": "north.sevenkingdoms.local",
    }
    monkeypatch.setenv("SAGE_ENGAGEMENT_NETBIOS_MAP", "not json{{")
    assert access_reconciler._engagement_netbios_map(state)["corphq"] == "corp.example.com"


def test_project_access_keeps_the_reported_domain_beside_the_learned_forest():
    foothold = access_reconciler.project_access(
        [{"id": 7, "agent": "Apollo", "host": "WS01", "domain": "CORPHQ", "user": "alice", "integrity_level": 2}],
        NOW, {"7": True}, netbios_to_fqdn={"corphq": "corp.example.com"},
    )[0]
    assert (foothold.forest, foothold.reported_domain) == ("corp.example.com", "corphq")


@pytest.mark.parametrize("output, expected", [
    ("The request will be processed at a domain controller for domain north.sevenkingdoms.local.\r\n\r\n"
     "Group name     Domain Admins", "north.sevenkingdoms.local"),
    ("the request will be processed at a domain controller for domain CORP.Example.COM.", "corp.example.com"),
    # As a tool result carries it: JSON-escaped, with a literal \r\n after the period (the observed task 8 shape).
    ('{"output_preview":"The request will be processed at a domain controller for domain north.sevenkingdoms.local.'
     '\\r\\n\\r\\nGroup name     Domain Admins"}', "north.sevenkingdoms.local"),
    ("The request will be processed at a domain controller for domain NORTH.", ""),  # not an FQDN
    ("Group name     Domain Admins\nThe command completed successfully.", ""),
    ("processed at a domain controller for domain a.local.\nprocessed at a domain controller for domain b.local.", ""),
])
def test_domain_controller_domain_reads_only_the_net_domain_line(output, expected):
    assert engagement_state.domain_controller_domain(output) == expected
