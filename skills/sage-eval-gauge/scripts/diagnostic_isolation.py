#!/usr/bin/env python3
"""Build an OS-contained command for local diagnostic child processes."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import re


class DiagnosticIsolationError(RuntimeError):
    pass


_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def isolated_command(
    argv: Sequence[str], *, writable_paths: Sequence[Path],
    environment: Mapping[str, str], cwd: Path, read_only_paths: Sequence[Path] = (),
    bwrap_path: Path = Path("/usr/bin/bwrap"),
) -> list[str]:
    """Return a bubblewrap command; the caller owns execution and deadlines."""
    values = list(argv)
    if (not values or any(not isinstance(item, str) or not item or "\0" in item for item in values)
            or not Path(values[0]).is_absolute()):
        raise DiagnosticIsolationError("isolated diagnostic command is invalid")
    try:
        sandbox = bwrap_path.resolve(strict=True); workdir = cwd.resolve(strict=True)
        if not sandbox.is_file() or not os.access(sandbox, os.X_OK) or not workdir.is_dir():
            raise OSError
    except OSError:
        raise DiagnosticIsolationError("diagnostic isolation boundary is unavailable") from None
    if any(not isinstance(key, str) or not isinstance(value, str)
           or not _ENVIRONMENT_KEY.fullmatch(key) or "\0" in value
           for key, value in environment.items()):
        raise DiagnosticIsolationError("isolated diagnostic environment is invalid")
    mounts: list[Path] = []
    for raw in writable_paths:
        try: path = raw.resolve(strict=True); metadata = path.stat(follow_symlinks=False)
        except OSError: raise DiagnosticIsolationError("writable diagnostic path is unavailable") from None
        if (not path.is_absolute() or path in {Path("/"), Path("/tmp")}
                or metadata.st_uid != os.getuid() or any(path == existing for existing in mounts)):
            raise DiagnosticIsolationError("writable diagnostic path is invalid")
        mounts.append(path)
    readonly: list[Path] = []
    for raw in read_only_paths:
        try: path = raw.resolve(strict=True); metadata = path.stat(follow_symlinks=False)
        except OSError: raise DiagnosticIsolationError("readonly diagnostic path is unavailable") from None
        if (not path.is_absolute() or path in {Path("/"), Path("/tmp")}
                or metadata.st_uid != os.getuid() or path in readonly or path in mounts):
            raise DiagnosticIsolationError("readonly diagnostic path is invalid")
        readonly.append(path)
    temp = Path("/tmp")
    if workdir.is_relative_to(temp) and not any(
            workdir == mount or workdir.is_relative_to(mount)
            for mount in [*mounts, *readonly] if mount.is_dir()
    ):
        raise DiagnosticIsolationError("isolated diagnostic working directory is unavailable")
    command = [str(sandbox), "--unshare-net", "--unshare-pid", "--die-with-parent",
               "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for path in sorted(readonly, key=lambda item: str(item)):
        command.extend(("--ro-bind", str(path), str(path)))
    for path in sorted(mounts, key=lambda item: str(item)):
        command.extend(("--bind", str(path), str(path)))
    command.append("--clearenv")
    for key, value in sorted(environment.items()):
        command.extend(("--setenv", key, value))
    return [*command, "--chdir", str(workdir), "--", *values]
