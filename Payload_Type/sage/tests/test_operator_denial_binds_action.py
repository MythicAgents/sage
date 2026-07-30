"""An operator rejection binds the ACTION for the rest of the request.

Root cause of the live re-proposal loop. On rejection the agent received only
`[DENIED by operator] <tool> was not executed.` — which says *the tool did not run*, not *this action
is closed*. The model read it as a failed attempt, re-proposed the identical action, the Supervisor
re-delegated the same objective, and the request looped until the global step limit.

Two mechanisms already existed and both missed:

1. `TurnAuthority.denies_action_digest` is keyed on `approval_action_fingerprint`, which hashes the
   FULL canonical argument dict. The observed loop varied its arguments (`luid: ""` -> `"0"` ->
   `"0x5b16c"`), so every re-proposal produced a different digest and matched nothing. ISC-69a already
   had to learn this for the card guard; the denial path never got the same treatment.
2. That digest check is skipped entirely when a typed contract is installed — which is the supervised
   path, i.e. the only path where operator rejections happen at all.

The fix keys denials on `_guarded_action_key` — (tool, command, callback), typed fields only, no
prose — and checks it regardless of the typed contract.

This is Claude Code parity, deliberately: a denied action is closed, but the agent stays free to try
a DIFFERENT approach. Rejection does not end the request.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.model import Model, _guarded_action_key


def _action(name="issue_task_and_waitfor_task_output", **args):
    return {"name": name, "args": args}


def test_key_ignores_argument_variation():
    """The exact drift that defeated the full-argument digest."""
    base = _action(command="ticket_cache_list", callback_display_id=1, luid="")
    drift1 = _action(command="ticket_cache_list", callback_display_id=1, luid="0")
    drift2 = _action(command="ticket_cache_list", callback_display_id=1, luid="0x5b16c")

    assert _guarded_action_key(base) == _guarded_action_key(drift1) == _guarded_action_key(drift2)


def test_key_separates_target_and_command():
    """Same command on another callback, or another command on the same one, is a different action."""
    a = _action(command="ticket_cache_list", callback_display_id=1)
    other_cb = _action(command="ticket_cache_list", callback_display_id=2)
    other_cmd = _action(command="whoami", callback_display_id=1)

    assert _guarded_action_key(a) != _guarded_action_key(other_cb)
    assert _guarded_action_key(a) != _guarded_action_key(other_cmd)


def test_key_is_defensive_on_junk():
    for junk in (None, "", 42, [], {"args": "not-a-dict"}):
        assert isinstance(_guarded_action_key(junk), str)


def test_rejection_records_the_coarse_key():
    """`_record_hitl_denials` must remember the action, not one exact argument dict."""
    model = Model.__new__(Model)
    rejected = _action(command="ticket_cache_list", callback_display_id=1, luid="")
    model._record_hitl_denials([rejected])

    assert _guarded_action_key(rejected) in model._denied_action_keys

    # A reworded re-proposal of the same action against the same target is recognised.
    reproposal = _action(command="ticket_cache_list", callback_display_id=1, luid="0x5b16c")
    assert _guarded_action_key(reproposal) in model._denied_action_keys

    # A genuinely different action is NOT blocked — the agent must stay free to try another route.
    alternative = _action(command="whoami", callback_display_id=1)
    assert _guarded_action_key(alternative) not in model._denied_action_keys


def test_denials_accumulate_across_rejections():
    model = Model.__new__(Model)
    model._record_hitl_denials([_action(command="ticket_cache_list", callback_display_id=1)])
    model._record_hitl_denials([_action(command="whoami", callback_display_id=1)])
    assert len(model._denied_action_keys) == 2


def test_malformed_action_fails_closed_and_records_nothing():
    """A nameless action must never land an empty key in the set — it would match everything.

    The existing `approval_action_fingerprint` already rejects it, so `_record_hitl_denials` raises
    before reaching the coarse-key code. That is fail-closed and correct: production action requests
    come from the middleware and always carry a tool name, so the raise is unreachable there. Pinned
    as the real behaviour rather than softened with defensive code for a case that cannot occur —
    what matters is only that no empty key is ever recorded.
    """
    import pytest

    model = Model.__new__(Model)
    with pytest.raises(ValueError):
        model._record_hitl_denials([{"name": "", "args": {}}])
    assert not getattr(model, "_denied_action_keys", set())


def test_steer_does_not_bind_the_action():
    """A steer denies THIS proposal but must not close the action for the request.

    Operators routinely phrase a steer as conditional approval — "approved, but also do this on
    callback 2". Binding there would permanently block the action they just said yes to, which is a
    worse failure than the loop ISC-76 exists to stop. Only a bare reject binds.
    """
    model = Model.__new__(Model)
    action = _action(command="ticket_cache_list", callback_display_id=1)

    model._record_hitl_denials([action], bind_action=False)
    assert not getattr(model, "_denied_action_keys", set()), "a steer must not bind"

    model._record_hitl_denials([action], bind_action=True)
    assert _guarded_action_key(action) in model._denied_action_keys, "a bare reject binds"


def test_bind_is_the_default():
    """Callers that do not opt out get the binding behaviour."""
    model = Model.__new__(Model)
    action = _action(command="whoami", callback_display_id=1)
    model._record_hitl_denials([action])
    assert _guarded_action_key(action) in model._denied_action_keys
