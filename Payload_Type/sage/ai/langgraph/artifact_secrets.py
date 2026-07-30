from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path
import stat
ADCS_CA_EXPORT_PFX_PURPOSE = "adcs-ca-export-pfx"
_SECRET_DIRNAME = "secrets"
_KEY_PREFIX = "artifact_master_"
_KEY_BYTES = 32
class ArtifactSecretError(RuntimeError): pass

def derive_password(
    state_dir: str | os.PathLike[str],
    *,
    engagement_id: str,
    purpose: str,
    canonical_host: str,
    domain: str,
) -> str:
    engagement = _text(engagement_id)
    purpose_text = _text(purpose)
    host = _text(canonical_host)
    target_domain = _text(domain)
    if not engagement or not purpose_text or not host or not target_domain:
        raise ArtifactSecretError("artifact secret derivation requires engagement, purpose, host, and domain")
    key = _load_or_create_master_key(Path(state_dir), engagement)
    message = "\x00".join(("v1", engagement, purpose_text, host, target_domain)).encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"SagePfx-v1-{token}"
def _load_or_create_master_key(state_dir: Path, engagement_id: str) -> bytes:
    _ensure_private_dir(state_dir, create=True, require_private=False)
    secret_dir = state_dir / _SECRET_DIRNAME
    _ensure_private_dir(secret_dir, create=True)
    digest = hashlib.sha256(engagement_id.encode("utf-8")).hexdigest()[:32]
    path = secret_dir / f"{_KEY_PREFIX}{digest}.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _o_nofollow(), 0o600)
    except FileExistsError:
        return _read_private_key(path)
    except OSError as exc:
        raise ArtifactSecretError(f"could not create artifact secret key: {exc}") from exc
    try:
        view = memoryview(os.urandom(_KEY_BYTES))
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ArtifactSecretError("artifact secret key write made no progress")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return _read_private_key(path)
def _read_private_key(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | _o_nofollow())
    except OSError as exc:
        raise ArtifactSecretError(f"could not open artifact secret key: {exc}") from exc
    try:
        info = os.fstat(fd)
        _validate_stat(info, expect_dir=False)
        data = os.read(fd, _KEY_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) != _KEY_BYTES:
        raise ArtifactSecretError("artifact secret key is corrupt")
    return data
def _ensure_private_dir(path: Path, *, create: bool, require_private: bool = True) -> None:
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ArtifactSecretError(f"could not create artifact secret directory: {exc}") from exc
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ArtifactSecretError(f"could not stat artifact secret directory: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ArtifactSecretError("artifact secret directory must not be a symlink")
    _validate_stat(info, expect_dir=True, require_private=require_private)
def _validate_private_file(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ArtifactSecretError(f"could not stat artifact secret key: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise ArtifactSecretError("artifact secret key must not be a symlink")
    _validate_stat(info, expect_dir=False)
def _validate_stat(info: os.stat_result, *, expect_dir: bool, require_private: bool = True) -> None:
    if expect_dir:
        if not stat.S_ISDIR(info.st_mode):
            raise ArtifactSecretError("artifact secret path is not a directory")
    elif not stat.S_ISREG(info.st_mode):
        raise ArtifactSecretError("artifact secret key is not a regular file")
    if not require_private:
        return
    if info.st_uid != os.geteuid():
        raise ArtifactSecretError("artifact secret path is not owned by the current user")
    if stat.S_IMODE(info.st_mode) != (0o700 if expect_dir else 0o600):
        raise ArtifactSecretError("artifact secret path mode is not private")
def _o_nofollow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)
def _text(value: object) -> str:
    return str(value or "").strip().casefold()
