import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-goad-reset" / "scripts" / "bh_reset.py"
SPEC = importlib.util.spec_from_file_location("bh_reset", SCRIPT)
bh_reset = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(bh_reset)


def test_wait_for_empty_delays_first_poll_and_retries(monkeypatch):
    responses = iter([
        (200, [{"name": "OLD.LOCAL"}]),
        (200, []),
    ])
    sleeps = []
    monkeypatch.setattr(bh_reset, "_domains", lambda: next(responses))

    result = bh_reset.wait_for_empty(
        initial_wait=10,
        poll_interval=5,
        attempts=2,
        sleep=sleeps.append,
    )

    assert result is True
    assert sleeps == [10, 5]
