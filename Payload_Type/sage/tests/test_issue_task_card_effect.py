"""Raw-task approval cards show the technique's effect with its target filled in, never a template.

Effects in TECHNIQUE_MODEL are templates such as ``graph-built:{target}``. The card used to print the
template verbatim (``graph-built:{target}``). It now fills the target the same way the gate does, and
omits the effect when the target can't be resolved, keeping only the expected evidence.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph import engagement_state  # noqa: E402
from ai.langgraph import model as model_mod  # noqa: E402

SHARPHOUND = '{"assembly_name": "SharpHound.exe", "assembly_arguments": "-c All"}'


def _foothold(callback_id="2", host="castelblack.north.lab.local", forest="north.lab.local"):
    return engagement_state.Foothold(
        callback_id=callback_id, agent="apollo", host=host, forest=forest, identity="NORTH\\samwell",
        integrity="medium", alive=True, source="test", timestamp="2026-10-29T00:00:00Z",
    )


def _card(command, parameters, footholds=(), callback_id=2):
    model = object.__new__(model_mod.Model)
    model.mythic_client = SimpleNamespace(
        _engagement_footholds=list(footholds), _engagement_hops=[], _engagement_graph_facts=[]
    )
    args = {"command": command, "callback_display_id": callback_id, "parameters": parameters}
    return model._hitl_decision_context([{"name": "issue_task_and_waitfor_task_output", "args": args}])


def test_sharphound_card_resolves_the_collection_key_from_the_callback_foothold():
    text = _card("execute_assembly", SHARPHOUND, [_foothold()])
    assert "Intended effect: graph-built:north.lab.local|baseline" in text
    assert "{" not in text and "}" not in text


def test_sharphound_domain_scope_is_part_of_the_resolved_key():
    params = '{"assembly_name": "SharpHound.exe", "assembly_arguments": "-c All --Domain essos.local"}'
    assert "graph-built:north.lab.local|baseline|scope:essos.local" in _card("execute_assembly", params, [_foothold()])


@pytest.mark.parametrize(
    "footholds",
    (
        (),                                   # gate has not reconciled yet
        (_foothold(callback_id="7"),),        # near-match: a foothold for another callback
    ),
)
def test_unresolvable_collection_key_omits_the_effect_and_prints_no_braces(footholds):
    text = _card("execute_assembly", SHARPHOUND, footholds)
    assert "Intended effect" not in text
    assert "Expected evidence: graph_verified" in text
    assert "{" not in text and "}" not in text


def test_domain_effect_resolves_from_the_command():
    text = _card("mimikatz", '{"custom_preview": "lsadump::dcsync /domain:lab.local /user:krbtgt"}')
    assert "Intended effect: krbtgt-hash:lab.local" in text


def test_host_scoped_effect_uses_the_callback_host():
    params = '{"custom_preview": "sekurlsa::logonpasswords"}'
    assert "Intended effect: creds:castelblack.north.lab.local" in _card("mimikatz", params, [_foothold()])
    unresolved = _card("mimikatz", params)
    assert "Intended effect" not in unresolved
    assert "{" not in unresolved


def test_no_technique_model_effect_card_prints_a_placeholder():
    """Every templated effect either resolves or is omitted; no template survives on any card."""
    for technique, model in engagement_state.TECHNIQUE_MODEL.items():
        effect = model_mod.Model._issue_task_card_effect(
            object.__new__(model_mod.Model), engagement_state, technique, "", model, {}
        )
        assert "{" not in effect and "}" not in effect, technique
