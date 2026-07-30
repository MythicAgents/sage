"""ISC-67 — the approval gate compares like with like.

ARGRES rewrites command parameters against the live schema (key renames, group repair, declared
defaults) BETWEEN the operator's approval and the effect boundary. The approval claim stores the
PRE-ARGRES proposal; the effect path carries the POST-ARGRES arguments. Comparing them denies the
operator's own approval whenever the resolver changed anything.

Confirmed live 2026-07-29: an approved `cat` was refused with "approved proposal does not cover this
exact effect" after ARGRES logged `notes=["mapped 'path' to 'Path'"]` — a pure key rename, no value
change. Note this is NOT the shape the original ISC-67 design anticipated (it assumed *defaulted
keys*), which is why the fix is resolver parity rather than an exclusion list.

**This is the approval boundary, so the tests that matter most are the adversarial ones.** The
mechanism is a lookup of one exact recorded (input -> output) pair for one command and callback, not
a rule. Everything below the parity test exists to prove it cannot be used to slip a materially
different action past the gate.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.mythic_tools import MythicTools

TOOL = "issue_task_and_waitfor_task_output"


def _tools(binding=None):
    t = MythicTools.__new__(MythicTools)
    if binding is not None:
        t._last_argres_binding = binding
    return t


def _binding(original, resolved, command="cat", cb=1):
    return {
        "command": command,
        "callback_display_id": cb,
        "original": original,
        "resolved": resolved,
    }


def _args(parameters, command="cat", cb=1):
    return {"command": command, "callback_display_id": cb, "parameters": parameters}


def _eff(tools, parameters, **kw):
    return tools._effective_request_action_arguments(TOOL, _args(parameters, **kw))


def test_key_rename_no_longer_denies_the_operators_own_approval():
    """The live failure: expected carries `path`, actual carries `Path`. Same effect."""
    tools = _tools(_binding({"path": "C:\\x.txt"}, {"Path": "C:\\x.txt"}))

    expected = _eff(tools, {"path": "C:\\x.txt"})   # what the operator approved
    actual = _eff(tools, {"Path": "C:\\x.txt"})     # what ARGRES produced

    assert expected == actual, "resolver-only differences must not deny an approved action"


def test_defaulted_key_addition_also_matches():
    """The shape the original design anticipated is covered by the same mechanism."""
    tools = _tools(
        _binding({"luid": ""}, {"luid": "", "getSystemTickets": False}, command="ticket_cache_list")
    )
    expected = _eff(tools, {"luid": ""}, command="ticket_cache_list")
    actual = _eff(tools, {"luid": "", "getSystemTickets": False}, command="ticket_cache_list")
    assert expected == actual


# ── Adversarial: the mechanism must never admit a materially different action ─────────────────

def test_changed_value_still_denies():
    """A different PATH is a different effect. Substitution must not fire."""
    tools = _tools(_binding({"path": "C:\\approved.txt"}, {"Path": "C:\\approved.txt"}))

    expected = _eff(tools, {"path": "C:\\approved.txt"})
    sneaky = _eff(tools, {"Path": "C:\\SOMETHING_ELSE.txt"})

    assert expected != sneaky, "a changed argument value must still fail coverage"


def test_binding_does_not_apply_to_a_different_command():
    tools = _tools(_binding({"path": "C:\\x.txt"}, {"Path": "C:\\x.txt"}, command="cat"))

    expected = _eff(tools, {"path": "C:\\x.txt"}, command="download")
    actual = _eff(tools, {"Path": "C:\\x.txt"}, command="download")

    assert expected != actual, "a binding recorded for `cat` must not normalize `download`"


def test_binding_does_not_apply_to_a_different_callback():
    tools = _tools(_binding({"path": "C:\\x.txt"}, {"Path": "C:\\x.txt"}, cb=1))

    expected = _eff(tools, {"path": "C:\\x.txt"}, cb=2)
    actual = _eff(tools, {"Path": "C:\\x.txt"}, cb=2)

    assert expected != actual, "a binding recorded for callback 1 must not normalize callback 2"


def test_partial_match_of_the_recorded_original_does_not_substitute():
    """Substitution requires the EXACT recorded input, not a superset or subset."""
    tools = _tools(_binding({"path": "C:\\x.txt", "flag": True}, {"Path": "C:\\x.txt"}))

    near = _eff(tools, {"path": "C:\\x.txt"})            # subset of the recorded original
    actual = _eff(tools, {"Path": "C:\\x.txt"})
    assert near != actual, "only the exact recorded original may be substituted"


def test_no_binding_leaves_behaviour_unchanged():
    """Absent a recorded resolution the gate behaves exactly as before this change."""
    tools = _tools()
    assert _eff(tools, {"path": "C:\\x.txt"}) != _eff(tools, {"Path": "C:\\x.txt"})


def test_junk_binding_is_ignored():
    for junk in (None, "", 42, [], {"command": "cat"}):
        tools = _tools(junk)
        # Must not raise, and must not silently equate different arguments.
        assert _eff(tools, {"path": "a"}) != _eff(tools, {"Path": "a"})
