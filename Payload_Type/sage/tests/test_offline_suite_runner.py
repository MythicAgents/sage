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


GiB = 2**30


def _printed_commands(monkeypatch, capsys, argv, *, xdist: bool, available: int | None = 64 * GiB, cores: int = 6):
    monkeypatch.setattr(runner, "available_memory_bytes", lambda: available)
    monkeypatch.setattr(runner.os, "cpu_count", lambda: cores)
    real = runner.importlib.util.find_spec
    monkeypatch.setattr(runner.importlib.util, "find_spec",
                        lambda name, *a: (object() if xdist else None) if name == "xdist" else real(name, *a))
    code = runner.main(["--print-command", *argv])
    out = capsys.readouterr()
    return code, [line.split() for line in out.out.splitlines() if line.strip()], out.err


def test_the_gate_is_the_parallel_run_when_xdist_is_installed(monkeypatch, capsys):
    code, commands, _ = _printed_commands(monkeypatch, capsys, [], xdist=True)
    assert code == 0 and len(commands) == 2
    assert " ".join(commands[0]).endswith("-n 6 -q -m not serial")
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


@pytest.mark.parametrize("available, cores, expected", [
    (64 * GiB, 6, 6),            # plenty of memory: one worker per core
    (7 * GiB, 6, 3),             # the Mythic + Sage host: (7 - 2) / 1.5 GiB
    (3 * GiB, 6, 1),             # barely anything free
    (1 * GiB, 6, 1),             # below the reserve never yields zero or a negative count
    (None, 6, 6),                # no MemAvailable (non-Linux): cores only
    (64 * GiB, None, 1),
])
def test_workers_are_the_core_count_capped_by_free_memory(available, cores, expected):
    assert runner.default_workers(available, cores) == expected


def test_low_memory_caps_the_parallel_pass_and_says_so(monkeypatch, capsys):
    code, commands, err = _printed_commands(monkeypatch, capsys, [], xdist=True, available=7 * GiB)
    assert code == 0 and " ".join(commands[0]).endswith("-n 3 -q -m not serial")
    assert "caps the parallel pass at 3 workers" in err


def test_too_little_memory_for_two_workers_runs_in_one_process(monkeypatch, capsys):
    code, commands, err = _printed_commands(monkeypatch, capsys, [], xdist=True, available=3 * GiB)
    assert code == 0 and len(commands) == 1
    assert "-n" not in commands[0][commands[0].index("pytest") + 1:]
    assert "not enough free memory" in err


def test_a_caller_choosing_workers_is_not_capped(monkeypatch, capsys):
    code, commands, err = _printed_commands(monkeypatch, capsys, ["-n", "6"], xdist=True, available=3 * GiB)
    assert code == 0 and commands[0][-2:] == ["-n", "6"] and commands[0].count("-n") == 1 and err == ""


def test_available_memory_is_read_from_meminfo(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16000000 kB\nMemFree:  100 kB\nMemAvailable:    7340032 kB\n")
    assert runner.available_memory_bytes(meminfo) == 7 * GiB
    assert runner.available_memory_bytes(tmp_path / "absent") is None
