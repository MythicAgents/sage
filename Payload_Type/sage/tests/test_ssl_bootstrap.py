from __future__ import annotations

import sys
from pathlib import Path


SAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SAGE_ROOT))

from ssl_bootstrap import configure_sage_ssl_cert_file  # noqa: E402


def test_optional_ca_bootstrap_combines_system_and_custom_trust(tmp_path):
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    system_bundle = tmp_path / "system.pem"
    system_bundle.write_text("SYSTEM-CA", encoding="utf-8")
    (certs_dir / "bundle.pem").write_text("CUSTOM-CA\n", encoding="utf-8")
    environ: dict[str, str] = {}

    selected = configure_sage_ssl_cert_file(
        certs_dir=certs_dir,
        environ=environ,
        system_ca_path=system_bundle,
    )

    combined = certs_dir / "combined-bundle.pem"
    assert selected == str(combined.resolve())
    assert environ == {"SSL_CERT_FILE": str(combined.resolve())}
    assert combined.read_text(encoding="utf-8") == "SYSTEM-CA\nCUSTOM-CA\n"


def test_optional_ca_bootstrap_continues_with_system_defaults_when_custom_bundle_is_absent(tmp_path):
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (certs_dir / "combined-bundle.pem").write_text("STALE", encoding="utf-8")
    environ: dict[str, str] = {}

    selected = configure_sage_ssl_cert_file(certs_dir=certs_dir, environ=environ)

    assert selected == ""
    assert environ == {}
    assert (certs_dir / "combined-bundle.pem").read_text(encoding="utf-8") == "STALE"


def test_optional_ca_bootstrap_preserves_explicit_operator_configuration(tmp_path):
    certs_dir = tmp_path / "certs"
    certs_dir.mkdir()
    (certs_dir / "bundle.pem").write_text("CUSTOM-CA", encoding="utf-8")
    environ = {"SSL_CERT_FILE": "/operator/explicit.pem"}

    selected = configure_sage_ssl_cert_file(certs_dir=certs_dir, environ=environ)

    assert selected == "/operator/explicit.pem"
    assert environ == {"SSL_CERT_FILE": "/operator/explicit.pem"}
    assert not (certs_dir / "combined-bundle.pem").exists()
