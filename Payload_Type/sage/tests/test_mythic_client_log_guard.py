"""Each record on the ``mythic`` logger reaches the container log once, however many Mythic clients Sage builds.

The Mythic scripting client (``mythic.mythic_classes.Mythic.__init__``) adds its own stdout handler to the
process-wide ``mythic`` logger, which ``mythic_container`` also logs through, on every ``mythic.login``. Sage logs in
at container start and for each new chat, so without the guard every container-log line is written once per login
plus once more.

Real boundaries: ``mythic_container``'s own stdout logging, the installed ``mythic`` package's ``login`` and client
constructor, and Sage's ``logging_fix`` owner. Stand-in, labelled: the Mythic server is a closed local port, so each
login builds its client and then fails its first request, as a login does in production before the server answers.
"""

from __future__ import annotations

import asyncio
import logging
import socket

import pytest
from mythic import mythic, mythic_classes
from mythic_container import logging as container_logging

from ai.langgraph import logging_fix

RECORD = "Error during graph execution: Error code: 401 - provider text"
LOGINS = 3


@pytest.fixture
def mythic_logger(monkeypatch):
    """The process-wide ``mythic`` logger with no handlers; its handlers, level and the client class are restored."""
    logger = logging.getLogger("mythic")
    assert logger is container_logging.logger is logging_fix.mythic_logger
    monkeypatch.setattr(mythic_classes.Mythic, "__init__", mythic_classes.Mythic.__init__)
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logger.level)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    return logger


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _login_against_closed_port(count: int = LOGINS) -> list[Exception]:
    """Call the real ``mythic.login`` ``count`` times, as ``MythicTools.login`` does (API token path)."""
    port = _closed_port()
    errors: list[Exception] = []

    async def _logins() -> None:
        for _ in range(count):
            try:
                await mythic.login(apitoken="sage-test-token", server_ip="127.0.0.1", server_port=port, ssl=False)
            except Exception as error:  # the closed port refuses the request after the client is built
                errors.append(error)

    asyncio.run(_logins())
    return errors


def _stdout_lines(capsys) -> list[str]:
    return [line for line in capsys.readouterr().out.splitlines() if line.strip()]


def _record_lines(logger: logging.Logger, capsys) -> list[str]:
    capsys.readouterr()
    logger.error(RECORD)
    return [line for line in _stdout_lines(capsys) if RECORD in line]


def test_one_record_is_one_container_log_line_after_several_logins(mythic_logger, capsys):
    container_logging.update_log_to_stdout("info")  # what mythic_container.logging.initialize() does at start
    container_handler, = mythic_logger.handlers
    logging_fix.install_mythic_client_log_guard()

    errors = _login_against_closed_port()

    assert len(errors) == LOGINS  # every login built its client and failed its request, as before
    lines = _record_lines(mythic_logger, capsys)
    assert len(lines) == 1, lines
    assert mythic_logger.handlers == [container_handler]  # mythic_container's handler, never removed
    assert mythic_logger.level == logging.WARNING  # the level the client sets is unchanged (parked)


def test_the_logging_fix_handler_is_kept(mythic_logger, capsys):
    logging_fix.ensure_logger_initialized()
    fallback, = mythic_logger.handlers
    logging_fix.install_mythic_client_log_guard()

    _login_against_closed_port()

    assert mythic_logger.handlers == [fallback]
    assert len(_record_lines(mythic_logger, capsys)) == 1


def test_with_no_other_handler_the_logging_fix_fallback_still_yields_one_line(mythic_logger, capsys):
    logging_fix.install_mythic_client_log_guard()

    _login_against_closed_port()
    assert mythic_logger.handlers == []  # the clients' own handlers are gone; nothing else was there
    logging_fix.ensure_logger_initialized()  # runs on every model build

    assert len(mythic_logger.handlers) == 1
    assert len(_record_lines(mythic_logger, capsys)) == 1


def test_installing_twice_wraps_the_constructor_once(mythic_logger, capsys):
    original = mythic_classes.Mythic.__init__
    logging_fix.install_mythic_client_log_guard()
    guarded = mythic_classes.Mythic.__init__
    logging_fix.install_mythic_client_log_guard()

    assert mythic_classes.Mythic.__init__ is guarded is not original
    assert guarded.__wrapped__ is original
    container_logging.update_log_to_stdout("info")
    _login_against_closed_port()
    assert len(mythic_logger.handlers) == 1
    assert len(_record_lines(mythic_logger, capsys)) == 1


def test_a_client_without_its_own_handler_is_left_alone(mythic_logger, monkeypatch):
    """Synthetic: a client constructor that adds no handler (as a fixed upstream release would)."""
    def quiet_init(self, *args, **kwargs):
        self.logger = logging.getLogger("mythic")

    monkeypatch.setattr(mythic_classes.Mythic, "__init__", quiet_init)
    other = logging.StreamHandler()
    mythic_logger.addHandler(other)
    logging_fix.install_mythic_client_log_guard()

    client = mythic_classes.Mythic(apitoken="sage-test-token")

    assert not hasattr(client, "log_handler")
    assert mythic_logger.handlers == [other]


def test_a_handler_equal_in_kind_to_the_clients_is_kept(mythic_logger):
    """Only the client's own handler object goes: another stdout StreamHandler with the client's format stays."""
    lookalike = logging.StreamHandler()
    lookalike.setFormatter(logging.Formatter(mythic_classes.LOG_FORMAT))
    mythic_logger.addHandler(lookalike)
    logging_fix.install_mythic_client_log_guard()

    client = mythic_classes.Mythic(apitoken="sage-test-token")

    assert client.log_handler is not lookalike and client.logger is mythic_logger
    assert mythic_logger.handlers == [lookalike]
