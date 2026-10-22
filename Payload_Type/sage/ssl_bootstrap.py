"""Configure Sage's optional custom certificate authority bundle.

The native Sage service and standalone evaluator clients share this bootstrap so they
use identical TLS trust. An operator-provided ``SSL_CERT_FILE`` wins. Otherwise,
``certs/bundle.pem`` is combined with the platform CA bundle when it exists; when it
does not exist, the process keeps the platform's normal trust configuration.
"""

from __future__ import annotations

from collections.abc import MutableMapping
import os
from pathlib import Path
import ssl


DEFAULT_SAGE_CERTS_DIR = Path(__file__).resolve().parent / "certs"
DEFAULT_SAGE_CUSTOM_CA_BUNDLE_PATH = DEFAULT_SAGE_CERTS_DIR / "bundle.pem"
DEFAULT_SAGE_COMBINED_CA_BUNDLE_PATH = DEFAULT_SAGE_CERTS_DIR / "combined-bundle.pem"


def _with_trailing_newline(value: str) -> str:
    if value and not value.endswith("\n"):
        return value + "\n"
    return value


def configure_sage_ssl_cert_file(
    *,
    certs_dir: str | Path | None = None,
    combined_bundle_path: str | Path | None = None,
    custom_bundle_path: str | Path | None = None,
    environ: MutableMapping[str, str] | None = None,
    system_ca_path: str | Path | None = None,
) -> str:
    """Select Sage's explicit or optional custom TLS trust bundle.

    Return the effective ``SSL_CERT_FILE`` path, or ``""`` when no explicit setting
    or custom bundle exists. The absence case deliberately leaves the environment
    unchanged so HTTP clients continue with their normal system trust behavior.
    """
    target_environ = os.environ if environ is None else environ
    configured = str(target_environ.get("SSL_CERT_FILE") or "").strip()
    if configured:
        return configured

    directory = Path(certs_dir) if certs_dir is not None else DEFAULT_SAGE_CERTS_DIR
    custom = Path(custom_bundle_path) if custom_bundle_path is not None else directory / "bundle.pem"
    if not custom.is_file():
        return ""

    combined = (
        Path(combined_bundle_path)
        if combined_bundle_path is not None
        else directory / "combined-bundle.pem"
    )
    platform_ca = (
        Path(system_ca_path)
        if system_ca_path is not None
        else Path(ssl.get_default_verify_paths().openssl_cafile or "")
    )
    platform_trust = platform_ca.read_text(encoding="utf-8") if platform_ca.is_file() else ""
    custom_trust = custom.read_text(encoding="utf-8")
    combined.write_text(
        _with_trailing_newline(platform_trust) + custom_trust,
        encoding="utf-8",
    )
    selected = str(combined.resolve())
    target_environ["SSL_CERT_FILE"] = selected
    return selected
