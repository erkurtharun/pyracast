import ast
import ipaddress
import itertools
import re
import runpy
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.urls import URLPattern, URLResolver
from dramatiq.brokers.stub import StubBroker

from deploy import authorize_gmail, watchdog
from pyracast.accounts.authentication import email_identity
from pyracast.forecasts.historical_upload import MAX_UPLOAD_BYTES
from pyracast.forecasts.management.commands.worker import Command as WorkerCommand
from pyracast.mail import gmail
from pyracast.metering.runtime import make_epias_actor
from pyracast.notifications.runtime import make_notification_actor
from pyracast.plants.runtime import make_calibration_actor
from pyracast.runtime import make_actor
from pyracast.urls import urlpatterns

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
TERRAFORM = ROOT / "infra" / "terraform"
WORKFLOWS = ROOT / ".github" / "workflows"
SUPPORT_SERVICES = {"postgres", "rabbitmq"}


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def compose_services() -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = yaml.safe_load(text(DEPLOY / "compose.yaml"))["services"]
    return services


def compose_environment() -> dict[str, Any]:
    environment: dict[str, Any] = compose_services()["api"]["environment"]
    return environment


def long_running_services() -> set[str]:
    return {name for name, service in compose_services().items() if "profiles" not in service}


def seconds(duration: str) -> int:
    match = re.fullmatch(r"(\d+)s", duration)
    assert match, duration
    return int(match[1])


def gateway_environment() -> dict[str, str]:
    environment: dict[str, str] = compose_services()["gateway"]["environment"]
    return environment


def stop_groups(script: str, command: str) -> list[tuple[int, tuple[str, ...]]]:
    pattern = re.compile(rf'^\s*"\$\{{{command}\[@\]\}}" stop -t (\d+) ([a-z ]+?)(?: \|\| true)?$')
    return [
        (int(match[1]), tuple(match[2].split()))
        for line in script.splitlines()
        if (match := pattern.match(line))
    ]


def systemd_unit(name: str) -> dict[str, str]:
    template = text(TERRAFORM / "templates" / "startup.sh.tftpl")
    match = re.search(
        rf"cat >/etc/systemd/system/{re.escape(name)} <<'EOF'\n(.*?)\nEOF", template, re.S
    )
    assert match, name
    return dict(
        line.split("=", 1) for line in match[1].splitlines() if "=" in line and line[0] != "#"
    )


def reported_events() -> dict[str, set[str]]:
    events: dict[str, set[str]] = {}
    for path in DEPLOY.glob("*.py"):
        for node in ast.walk(ast.parse(text(path))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "report"
            ):
                event, severity, *_ = node.args
                assert isinstance(event, ast.Constant), path
                assert isinstance(severity, ast.Constant), path
                events.setdefault(str(severity.value), set()).add(str(event.value))
    return events


def ci_job_names() -> set[str]:
    jobs = yaml.safe_load(text(WORKFLOWS / "ci.yml"))["jobs"]
    names: set[str] = set()
    for job in jobs.values():
        matrix = {
            key: values
            for key, values in job.get("strategy", {}).get("matrix", {}).items()
            if key not in {"include", "exclude"}
        }
        for combination in itertools.product(*matrix.values()):
            name = job["name"]
            for key, value in zip(matrix, combination, strict=True):
                name = name.replace(f"${{{{ matrix.{key} }}}}", str(value))
            names.add(name)
    return names


def release_required_jobs() -> set[str]:
    workflow = text(WORKFLOWS / "release.yml")
    listed = re.search(r"const required = \[(.*?)\];", workflow, re.S)
    assert listed
    names = set(re.findall(r"'([^']+)'", listed[1]))
    names.update(re.findall(r"required\.push\('([^']+)'\)", workflow))
    assert names
    return names


def top_level_route_prefixes() -> set[str]:
    prefixes = set()
    for pattern in urlpatterns:
        assert isinstance(pattern, URLPattern | URLResolver)
        prefixes.add("/" + str(pattern.pattern).split("/", 1)[0] + "/")
    prefixes.add(settings.STATIC_URL)
    return prefixes


def backend_router_prefixes() -> set[str]:
    routes = yaml.safe_load(text(DEPLOY / "routes.yml"))
    rule: str = routes["http"]["routers"]["backend"]["rule"]
    return set(re.findall(r"PathPrefix\(`([^`]+)`\)", rule))


def test_every_actor_queue_is_consumed_by_exactly_one_compose_worker() -> None:
    broker = StubBroker()
    actors = (
        make_actor(broker),
        make_calibration_actor(broker),
        make_notification_actor(broker),
        make_epias_actor(broker),
    )
    queues = {actor.queue_name for actor in actors}
    consumed: Counter[str] = Counter()
    for service in compose_services().values():
        command = service.get("command", [])
        if command[:2] == ["pyracast", "worker"]:
            selected = command[command.index("--queues") + 1 :]
            consumed.update(itertools.takewhile(lambda item: not item.startswith("--"), selected))
    parser = WorkerCommand().create_parser("pyracast", "worker")
    choices = next(action.choices for action in parser._actions if action.dest == "queues")

    assert set(consumed) == queues
    assert set(consumed.values()) == {1}
    assert set(choices or ()) == queues


def test_watchdog_supervises_every_long_running_compose_service() -> None:
    assert set(watchdog.SERVICES) == long_running_services()


@pytest.mark.django_db
def test_smoke_sign_in_uses_a_verified_email_identity(tmp_path: Path) -> None:
    setup = re.search(r"^actor = .*?(?=^tenant = )", text(DEPLOY / "smoke.sh"), re.M | re.S)
    assert setup
    fixture = tmp_path / "smoke_identity.py"
    fixture.write_text(setup[0], encoding="utf-8")
    actor: User = runpy.run_path(
        str(fixture), init_globals={"User": User, "EmailAddress": EmailAddress}
    )["actor"]

    assert email_identity(actor.email) == actor
    assert actor.has_usable_password()
    assert EmailAddress.objects.filter(
        user=actor, email=actor.email, primary=True, verified=True
    ).exists()


def test_release_drains_every_application_service_within_its_stop_grace_period() -> None:
    script = text(DEPLOY / "install-release.sh")
    drain = stop_groups(script, "old")
    services = compose_services()

    assert stop_groups(script, "compose") == drain
    assert sorted(name for _, group in drain for name in group) == sorted(
        long_running_services() - SUPPORT_SERVICES
    )
    for timeout, group in drain:
        for name in group:
            assert timeout >= seconds(services[name]["stop_grace_period"]), name


def test_host_service_stops_within_every_compose_stop_grace_period() -> None:
    unit = systemd_unit("pyracast.service")
    stop_timeout = re.search(r"stop --timeout (\d+)$", unit["ExecStop"])
    assert stop_timeout
    grace = max(
        seconds(service["stop_grace_period"])
        for service in compose_services().values()
        if "stop_grace_period" in service
    )

    assert int(stop_timeout[1]) >= grace
    assert int(unit["TimeoutStopSec"]) > int(stop_timeout[1])


def test_every_alert_policy_matches_a_reported_event_and_every_error_is_alerted() -> None:
    policies = set(re.findall(r'event = "(pyracast_[a-z_]+)"', text(TERRAFORM / "monitoring.tf")))
    events = reported_events()

    assert policies
    assert policies <= {event for group in events.values() for event in group}
    assert events["ERROR"] <= policies


def test_origin_firewall_admits_exactly_the_gateway_trusted_ipv4_ranges() -> None:
    firewall = re.search(
        r'resource "google_compute_firewall" "https" \{.*?source_ranges = \[(.*?)\]',
        text(TERRAFORM / "main.tf"),
        re.S,
    )
    assert firewall
    trusted = gateway_environment()["TRAEFIK_ENTRYPOINTS_WEBSECURE_FORWARDEDHEADERS_TRUSTEDIPS"]
    upstream = compose_environment()["PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS"]
    networks = [ipaddress.ip_network(item) for item in trusted.split(",")]

    assert upstream == trusted
    assert set(re.findall(r'"([^"]+)"', firewall[1])) == {
        str(network) for network in networks if network.version == 4
    }


def test_trusted_proxy_is_the_gateway_static_address_outside_the_dynamic_range() -> None:
    gateway = compose_services()["gateway"]["networks"]["application"]["ipv4_address"]
    network = yaml.safe_load(text(DEPLOY / "compose.yaml"))["networks"]["application"]
    (allocation,) = network["ipam"]["config"]
    address = ipaddress.ip_address(gateway)

    assert compose_environment()["PYRACAST_TRUSTED_PROXY_CIDRS"] == f"{gateway}/32"
    assert address in ipaddress.ip_network(allocation["subnet"])
    assert address not in ipaddress.ip_network(allocation["ip_range"])
    assert f'REMOTE_ADDR="{gateway}"' in text(DEPLOY / "smoke.sh")


def test_gateway_routes_every_django_prefix_to_the_backend() -> None:
    assert top_level_route_prefixes() <= backend_router_prefixes()


def test_gateway_timeout_covers_gunicorn_and_grace_periods_fit_service_limits() -> None:
    gunicorn = runpy.run_path(str(DEPLOY / "gunicorn_conf.py"))
    gateway = gateway_environment()
    services = compose_services()

    assert gunicorn["timeout"] <= seconds(
        gateway["TRAEFIK_SERVERSTRANSPORT_FORWARDINGTIMEOUTS_RESPONSEHEADERTIMEOUT"]
    )
    assert gunicorn["graceful_timeout"] < seconds(services["api"]["stop_grace_period"])
    assert seconds(gateway["TRAEFIK_ENTRYPOINTS_WEBSECURE_TRANSPORT_LIFECYCLE_GRACETIMEOUT"]) < (
        seconds(services["gateway"]["stop_grace_period"])
    )


def test_gateway_body_limit_admits_the_largest_upload_and_smoke_probes_one_byte_over() -> None:
    routes = yaml.safe_load(text(DEPLOY / "routes.yml"))
    limit = routes["http"]["middlewares"]["body-limit"]["buffering"]["maxRequestBodyBytes"]

    assert limit > MAX_UPLOAD_BYTES
    assert f"head -c {limit + 1} /dev/zero" in text(DEPLOY / "smoke.sh")


def test_release_waits_only_for_jobs_that_ci_defines() -> None:
    assert release_required_jobs() <= ci_job_names()


def test_gmail_authorization_helper_matches_the_backend_configuration() -> None:
    read_by_settings = set(
        re.findall(
            r'os\.environ\.get\("(PYRACAST_GMAIL_[A-Z_]+)"', text(ROOT / "src/pyracast/settings.py")
        )
    )

    assert authorize_gmail.GMAIL_SCOPE == gmail.SEND_SCOPE
    assert authorize_gmail.OAUTH_EXCHANGE_ENDPOINT == gmail.TOKEN_URL
    assert read_by_settings == authorize_gmail.GMAIL_KEYS


def test_host_configuration_provides_every_key_the_host_scripts_read() -> None:
    written = re.search(
        r"host_config_base64 = base64encode\(jsonencode\(\{(.*?)\}\)\)",
        text(TERRAFORM / "main.tf"),
        re.S,
    )
    assert written
    provided = set(re.findall(r"^\s*([a-z_]+)\s*=", written[1], re.M))
    read = {
        key
        for path in [*DEPLOY.glob("*.py"), DEPLOY / "install-release.sh"]
        for key in re.findall(r"config\[[\"']([a-z_]+)[\"']\]", text(path))
    }

    assert read
    assert read <= provided
