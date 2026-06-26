"""Tests for the eval-layer GOAD wall checkpoints (ai/hillclimb/wall_checkpoints.py).

Run: cd Payload_Type/sage && python3 -m pytest tests/test_wall_checkpoints.py -q

These walls are scenario-specific and intentionally live in the eval layer, NOT the runtime controller.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai.hillclimb import wall_checkpoints as wc  # noqa: E402


def test_walls_reached_ordered_prefix():
    assert wc.walls_reached({"graph-built:x", "da:north"}) == ["W1_graph_collected", "W2_da_child"]


def test_full_essos_chain_reaches_all_walls():
    achieved = {
        "graph-built:host", "da:north.sevenkingdoms.local", "krbtgt-hash:north.sevenkingdoms.local",
        "da:sevenkingdoms.local", "managed-local-admin-secret:braavos@essos.local",
        "local-admin:braavos@essos.local", "remote-exec:braavos@essos.local",
        "adcs-ca-private-key:braavos@essos.local", "da:essos.local",
    }
    reached = wc.walls_reached(achieved)
    assert reached[0] == "W1_graph_collected" and reached[-1] == "W8_essos_da"
    assert len(reached) == 8


def test_krbtgt_wall_but_not_cross_domain():
    """The current autonomous ceiling: child krbtgt reached, cross-domain (W4) NOT — the wall to cross."""
    reached = wc.walls_reached({"graph-built:x", "da:north", "krbtgt-hash:north.sevenkingdoms.local"})
    assert "W3_krbtgt_child" in reached
    assert "W4_cross_domain_parent" not in reached


def test_walls_from_result_reads_achieved_effects():
    class R:
        achieved_effects = ["da:essos.local"]
    assert "W8_essos_da" in wc.walls_from_result(R())


def test_empty_is_no_walls():
    assert wc.walls_reached(set()) == []
    assert wc.walls_reached(None) == []
