import argparse
import socket
import ssl
import sys
import time
from collections.abc import Callable, Sequence

ENFORCED = "refused a handshake without a client certificate"
CLOSED = "closed the connection during the handshake"
ACCEPTED = "completed a handshake without a client certificate"
# TLS 1.3 alert 116: Go's server sends it when a required client certificate is missing.
CERTIFICATE_REQUIRED = "TLSV13_ALERT_CERTIFICATE_REQUIRED"


def _after_handshake(secure: ssl.SSLSocket) -> str:
    # The client never writes, so the server's first record is its verdict on the empty certificate.
    try:
        received = secure.recv(1)
    except ssl.SSLError as error:
        if error.reason == CERTIFICATE_REQUIRED:
            return ENFORCED
        return f"ended the connection after the handshake ({error.reason or error})"
    except TimeoutError:
        return ACCEPTED
    except OSError as error:
        return f"ended the connection after the handshake ({error})"
    return ACCEPTED if received else "closed the connection after the handshake"


def observe(address: str, port: int, server_name: str, cafile: str | None, wait: float) -> str:
    context = ssl.create_default_context(cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        with (
            socket.create_connection((address, port), timeout=wait) as connection,
            context.wrap_socket(connection, server_hostname=server_name) as secure,
        ):
            return _after_handshake(secure)
    except (ssl.SSLEOFError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        return CLOSED
    except ssl.SSLError as error:
        return f"failed the handshake ({error.reason or error})"
    except TimeoutError:
        return f"did not answer within {wait:g} s"
    except OSError as error:
        return f"could not be reached ({error})"


def await_enforcement(
    probe: Callable[[], str], attempts: int, interval: float, sleep: Callable[[float], None]
) -> str:
    observation = probe()
    for _ in range(attempts - 1):
        if observation == ENFORCED:
            break
        sleep(interval)
        observation = probe()
    return observation


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("server_name")
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--cafile")
    parser.add_argument("--attempts", type=int, default=20)
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--wait", type=float, default=5.0)
    options = parser.parse_args(arguments)
    observation = await_enforcement(
        lambda: observe(
            options.address, options.port, options.server_name, options.cafile, options.wait
        ),
        options.attempts,
        options.interval,
        time.sleep,
    )
    entrypoint = f"The public entrypoint {options.address}:{options.port} ({options.server_name})"
    if observation != ENFORCED:
        print(f"{entrypoint} does not enforce origin pulls: it {observation}.", file=sys.stderr)
        return 1
    print(f"{entrypoint} {ENFORCED}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
