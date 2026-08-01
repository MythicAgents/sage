"""What `execute_capability` hands to `materialize_capability_inputs` must be JSON-native.

`materialize_capability_inputs` declares `action: dict | str`, and the first thing it does is run
its request-contract guard, which canonicalizes the arguments through
`request_contract.canonical_action_arguments`. That function refuses any non-JSON value by design.

`execute_capability` was passing the `CapabilityAction` dataclass straight through, so Sage's own
internal call was denied with `arguments.action contains non-JSON value CapabilityAction`. An
autonomous run reached twenty verified effects and the CA private key, then halted on that
serialization mismatch rather than on anything to do with the capability.

The bug survived because nothing asserted the boundary. These tests do.

Identifiers here are deliberately generic (`lab.local`, `ca01`, `alice`). Only the effect *shape*
`adcs-ca-private-key:<ca>@<domain>` is load-bearing, and a fixture named after the benchmark range
would let a range-specific branch pass unnoticed — the failure mode `AGENTS.md` § Highest-Value
Demo Work exists to prevent.
"""
import asyncio
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import capabilities  # noqa: E402
import mythic_tools  # noqa: E402
import request_contract  # noqa: E402
from test_circuit_breaker import _split_issue  # noqa: E402


def _action() -> capabilities.CapabilityAction:
    return capabilities.CapabilityAction(
        name="adcs-certificate-auth",
        target="domain=lab.local;account=alice;ca=ca01;callback=1",
        preconditions=["adcs-ca-private-key:ca01@lab.local"],
        effects=["da:lab.local"],
        intent={
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "alice",
            "callback_id": "1",
        },
    )


def test_canonicalizer_refuses_the_dataclass_and_accepts_its_dict():
    """The guard's own contract, pinned: this is why the call site has to convert."""
    action = _action()
    try:
        request_contract.canonical_action_arguments({"action": action})
    except ValueError as exc:
        assert "non-JSON value CapabilityAction" in str(exc)
    else:
        raise AssertionError("a dataclass must not canonicalize")

    canonical = request_contract.canonical_action_arguments({"action": asdict(action)})
    assert canonical["action"]["name"] == "adcs-certificate-auth"


def test_action_dict_round_trips_back_into_the_same_capability_request():
    """Converting must not lose the action: the tool rebuilds it on the other side."""
    parsed = request_contract.parse_capability_request(asdict(_action()), {})
    assert parsed.action_data["name"] == "adcs-certificate-auth"
    assert "lab.local" in str(parsed.action_data["target"])


def test_execute_capability_hands_materializer_a_json_native_action(monkeypatch):
    """The regression itself: whatever reaches the materializer must pass the guard."""
    mt = mythic_tools.MythicTools(agent_task_id="materialization-contract")
    mt.client = object()
    seen = {}

    async def capture(action, inputs=None):
        seen["action"] = action
        # Canonicalizing here is the assertion: the real guard does exactly this, and a dataclass
        # raises. Returning a failure payload keeps execute_capability from running any further.
        request_contract.canonical_action_arguments({"action": action, "inputs": inputs})
        return '{"ok": false, "missing": ["stub"], "reason": "captured by test"}'

    monkeypatch.setattr(mt, "materialize_capability_inputs", capture)
    monkeypatch.setattr(mt, "_capability_needs_runtime_materialization", lambda a, i: True)
    # The live run reached the materializer because it had already earned the CA private key
    # (`adcs-ca-private-key:ca01@lab.local` was an achieved effect). Without it an earlier
    # artifact-scope guard refuses first and the boundary under test is never exercised.
    monkeypatch.setattr(
        mt,
        "_capability_achieved_effects",
        lambda *a, **k: {"adcs-ca-private-key:ca01@lab.local"},
    )

    # A current-context preflight issues a real `shell` probe before materialization, so the issue
    # path has to be stubbed or the capability fails on the fake client instead of reaching the
    # boundary under test.
    with _split_issue("Cached Tickets\r\n"):
        asyncio.run(mt.execute_capability({
            "capability": "adcs-certificate-auth",
            "domain": "lab.local",
            "account": "alice",
            "ca_host": "ca01",
            "callback_id": "1",
        }))

    assert "action" in seen, "execute_capability never reached the materializer"
    assert not isinstance(seen["action"], capabilities.CapabilityAction), (
        "the dataclass reached a tool whose signature declares dict | str"
    )
    assert seen["action"]["name"] == "adcs-certificate-auth"
