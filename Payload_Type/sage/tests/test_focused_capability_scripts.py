import asyncio
import importlib.util
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "skills"
    / "sage-focused-capability-tests"
    / "scripts"
    / "run_focused_managed_secret_read.py"
)
SPEC = importlib.util.spec_from_file_location("run_focused_managed_secret_read", SCRIPT)
run_managed_secret = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(run_managed_secret)


class FakeTools:
    def __init__(self, outputs):
        self._outputs = iter(outputs)
        self._last_issued_task_display_id = None
        self.calls = []

    async def issue_task_and_waitfor_task_output(self, command, parameters, callback_id, timeout):
        self.calls.append((command, parameters, callback_id, timeout))
        self._last_issued_task_display_id = 220 + len(self.calls)
        return next(self._outputs)


def test_managed_secret_helper_executes_multistep_plan_and_uses_probe_command_output():
    tools = FakeTools([
        "Successfully loaded sharpview.exe into the default AppDomain",
        "distinguishedname : CN=BRAAVOS,DC=essos,DC=local\nms-mcs-admpwd : CorrectHorseBatteryStaple!",
    ])
    commands = [
        {
            "command": "load-assembly",
            "parameters": {"filename": "SharpView.exe"},
            "expected_probe": "",
        },
        {
            "command": "invoke-assembly",
            "parameters": {"assembly": "SharpView.exe", "arguments": "Get-DomainComputer -FindOne"},
            "expected_probe": "extract_managed_local_admin_secret_probe",
        },
    ]

    output, task_id = asyncio.run(
        run_managed_secret._execute_plan_commands(tools, commands, callback_id=2, timeout=120)
    )

    assert [call[0] for call in tools.calls] == ["load-assembly", "invoke-assembly"]
    assert tools.calls[1][1]["assembly"] == "SharpView.exe"
    assert output.startswith("distinguishedname")
    assert task_id == 222


def test_managed_secret_helper_preserves_single_command_plan():
    tools = FakeTools([
        "distinguishedname : CN=WS01,DC=child,DC=lab,DC=local\nms-mcs-admpwd : CorrectHorseBatteryStaple!",
    ])
    commands = [
        {
            "command": "powerpick",
            "parameters": "DirectorySearcher query",
            "expected_probe": "extract_managed_local_admin_secret_probe",
        },
    ]

    output, task_id = asyncio.run(
        run_managed_secret._execute_plan_commands(tools, commands, callback_id=13, timeout=60)
    )

    assert tools.calls == [("powerpick", "DirectorySearcher query", 13, 60)]
    assert output.startswith("distinguishedname")
    assert task_id == 221
