"""Provider TLS trust through Sage's real model factory, the OpenAI SDK and the adapter, on both client paths.

A local HTTPS server on 127.0.0.1 presents a certificate from a throwaway certificate authority and answers with a
recorded Responses body from the provider battery. With the bundle that ``configure_sage_ssl_cert_file`` selects for a
``certs/bundle.pem`` holding that authority, the request succeeds on the default client (the one the adapter builds;
httpx2 from openai 3) and on Sage's injected classic-httpx client. Without it, both fail certificate verification
instead of falling back to an unverified connection. Stand-ins, labelled: the server, its certificate authority and the
loopback endpoint.
"""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import json
import ssl
import threading
from pathlib import Path

import openai
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from langchain_core.messages import AIMessage

from ssl_bootstrap import configure_sage_ssl_cert_file
from tests import provider_wire_replay as replay

FIXTURE = json.loads((Path(__file__).parent / "fixtures/model_input_boundaries/provider_wire_round_trips.json")
                     .read_text(encoding="utf-8"))
ROW = next(row for row in FIXTURE["rows"]
           if "message" in row["expected"] and row["expected"]["message"]["content_types"] == ["text"])
TRUST_VARIABLES = ("SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                   "http_proxy", "https_proxy", "all_proxy")


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_authority_and_server(directory: Path) -> tuple[Path, Path, Path]:
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (x509.CertificateBuilder().subject_name(_name("Sage test authority")).issuer_name(_name("Sage test authority"))
               .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
               .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                            data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
               .sign(ca_key, hashes.SHA256()))
    server_key = ec.generate_private_key(ec.SECP256R1())
    server_cert = (x509.CertificateBuilder().subject_name(_name("127.0.0.1")).issuer_name(ca_cert.subject)
                   .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                                  critical=False)
                   .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                                  critical=False)
                   .sign(ca_key, hashes.SHA256()))
    bundle = directory / "bundle.pem"
    bundle.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_file, key_file = directory / "server.pem", directory / "server.key"
    cert_file.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    return bundle, cert_file, key_file


@pytest.fixture
def provider(tmp_path, monkeypatch):
    for name in TRUST_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    certs = tmp_path / "certs"
    certs.mkdir()
    _bundle, cert_file, key_file = _write_authority_and_server(certs)
    body = json.dumps(ROW["body"]).encode()
    paths: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server's handler name
            self.rfile.read(int(self.headers.get("content-length") or 0))
            paths.append(self.path)
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"certs": certs, "port": server.server_address[1], "paths": paths}
    finally:
        server.shutdown()
        server.server_close()


def _invoke(provider: dict, path: str, case: str):
    # One endpoint per case and path: the adapter caches its default client per endpoint, and the client's trust is
    # fixed when it is built.
    endpoint = f"https://127.0.0.1:{provider['port']}/tls-{case}-{path}/v1"
    llm = replay.model_instance(ROW, path, endpoint=endpoint)._get_base_chat_model()
    return llm.root_client._client, replay.outcome(llm)


def _verification_failure(error: BaseException) -> bool:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


@pytest.mark.parametrize("path", replay.PATHS)
def test_the_bootstrap_bundle_verifies_the_provider_on_both_client_paths(provider, path, monkeypatch):
    environment: dict[str, str] = {}
    selected = configure_sage_ssl_cert_file(certs_dir=provider["certs"], environ=environment)
    assert selected and Path(selected).name == "combined-bundle.pem"
    monkeypatch.setenv("SSL_CERT_FILE", selected)

    client, result = _invoke(provider, path, "trusted")

    assert isinstance(client, openai.DefaultHttpxClient) is (path == "default")
    assert isinstance(result, AIMessage), repr(result)
    assert replay.message_view(result)["text"] == ROW["expected"]["message"]["text"]
    assert provider["paths"] == [f"/tls-trusted-{path}/v1/responses"]


@pytest.mark.parametrize("path", replay.PATHS)
def test_without_the_bundle_both_client_paths_refuse_the_provider(provider, path):
    client, result = _invoke(provider, path, "untrusted")

    assert isinstance(client, openai.DefaultHttpxClient) is (path == "default")
    assert isinstance(result, openai.APIConnectionError), repr(result)
    assert _verification_failure(result)
    assert provider["paths"] == []
