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
    (["--serial", "-q"], ({"--serial"}, ["-q"])),
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


def _printed_commands(monkeypatch, capsys, argv, *, xdist: bool):
    real = runner.importlib.util.find_spec
    monkeypatch.setattr(runner.importlib.util, "find_spec",
                        lambda name, *a: (object() if xdist else None) if name == "xdist" else real(name, *a))
    code = runner.main(["--print-command", *argv])
    out = capsys.readouterr()
    return code, [line.split() for line in out.out.splitlines() if line.strip()], out.err


def test_the_gate_is_the_parallel_run_when_xdist_is_installed(monkeypatch, capsys):
    code, commands, _ = _printed_commands(monkeypatch, capsys, [], xdist=True)
    assert code == 0 and len(commands) == 2
    assert " ".join(commands[0]).endswith("-n auto -q -m not serial")
    assert " ".join(commands[1]).endswith("-q -m serial") and "-n" not in commands[1]


def test_serial_runs_everything_in_one_process(monkeypatch, capsys):
    code, commands, _ = _printed_commands(monkeypatch, capsys, ["--serial"], xdist=True)
    assert code == 0 and len(commands) == 1
    pytest_args = commands[0][commands[0].index("pytest") + 1:]
    assert "-n" not in pytest_args and "-m" not in pytest_args


def test_without_xdist_the_default_falls_back_to_one_process_and_says_so(monkeypatch, capsys):
    code, commands, err = _printed_commands(monkeypatch, capsys, [], xdist=False)
    assert code == 0 and len(commands) == 1
    assert "-n" not in commands[0][commands[0].index("pytest") + 1:]
    assert "pytest-xdist is not installed" in err


def test_requiring_parallel_without_xdist_fails_instead_of_falling_back(monkeypatch, capsys):
    code, commands, err = _printed_commands(monkeypatch, capsys, ["--parallel"], xdist=False)
    assert code == 1 and commands == [] and "needs pytest-xdist" in err


def test_parallel_and_serial_together_is_refused(monkeypatch, capsys):
    code, commands, _ = _printed_commands(monkeypatch, capsys, ["--parallel", "--serial"], xdist=True)
    assert code == 2 and commands == []
