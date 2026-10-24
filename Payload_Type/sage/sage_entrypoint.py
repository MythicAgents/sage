"""Container entrypoint: run Sage as the owner of its service directory, not as root.

Mythic bind-mounts the installed service directory (``InstalledServices/sage``) at ``/Mythic`` and starts
the container as root. Everything Sage writes there — ``sage.db``, ``.sage_engagement``, ``.phoenix`` — was
then root-owned on the host, so a later ``mythic-cli install`` by the unprivileged operator who owns that
directory could not delete it and left Sage uninstalled until someone ran ``sudo rm``.

The image starts here as root, hands any root-owned files already in the service directory back to the
directory's owner, drops to that owner's uid and gid, and execs the real command. When the directory is
owned by root (a root install, or a Docker volume instead of a bind mount) nothing changes and Sage keeps
running as root, exactly as before.

Standard library only: this runs before anything else in the image.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVICE_DIR = Path("/Mythic")
DEFAULT_COMMAND = ("python3", "main.py")
# HOME for the unprivileged process. The image's /root is unreadable to it, and libraries that cache under
# HOME must not write into the service directory.
RUNTIME_HOME = Path("/tmp/sage-home")


def hand_back_root_owned(root: Path, uid: int, gid: int) -> int:
    """Chown every root-owned entry under ``root`` (not following symlinks) to ``uid``:``gid``.

    Best effort: an entry that cannot be changed is reported and skipped, never fatal, so a filesystem that
    refuses chown (some desktop file-sharing layers) still starts.

    Args:
        root: Directory to walk. Symlinks inside it are changed themselves and never followed.
        uid: User id to give each root-owned entry.
        gid: Group id to give each root-owned entry.

    Returns:
        The number of entries changed.
    """
    changed = 0
    for directory, subdirs, files in os.walk(root):
        # os.walk visits each real directory once as `directory`; symlinked ones only appear in `subdirs`.
        here = Path(directory)
        links = [here / name for name in subdirs if (here / name).is_symlink()]
        for path in [here, *links, *(here / name for name in files)]:
            try:
                if path.lstat().st_uid != 0:
                    continue
                os.lchown(path, uid, gid)
                changed += 1
            except OSError as exc:
                print(
                    f"sage-entrypoint: could not hand back {path}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
    return changed


def drop_to_owner_of(service_dir: Path) -> tuple[int, int] | None:
    """As root, switch to the owner of ``service_dir`` when that owner is not root.

    Hands root-owned files in ``service_dir`` back to its owner, gives the process a writable ``HOME``,
    then drops supplementary groups, the group id, and the user id, in that order.

    Args:
        service_dir: The bind-mounted service directory whose owner Sage should run as.

    Returns:
        The ``(uid, gid)`` switched to, or ``None`` when the process keeps its current identity: it was
        not started as root, or ``service_dir`` is owned by root.
    """
    if os.geteuid() != 0:
        return None
    owner = service_dir.stat()
    if owner.st_uid == 0:
        return None
    uid, gid = owner.st_uid, owner.st_gid
    changed = hand_back_root_owned(service_dir, uid, gid)
    if changed:
        print(
            f"sage-entrypoint: handed {changed} root-owned path(s) in {service_dir} back to {uid}:{gid}",
            file=sys.stderr,
            flush=True,
        )
    RUNTIME_HOME.mkdir(parents=True, exist_ok=True)
    os.chown(RUNTIME_HOME, uid, gid)
    os.environ["HOME"] = str(RUNTIME_HOME)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    return uid, gid


def main(argv: list[str]) -> None:
    """Drop privileges when appropriate, then replace this process with the Sage command.

    Args:
        argv: The entrypoint's own argument vector. Everything after ``argv[0]`` is the command to exec;
            with no arguments, ``python3 main.py`` runs.
    """
    command = argv[1:] or list(DEFAULT_COMMAND)
    drop_to_owner_of(SERVICE_DIR)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main(sys.argv)
