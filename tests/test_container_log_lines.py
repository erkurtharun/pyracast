import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.container_logs import shipped
from tests.support.log_filters import filter_of, matches, resources

ROOT = Path(__file__).resolve().parents[1]
APPLICATION = "/pyracast-api-1"
CONTINUATION = "\n    "
DJANGO = "import django\ndjango.setup()\n"
REQUEST = r"""
from urllib.parse import quote
from django.test import RequestFactory
request = RequestFactory().get("/api/plants/" + quote("\n" + FORGED), HTTP_HOST="localhost")
"""
GUNICORN = r"""
import datetime, runpy, types
from urllib.parse import quote
from gunicorn.config import Config
from gunicorn.glogging import Logger
from gunicorn.util import unquote_to_wsgi_str
config = Config()
for name, value in runpy.run_path("deploy/gunicorn_conf.py").items():
    if name in config.settings:
        config.set(name, value)
log = Logger(config)
raw = "/api/plants/" + quote("\n" + FORGED)
environ = {
    "REQUEST_METHOD": "GET",
    "RAW_URI": raw,
    "SERVER_PROTOCOL": "HTTP/1.1",
    "PATH_INFO": unquote_to_wsgi_str(raw),
    "QUERY_STRING": "",
}
"""
WRITERS = {
    "application line": DJANGO
    + r"""
import logging
logging.getLogger("pyracast.forecasts").warning("provider answered %s", "late\n" + FORGED)
""",
    "exception message": DJANGO
    + r"""
import logging
try:
    raise ValueError("unreadable value\n" + FORGED)
except ValueError:
    logging.getLogger("pyracast.orchestration").exception("task failed")
""",
    "request error log": DJANGO
    + REQUEST
    + r"""
from django.http import HttpResponse
from django.utils.log import log_response
try:
    raise RuntimeError("no plant at " + request.path)
except RuntimeError as error:
    response = HttpResponse(status=500)
    log_response("%s: %s", "Internal Server Error", request.path, response=response,
                 request=request, exception=error)
""",
    "api error log": DJANGO
    + REQUEST
    + r"""
from pyracast.api.errors import log_server_error
try:
    raise RuntimeError("no plant at " + request.path)
except RuntimeError as error:
    log_server_error(request, "plants", error)
""",
    "library logger": DJANGO
    + r"""
import logging
logging.getLogger("dramatiq.worker.WorkerThread").error("Failed to process %s", "m\n" + FORGED)
""",
    "warning": DJANGO
    + r"""
import warnings
warnings.warn("value\n" + FORGED, RuntimeWarning)
""",
}
GUNICORN_WRITERS = {
    "gunicorn access log": GUNICORN
    + r"""
response = types.SimpleNamespace(status="404 Not Found", headers=[], sent=0)
log.access(response, types.SimpleNamespace(headers=[]), environ, datetime.timedelta(milliseconds=5))
""",
    "gunicorn error log": GUNICORN
    + r"""
try:
    raise RuntimeError("no route for " + environ["PATH_INFO"])
except RuntimeError:
    log.exception("Error handling request %s", raw)
""",
}
LONG_LINE = (
    DJANGO
    + r"""
import logging
for pad in range(16 * 1024 - 120, 16 * 1024):
    logging.getLogger("pyracast.forecasts").warning("%s%s", "x" * pad, FORGED)
"""
)


def container_output(script: str, forged: str = "") -> tuple[str, str]:
    """stdout and stderr of a process whose logging is configured as in the application."""
    environment = {
        key: value for key, value in os.environ.items() if key != "PYRACAST_SECURITY_LOG_PATH"
    }
    result = subprocess.run(
        # The child reads its program and the text a caller would send from the environment.
        [
            sys.executable,
            "-c",
            "import os; FORGED = os.environ['FORGED_LINE']; exec(os.environ['LOG_WRITER'])",
        ],
        cwd=ROOT,
        env=environment
        | {
            "DJANGO_SETTINGS_MODULE": "tests.settings",
            "PYRACAST_LOG_LEVEL": "INFO",
            "PYTHONIOENCODING": "utf-8",
            "LOG_WRITER": script,
            "FORGED_LINE": forged,
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout, result.stderr


def security_filter() -> list[str]:
    monitoring = (ROOT / "infra" / "terraform" / "monitoring.tf").read_text(encoding="utf-8")
    sink = resources(monitoring, "google_logging_project_sink")["security"]
    return filter_of(sink, monitoring, {"var.project_id": "project"})


def counted(stdout: str, stderr: str, container: str = APPLICATION) -> list[str]:
    terms = security_filter()
    entries = shipped(stdout, container) + shipped(stderr, container)
    return [entry["jsonPayload"]["message"] for entry in entries if matches(terms, entry)]


@pytest.fixture(scope="module")
def security_line() -> str:
    stdout, stderr = container_output(
        DJANGO
        + "from pyracast.accounts.audit import SecurityEvent, record_security_event\n"
        + "record_security_event(SecurityEvent.LOGIN_LOCKED, address='someone@example.com', "
        + "client_ip='203.0.113.7')\n"
    )
    (line,) = stdout.splitlines()
    assert stderr == ""
    return line


def test_a_security_line_counts_once_from_the_application_container(security_line: str) -> None:
    assert counted(security_line + "\n", "") == [security_line]


def assert_one_line_start(script: str, security_line: str) -> None:
    stdout, stderr = container_output(script, security_line)

    assert counted(stdout, stderr) == []
    assert CONTINUATION + security_line[: security_line.index("{")] in stdout + stderr


@pytest.mark.parametrize("writer", WRITERS)
def test_a_record_starts_one_line_whatever_line_break_its_text_carries(
    writer: str, security_line: str
) -> None:
    assert_one_line_start(WRITERS[writer], security_line)


@pytest.mark.skipif(os.name == "nt", reason="Gunicorn runs only on the POSIX host")
@pytest.mark.parametrize("writer", GUNICORN_WRITERS)
def test_a_gunicorn_record_starts_one_line_whatever_the_request_path_carries(
    writer: str, security_line: str
) -> None:
    assert_one_line_start(GUNICORN_WRITERS[writer], security_line)


def test_a_record_longer_than_a_docker_line_starts_no_further_entry(security_line: str) -> None:
    stdout, stderr = container_output(LONG_LINE, security_line)

    assert counted(stdout, stderr) == []
    assert max(len(line.encode()) for line in stdout.split("\n")) < 16 * 1024
    assert stdout.replace(CONTINUATION, "").count(security_line) == 120
