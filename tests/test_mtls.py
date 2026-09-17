"""mTLS gateway->vLLM, against a real TLS handshake (PRD §5.4 item 1, §7 Phase 2).

SCOPED HONESTLY -- SEE PRD §4.8
================================
This proves the gateway's OWN wiring: given a client cert/key and a CA bundle,
`UpstreamPool` presents the former and verifies the latter, against a server
that actually enforces both. It does not, and cannot, validate the real
deployment path -- the private-subnet network policy, the platform's own CA,
cert rotation, or how DevOps provisions the pair onto each pod. PRD §4.8 is
explicit that the security review has to run against that real path; nothing
here substitutes for it.

WHY A RAW `ssl`-WRAPPED SOCKET, NOT THE STUB UNDER UVICORN
============================================================
The first version of this test ran `stub.server:app` under real `uvicorn`
with `--ssl-certfile`/`--ssl-ca-certs`/`--ssl-cert-reqs`. It reproduced a
handshake failure (`RemoteProtocolError`/connection-reset) against uvicorn's
asyncio TLS transport requiring a client certificate on this environment
(uvicorn 0.30.1, httpx 0.28.1/httpcore 1.0.9, Python 3.12) that plain `curl`
against the identical certificate pair does not hit -- an unrelated
ASGI-server compatibility question this repo has no reason to chase, since
what actually needs proving is the TLS layer itself: does the client present
its certificate, and does it verify the server's. A bare `ssl.SSLContext`
wrapping a blocking socket, in a background thread, removes uvicorn's asyncio
transport from the picture entirely -- Python's stdlib `ssl` module performs
the full handshake, including client-certificate verification, synchronously
inside `wrap_socket()`, which is the most-tested path this functionality has.
The response body is a fixed 200 OK; no ASGI app, no ties to `stub.server`'s
routes, because none of that is what this test is checking.
"""
from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
import threading

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from gateway.config import Settings
from gateway.upstream.client import UpstreamPool


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _self_signed_ca(common_name: str) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _leaf(
    ca_key: rsa.RSAPrivateKey, ca_cert: x509.Certificate, common_name: str,
    *, san: x509.SubjectAlternativeName | None = None,
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = _key()
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
    )
    if san is not None:
        builder = builder.add_extension(san, critical=False)
    cert = builder.sign(ca_key, hashes.SHA256())
    return key, cert


def _write_pem(path, *, key: rsa.RSAPrivateKey | None = None,
               cert: x509.Certificate | None = None) -> None:
    with open(path, "wb") as f:
        if key is not None:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        if cert is not None:
            f.write(cert.public_bytes(serialization.Encoding.PEM))


@pytest.fixture
def cert_chain(tmp_path):
    """A CA, a server leaf it signed (SAN=127.0.0.1), a client leaf it signed,
    and a second, unrelated CA -- for proving verification actually checks the
    issuer rather than merely requiring *a* CA bundle to be set.

    Returns a dict of path strings: ca, server_cert, server_key, client_cert,
    client_key, other_ca.
    """
    ca_key, ca_cert = _self_signed_ca("test mTLS CA")
    other_ca_key, other_ca_cert = _self_signed_ca("unrelated CA")

    san = x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))])
    server_key, server_cert = _leaf(ca_key, ca_cert, "127.0.0.1", san=san)
    client_key, client_cert = _leaf(ca_key, ca_cert, "gateway-test-client")

    paths = {
        "ca": tmp_path / "ca.crt",
        "other_ca": tmp_path / "other-ca.crt",
        "server_cert": tmp_path / "server.crt",
        "server_key": tmp_path / "server.key",
        "client_cert": tmp_path / "client.crt",
        "client_key": tmp_path / "client.key",
    }
    _write_pem(paths["ca"], cert=ca_cert)
    _write_pem(paths["other_ca"], cert=other_ca_cert)
    _write_pem(paths["server_cert"], cert=server_cert)
    _write_pem(paths["server_key"], key=server_key)
    _write_pem(paths["client_cert"], cert=client_cert)
    _write_pem(paths["client_key"], key=client_key)
    return {k: str(v) for k, v in paths.items()}


class _MTLSEchoServer:
    """A blocking, `ssl`-wrapped TCP server requiring a client certificate.

    One handler thread per connection, a fixed 200 OK response regardless of
    what was sent -- the request content is irrelevant to what this proves.
    `wrap_socket()` performs the full handshake, INCLUDING client-certificate
    verification, before returning; a handshake failure (no cert, wrong CA)
    raises `ssl.SSLError` right there, which is exactly the negative case the
    tests below want.
    """

    def __init__(self, cert_chain: dict[str, str]) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_cert_chain(cert_chain["server_cert"], cert_chain["server_key"])
        ctx.load_verify_locations(cert_chain["ca"])
        self._ctx = ctx

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self.port: int = self._sock.getsockname()[1]

        self._stop = False
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            tls = self._ctx.wrap_socket(conn, server_side=True)
        except ssl.SSLError:
            # No client cert, or one the trust store doesn't accept -- exactly
            # the negative case the handshake-requirements tests exercise.
            conn.close()
            return
        try:
            tls.recv(65536)
            body = b"OK"
            tls.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
        except OSError:
            pass
        finally:
            tls.close()

    def stop(self) -> None:
        self._stop = True
        self._sock.close()
        self._thread.join(timeout=2)


@pytest.fixture
def mtls_server(cert_chain):
    """A running `_MTLSEchoServer`. Returns (base_https_url, cert_chain).

    No readiness poll needed: the socket is bound and listening before
    `__init__` returns, unlike a spawned subprocess.
    """
    server = _MTLSEchoServer(cert_chain)
    try:
        yield f"https://127.0.0.1:{server.port}", cert_chain
    finally:
        server.stop()


def _client_ssl_context(
    cert_chain: dict[str, str], *, present_cert: bool, ca: str,
) -> ssl.SSLContext:
    """A hand-built context, deliberately not httpx's `cert=`/`verify=<str>`.

    httpx 0.28.1's `create_ssl_context` returns for a string `verify` before it
    ever applies `cert=` -- see `_upstream_ssl_context` in
    gateway/upstream/client.py, the production code this discovery landed in.
    Building the context directly is the only way these tests can control
    "cert presented" and "cert NOT presented" independently of that bug.
    """
    ctx = ssl.create_default_context(cafile=cert_chain[ca])
    if present_cert:
        ctx.load_cert_chain(cert_chain["client_cert"], cert_chain["client_key"])
    return ctx


class TestHandshakeRequirements:
    """Proves the test server itself is a faithful mTLS fixture -- if these
    fail, the "it works" test below would be meaningless.
    """

    def test_rejects_a_connection_with_no_client_certificate(self, mtls_server):
        base, cert_chain = mtls_server
        ctx = _client_ssl_context(cert_chain, present_cert=False, ca="ca")
        with httpx.Client(verify=ctx) as c, pytest.raises(httpx.TransportError):
            c.get(base, timeout=2.0)

    def test_rejects_a_server_cert_not_signed_by_the_trusted_ca(self, mtls_server):
        """verify= must be doing real chain validation, not merely requiring
        that SOME CA bundle be present."""
        base, cert_chain = mtls_server
        ctx = _client_ssl_context(cert_chain, present_cert=True, ca="other_ca")
        with httpx.Client(verify=ctx) as c, pytest.raises(httpx.TransportError):
            c.get(base, timeout=2.0)

    def test_succeeds_with_the_correct_client_cert_and_ca(self, mtls_server):
        base, cert_chain = mtls_server
        ctx = _client_ssl_context(cert_chain, present_cert=True, ca="ca")
        with httpx.Client(verify=ctx) as c:
            resp = c.get(base, timeout=2.0)
        assert resp.status_code == 200


class TestUpstreamPoolMTLS:
    """The actual gateway wiring: gateway/config.py's settings ->
    UpstreamPool.start()'s httpx.AsyncClient(cert=..., verify=...) -> a real
    handshake. Exercises the real pool, not a hand-rolled httpx client, so a
    regression in how `start()` reads settings would fail here.
    """

    async def test_pool_completes_mtls_handshake_with_correct_cert_and_ca(
        self, mtls_server, monkeypatch,
    ):
        base, cert_chain = mtls_server
        monkeypatch.setattr(
            "gateway.upstream.client.settings",
            Settings(
                UPSTREAM_CLIENT_CERT_PATH=cert_chain["client_cert"],
                UPSTREAM_CLIENT_KEY_PATH=cert_chain["client_key"],
                UPSTREAM_CA_BUNDLE_PATH=cert_chain["ca"],
            ),
        )
        pool = UpstreamPool(urls=[base])
        await pool.start()
        try:
            health = await pool.health()
            assert health == {base: True}
        finally:
            await pool.stop()

    async def test_pool_fails_closed_with_no_client_certificate_configured(
        self, mtls_server, monkeypatch,
    ):
        """The negative case matters as much as the positive one: an operator
        who forgets to set the cert/key pair must see the connection fail,
        never silently fall back to an unauthenticated one."""
        base, cert_chain = mtls_server
        monkeypatch.setattr(
            "gateway.upstream.client.settings",
            Settings(UPSTREAM_CA_BUNDLE_PATH=cert_chain["ca"]),
        )
        pool = UpstreamPool(urls=[base])
        await pool.start()
        try:
            health = await pool.health()
            assert health == {base: False}
        finally:
            await pool.stop()
