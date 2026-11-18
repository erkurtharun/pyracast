import contextlib
import datetime
import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from deploy import probe_origin_pull

SITE = "pyracast.example"
REQUIRES = "requires a client certificate"
ACCEPTS = "accepts any handshake"
CLOSES = "closes after the client hello"


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("site")
    now = datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SITE)])
    issued = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(SITE)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path = directory / "certificate.pem"
    path.write_bytes(issued.public_bytes(serialization.Encoding.PEM))
    return path


@contextlib.contextmanager
def entrypoint(certificate: Path, behaviour: str) -> Iterator[int]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, certificate.with_name("key.pem"))
    if behaviour == REQUIRES:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(certificate)
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.1)
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            with connection, contextlib.suppress(OSError):
                connection.settimeout(5)
                if behaviour == CLOSES:
                    connection.recv(65536)
                    continue
                with context.wrap_socket(
                    connection, server_side=True, do_handshake_on_connect=False
                ) as secure:
                    with contextlib.suppress(ssl.SSLError):
                        secure.do_handshake()
                    # Closing with unread client bytes resets the connection, and Windows then
                    # discards the alert the client has not read yet.
                    while socket.socket.recv(secure, 4096):
                        pass

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stopped.set()
        thread.join(10)
        listener.close()


def observe(certificate: Path, port: int, server_name: str = SITE, wait: float = 2.0) -> str:
    return probe_origin_pull.observe("127.0.0.1", port, server_name, str(certificate), wait)


def test_an_entrypoint_that_requires_the_edge_certificate_refuses_a_handshake_without_one(
    certificate: Path,
) -> None:
    with entrypoint(certificate, REQUIRES) as port:
        assert observe(certificate, port) == probe_origin_pull.ENFORCED


def test_an_entrypoint_whose_tls_option_was_rejected_closes_during_the_handshake(
    certificate: Path,
) -> None:
    with entrypoint(certificate, CLOSES) as port:
        assert observe(certificate, port) == probe_origin_pull.CLOSED


def test_an_entrypoint_that_completes_a_handshake_without_a_certificate_is_not_enforcing(
    certificate: Path,
) -> None:
    with entrypoint(certificate, ACCEPTS) as port:
        assert observe(certificate, port, wait=0.5) == probe_origin_pull.ACCEPTED


def test_an_entrypoint_presenting_a_certificate_for_another_name_is_not_enforcing(
    certificate: Path,
) -> None:
    with entrypoint(certificate, REQUIRES) as port:
        observation = observe(certificate, port, server_name="other.example")

    assert observation == "failed the handshake (CERTIFICATE_VERIFY_FAILED)"


def test_an_entrypoint_nothing_listens_on_is_not_enforcing(certificate: Path) -> None:
    with socket.create_server(("127.0.0.1", 0)) as unused:
        port = unused.getsockname()[1]

    observation = observe(certificate, port)

    assert observation.startswith(("could not be reached", "did not answer within"))


def test_the_probe_asks_again_until_the_entrypoint_enforces() -> None:
    observations = iter([probe_origin_pull.ACCEPTED, probe_origin_pull.CLOSED])
    sleeps: list[float] = []

    observation = probe_origin_pull.await_enforcement(
        lambda: next(observations, probe_origin_pull.ENFORCED), 5, 2.0, sleeps.append
    )

    assert observation == probe_origin_pull.ENFORCED
    assert sleeps == [2.0, 2.0]


def test_the_last_observation_is_reported_when_the_entrypoint_never_enforces() -> None:
    sleeps: list[float] = []

    observation = probe_origin_pull.await_enforcement(
        lambda: probe_origin_pull.CLOSED, 3, 1.0, sleeps.append
    )

    assert observation == probe_origin_pull.CLOSED
    assert sleeps == [1.0, 1.0]


def test_main_probes_the_public_port_on_loopback_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[tuple[str, int, str, str | None, float]] = []

    def record(address: str, port: int, server_name: str, cafile: str | None, wait: float) -> str:
        probes.append((address, port, server_name, cafile, wait))
        return probe_origin_pull.ENFORCED

    monkeypatch.setattr(probe_origin_pull, "observe", record)

    assert probe_origin_pull.main([SITE]) == 0
    assert probes == [("127.0.0.1", 443, SITE, None, 5.0)]


def test_main_passes_an_entrypoint_that_refuses_a_handshake_without_the_edge_certificate(
    certificate: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with entrypoint(certificate, REQUIRES) as port:
        status = probe_origin_pull.main(
            [SITE, "--port", str(port), "--cafile", str(certificate), "--attempts", "1"]
        )

    assert status == 0
    assert capsys.readouterr().out == (
        f"The public entrypoint 127.0.0.1:{port} ({SITE}) refused a handshake without a client "
        "certificate.\n"
    )


def test_main_fails_an_entrypoint_that_closes_during_the_handshake(
    certificate: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with entrypoint(certificate, CLOSES) as port:
        status = probe_origin_pull.main(
            [SITE, "--port", str(port), "--cafile", str(certificate), "--attempts", "1"]
        )

    assert status == 1
    assert capsys.readouterr().err == (
        f"The public entrypoint 127.0.0.1:{port} ({SITE}) does not enforce origin pulls: it "
        "closed the connection during the handshake.\n"
    )
