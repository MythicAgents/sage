import importlib.util
from pathlib import Path

import pytest


SCRIPT = (Path(__file__).resolve().parents[3] / "skills" / "sage-focused-capability-tests" / "scripts"
          / "run_offline_suite.py")
SPEC = importlib.util.spec_from_file_location("run_offline_suite", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runner)


@pytest.mark.parametrize("argv, expected", [
    ([], (set(), [])),
    (["-q", "-k", "foo"], (set(), ["-q", "-k", "foo"])),               # pytest options need no separator
    (["supported", "-q"], ({"supported"}, ["-q"])),                     # legacy tier word is accepted
    (["--parallel", "--print-command", "-n", "0"], ({"--parallel", "--print-command"}, ["-n", "0"])),
    (["--", "--print-command"], (set(), ["--print-command"])),          # after --, everything belongs to pytest
])
def test_wrapper_options_come_first_and_the_rest_goes_to_pytest(argv, expected):
    assert runner.split_args(argv) == expected


def test_the_default_run_is_one_serial_command_over_every_test():
    (command,) = runner.pass_commands(["-q"])
    pytest_args = command[command.index("pytest") + 1:]
    assert "-n" not in pytest_args and "-m" not in pytest_args


def test_parallel_run_is_a_parallel_pass_then_a_serial_pass_over_every_test():
    parallel, serial = runner.pass_commands(["-q"], parallel=True)

    assert parallel[-5:] == ["-n", "auto", "-q", "-m", "not serial"]
    assert serial[-3:] == ["-q", "-m", "serial"] and "-n" not in serial[serial.index("pytest") + 1:]
    assert not any(item.startswith("--ignore") for item in parallel + serial)


@pytest.mark.parametrize("args", [["-n", "0"], ["-n4"], ["--numprocesses=2"], ["-m", "slow"]])
def test_a_caller_choosing_workers_or_markers_gets_exactly_one_command(args):
    (command,) = runner.pass_commands(args, parallel=True)
    assert command[-len(args):] == args
    assert command.count("-n") <= 1


def test_without_xdist_the_suite_runs_once_serially():
    (command,) = runner.pass_commands(["-q"], parallel=False)
    pytest_args = command[command.index("pytest") + 1:]
    assert "-n" not in pytest_args and "-m" not in pytest_args
