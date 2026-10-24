"""The container entrypoint runs Sage as the owner of its service directory, not as root.

A root-run container left root-owned files in InstalledServices/sage that a non-root reinstall could not
delete. These tests drive the entrypoint's decisions with the privileged calls replaced; the real
privilege drop is checked against a built image.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sage_entrypoint as entry


class _Privileges:
    """Records privileged calls instead of making them; reports chosen paths as root-owned."""

    def __init__(self, monkeypatch, *, euid: int, root_owned: set[Path] = frozenset()):
        """Patch the entrypoint's privileged calls for one test.

        Args:
            monkeypatch: The test's pytest monkeypatch fixture; patches are undone after the test.
            euid: Effective user id the entrypoint should see.
            root_owned: Paths whose ``stat``/``lstat`` should report uid 0.
        """
        self.calls: list[tuple] = []
        self.root_owned = {Path(p) for p in root_owned}
        privileges = self

        def reporting_root_owned(real):
            """Wrap a ``Path`` stat method so the chosen paths report uid 0."""

            def stat(path, **kwargs):
                """Return the real stat result, with ``st_uid`` 0 for a chosen path."""
                result = real(path, **kwargs)
                if Path(path) not in privileges.root_owned:
                    return result
                values = list(result)
                values[4] = 0  # st_uid
                return os.stat_result(values)

            return stat

        monkeypatch.setattr(entry.os, "geteuid", lambda: euid)
        monkeypatch.setattr(entry.Path, "lstat", reporting_root_owned(Path.lstat))
        monkeypatch.setattr(entry.Path, "stat", reporting_root_owned(Path.stat))
        monkeypatch.setattr(
            entry.os, "lchown", lambda p, u, g: self.calls.append(("lchown", Path(p), u, g))
        )
        monkeypatch.setattr(
            entry.os, "chown", lambda p, u, g: self.calls.append(("chown", Path(p), u, g))
        )
        monkeypatch.setattr(
            entry.os, "setgroups", lambda groups: self.calls.append(("setgroups", tuple(groups)))
        )
        monkeypatch.setattr(entry.os, "setgid", lambda g: self.calls.append(("setgid", g)))
        monkeypatch.setattr(entry.os, "setuid", lambda u: self.calls.append(("setuid", u)))

    def named(self, name):
        """Return the recorded calls of one kind, such as ``"lchown"``, in call order."""
        return [call for call in self.calls if call[0] == name]


@pytest.fixture
def service_dir(tmp_path, monkeypatch):
    """A service directory with Sage-like runtime state, and the runtime HOME redirected into tmp_path."""
    service = tmp_path / "Mythic"
    (service / ".sage_engagement" / "decision_records").mkdir(parents=True)
    (service / ".sage_engagement" / "decision_records" / "chat_1.seal.json").write_text("{}")
    (service / "sage.db").write_text("")
    (service / "main.py").write_text("")
    monkeypatch.setattr(entry, "RUNTIME_HOME", tmp_path / "home")
    monkeypatch.delenv("HOME", raising=False)
    return service


def test_root_drops_to_the_service_directory_owner_and_hands_back_root_files(
    service_dir, monkeypatch
):
    """Started as root, the entrypoint hands back only root-owned files and drops to the directory owner."""
    owner = service_dir.stat()
    root_owned = {
        service_dir / ".sage_engagement",
        service_dir / ".sage_engagement" / "decision_records" / "chat_1.seal.json",
        service_dir / "sage.db",
    }
    privileges = _Privileges(monkeypatch, euid=0, root_owned=root_owned)

    assert entry.drop_to_owner_of(service_dir) == (owner.st_uid, owner.st_gid)

    handed_back = {call[1] for call in privileges.named("lchown")}
    assert handed_back == root_owned  # only the root-owned entries, nothing the owner already holds
    assert all(call[2:] == (owner.st_uid, owner.st_gid) for call in privileges.named("lchown"))
    # Group before user: once the uid is dropped the process could no longer change its gid.
    order = [call[0] for call in privileges.calls if call[0] in {"setgroups", "setgid", "setuid"}]
    assert order == ["setgroups", "setgid", "setuid"]
    assert privileges.named("setuid") == [("setuid", owner.st_uid)]
    assert os.environ["HOME"] == str(entry.RUNTIME_HOME)
    assert ("chown", entry.RUNTIME_HOME, owner.st_uid, owner.st_gid) in privileges.calls


def test_root_owned_service_directory_keeps_running_as_root(service_dir, monkeypatch):
    """A root-owned service directory leaves the process root and changes nothing."""
    # A root install or a Docker volume: the directory itself is root's, so nothing changes.
    privileges = _Privileges(monkeypatch, euid=0, root_owned={service_dir})

    assert entry.drop_to_owner_of(service_dir) is None
    assert privileges.calls == []
    assert "HOME" not in os.environ


def test_non_root_start_changes_nothing(service_dir, monkeypatch):
    """A container already started as a non-root user makes no privileged calls."""
    privileges = _Privileges(monkeypatch, euid=1000, root_owned={service_dir / "sage.db"})

    assert entry.drop_to_owner_of(service_dir) is None
    assert privileges.calls == []


def test_a_file_that_refuses_chown_is_skipped_not_fatal(service_dir, monkeypatch, capsys):
    """A chown refusal is reported on stderr and skipped; the other files are still handed back."""
    target = service_dir / "sage.db"
    _Privileges(monkeypatch, euid=0, root_owned={target, service_dir / "main.py"})

    def refusing_lchown(path, uid, gid):
        if Path(path) == target:
            raise PermissionError("operation not permitted")

    monkeypatch.setattr(entry.os, "lchown", refusing_lchown)

    assert entry.hand_back_root_owned(service_dir, 1000, 1000) == 1
    assert "could not hand back" in capsys.readouterr().err


def test_symlinks_are_changed_themselves_and_never_followed(tmp_path, monkeypatch):
    """A symlink in the service directory is chowned itself; its target outside is untouched."""
    service = tmp_path / "Mythic"
    service.mkdir()
    outside = tmp_path / "outside"
    (outside / "secret").mkdir(parents=True)
    (service / "link").symlink_to(outside, target_is_directory=True)
    privileges = _Privileges(
        monkeypatch,
        euid=0,
        root_owned={service / "link", outside / "secret"},
    )

    entry.hand_back_root_owned(service, 1000, 1000)

    assert {call[1] for call in privileges.named("lchown")} == {service / "link"}


def test_main_execs_the_given_command_or_main_py_by_default(monkeypatch):
    """``main`` execs ``python3 main.py`` with no arguments, otherwise the command it was given."""
    execs = []
    monkeypatch.setattr(entry, "drop_to_owner_of", lambda path: None)
    monkeypatch.setattr(entry.os, "execvp", lambda file, args: execs.append(args))

    entry.main(["sage-entrypoint"])
    entry.main(["sage-entrypoint", "python3", "-c", "pass"])

    assert execs == [["python3", "main.py"], ["python3", "-c", "pass"]]
