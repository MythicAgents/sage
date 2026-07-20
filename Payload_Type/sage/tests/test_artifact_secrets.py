import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"))
import artifact_secrets  # noqa: E402

def _derive(state_dir: Path, engagement: str = "test-op", host: str = "ca01", domain: str = "lab.local") -> str:
    return artifact_secrets.derive_password(
        state_dir,
        engagement_id=engagement,
        purpose=artifact_secrets.ADCS_CA_EXPORT_PFX_PURPOSE,
        canonical_host=host,
        domain=domain,
    )

def test_durable_secret_derivation_is_cross_process_stable_and_scoped(tmp_path):
    state_dir = tmp_path / "state"
    code = (
        "import sys;sys.path.insert(0,sys.argv[1]);import artifact_secrets as a;"
        "print(a.derive_password(sys.argv[2],engagement_id=sys.argv[3],purpose=a.ADCS_CA_EXPORT_PFX_PURPOSE,"
        "canonical_host=sys.argv[4],domain=sys.argv[5]))"
    )
    args = [sys.executable, "-c", code, str(Path(__file__).resolve().parents[1] / "ai" / "langgraph"), str(state_dir)]
    first = subprocess.check_output([*args, "test-op", "ca01", "lab.local"], text=True).strip()
    second = subprocess.check_output([*args, "test-op", "ca01", "lab.local"], text=True).strip()

    assert first == second
    assert first.startswith("SagePfx-v1-")
    assert _derive(state_dir, engagement="other-op") != first
    assert _derive(state_dir, host="ca02") != first
    assert _derive(state_dir, domain="other.local") != first

def test_durable_secret_state_uses_private_modes(tmp_path):
    state_dir = tmp_path / "state"

    _derive(state_dir)

    secret_dir = state_dir / "secrets"
    key_path = next(secret_dir.glob("artifact_master_*.key"))
    assert stat.S_IMODE(secret_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert key_path.stat().st_size == 32

def test_durable_secret_creation_handles_short_writes(tmp_path, monkeypatch):
    real_write = os.write
    monkeypatch.setattr(artifact_secrets.os, "write", lambda fd, data: real_write(fd, data[:3]))
    first = _derive(tmp_path / "state")
    assert _derive(tmp_path / "state") == first

@pytest.mark.parametrize("mutator", ["symlink", "group_readable", "owner_readable", "corrupt"])
def test_durable_secret_state_rejects_unsafe_key_state(tmp_path, mutator):
    state_dir = tmp_path / "state"
    _derive(state_dir)
    key_path = next((state_dir / "secrets").glob("artifact_master_*.key"))
    if mutator == "symlink":
        replacement = tmp_path / "replacement.key"
        replacement.write_bytes(b"x" * 32)
        key_path.unlink()
        key_path.symlink_to(replacement)
    elif mutator == "group_readable":
        os.chmod(key_path, 0o640)
    elif mutator == "owner_readable":
        os.chmod(state_dir / "secrets", 0o500); os.chmod(key_path, 0o400)
    else:
        key_path.write_bytes(b"short")

    with pytest.raises(artifact_secrets.ArtifactSecretError):
        _derive(state_dir)
