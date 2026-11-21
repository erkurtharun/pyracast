import ast
import dataclasses
import io
import ipaddress
import itertools
import os
import re
import runpy
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client
from django.urls import URLPattern, URLResolver
from dramatiq.brokers.stub import StubBroker

from deploy import (
    authorize_gmail,
    backup,
    host,
    probe_origin_pull,
    rehearsal,
    release_state,
    reset_database,
    runtime_secrets,
    watchdog,
)
from pyracast import maintenance
from pyracast.accounts.authentication import login_identity
from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.forecasts.historical_upload import MAX_UPLOAD_BYTES
from pyracast.forecasts.management.commands.check_delivery_health import (
    Command as DeliveryHealthCommand,
)
from pyracast.forecasts.management.commands.worker import Command as WorkerCommand
from pyracast.forecasts.runtime import make_actor
from pyracast.mail import gmail
from pyracast.metering.runtime import make_epias_actor
from pyracast.notifications.runtime import make_notification_actor, make_outbound_mail_actor
from pyracast.plants.runtime import make_calibration_actor
from pyracast.urls import urlpatterns
from tests.support.gateway_routes import (
    backend_route,
    middleware,
    routed_router,
    router_rule,
    routers,
    routes,
)
from tests.support.workflows import ci_job_names, ci_jobs, job_names

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
TERRAFORM = ROOT / "infra" / "terraform"
SUPPORT_SERVICES = {"postgres", "rabbitmq"}
EDGE_SERVICES = {"gateway", "maintenance"}
# POSIX only; the deployment helpers run on Linux.
NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def terraform_block(path: Path, header: str) -> str:
    match = re.search(rf"^{re.escape(header)} \{{\n(.*?)\n\}}$", text(path), re.M | re.S)
    assert match, header
    return " ".join(match[1].split())


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


def stop_groups(script: str, *, cleanup: bool) -> list[tuple[int, tuple[str, ...]]]:
    suffix = r" \|\| stopped=0" if cleanup else ""
    pattern = re.compile(rf'^\s*"\$\{{(?:old|compose)\[@\]\}}" stop -t (\d+) ([a-z ]+?){suffix}$')
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


def run_scripts(commands: str) -> dict[str, str]:
    return {path: text(ROOT / path) for path in set(re.findall(r"scripts/\w+\.py", commands))}


def locked_installs(commands: str, scripts: dict[str, str]) -> tuple[str, ...]:
    direct = re.findall(r"uv sync --locked.*", commands)
    delegated = [
        path for path, source in scripts.items() if re.search(r'"sync",\s*"--locked"', source)
    ]
    return tuple(sorted({*direct, *delegated}))


def top_level_route_prefixes() -> set[str]:
    prefixes = set()
    for pattern in urlpatterns:
        assert isinstance(pattern, URLPattern | URLResolver)
        prefixes.add("/" + str(pattern.pattern).split("/", 1)[0] + "/")
    prefixes.add(settings.STATIC_URL)
    return prefixes


def yaml_scalar(document: str, *keys: str) -> str:
    node = yaml.compose(document, Loader=yaml.SafeLoader)
    for key in keys:
        assert isinstance(node, yaml.MappingNode), key
        node = next(value for name, value in node.value if name.value == key)
    assert isinstance(node, yaml.ScalarNode)
    return str(node.value)


def test_every_actor_queue_is_consumed_by_exactly_one_compose_worker() -> None:
    broker = StubBroker()
    actors = (
        make_actor(broker),
        make_calibration_actor(broker),
        make_notification_actor(broker),
        make_outbound_mail_actor(broker),
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
def test_smoke_accepts_the_anonymous_session_contract(
    client: Client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = client.get("/api/session/")
    assert response.status_code == 200
    probe = re.search(
        r"^csrf_token=.*?python -c '\n(.*?)\n'\)", text(DEPLOY / "smoke.sh"), re.M | re.S
    )
    assert probe
    fixture = tmp_path / "smoke_session.py"
    fixture.write_text(probe[1], encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(response.content.decode()))

    runpy.run_path(str(fixture))

    assert capsys.readouterr().out.strip() == response.json()["csrf_token"]


@pytest.mark.django_db
def test_smoke_sign_in_uses_a_verified_email_identity(tmp_path: Path) -> None:
    script = text(DEPLOY / "smoke.sh")
    imports = re.search(r"^from pyracast.accounts.models import .*$", script, re.M)
    assert imports
    setup = re.search(r"^actor = .*?(?=^region = )", script, re.M | re.S)
    assert setup
    fixture = tmp_path / "smoke_identity.py"
    fixture.write_text(imports[0] + "\n" + setup[0], encoding="utf-8")
    actor: User = runpy.run_path(
        str(fixture), init_globals={"User": User, "EmailAddress": EmailAddress}
    )["actor"]

    assert login_identity(actor.email) == actor
    assert actor.has_usable_password()
    assert EmailAddress.objects.filter(
        user=actor, email=actor.email, primary=True, verified=True
    ).exists()
    membership = Membership.objects.get(user=actor)
    assert membership.role == WorkspaceRole.ADMIN
    assert membership.tenant.name == "Container smoke"


def test_release_drains_every_application_service_within_its_stop_grace_period() -> None:
    script = text(DEPLOY / "install-release.sh")
    drain = stop_groups(script, cleanup=False)
    services = compose_services()

    cleanup = stop_groups(script, cleanup=True)
    # For a previous helper that refuses to run beside it, the failure path stops the notice too.
    beside_helper = (drain[-1][0], ("gateway", "maintenance", *drain[-1][1]))
    assert [group for group in cleanup if group != beside_helper] == drain
    assert beside_helper in cleanup
    assert sorted(name for _, group in drain for name in group) == sorted(
        long_running_services() - SUPPORT_SERVICES - EDGE_SERVICES
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


def test_the_api_writes_its_security_lines_through_a_blocking_log_path() -> None:
    logging = compose_services()["api"]["logging"]

    assert logging["driver"] == "gcplogs"
    assert logging["options"]["mode"] == "blocking"
    assert compose_environment()["PYRACAST_SECURITY_LOG_PATH"] == "/proc/1/fd/1"


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

    smoke = yaml_scalar(
        text(DEPLOY / "compose.smoke.yaml"),
        "services",
        "gateway",
        "networks",
        "application",
        "ipv4_address",
    )

    assert compose_environment()["PYRACAST_TRUSTED_PROXY_CIDRS"] == f"{gateway}/32"
    assert address in ipaddress.ip_network(allocation["subnet"])
    assert address not in ipaddress.ip_network(allocation["ip_range"])
    assert f'REMOTE_ADDR="{gateway}"' in text(DEPLOY / "smoke.sh")
    assert smoke == gateway


def test_gateway_routes_every_django_prefix_and_nothing_else_to_the_backend() -> None:
    routed = backend_route()

    for prefix in top_level_route_prefixes():
        assert routed("pyracast.example", prefix + "probe"), prefix
    assert routed("pyracast.example", "/api")
    assert routed("pyracast.example", "/admin")
    assert not routed("pyracast.example", "/")
    assert not routed("pyracast.example", "/app/")
    assert not routed("pyracast.example", "/apiary")
    assert not routed("other.example", "/api/session/")


def test_router_rule_evaluation_catches_a_branch_that_can_never_match() -> None:
    routed = router_rule(
        "Host(`pyracast.example`) && "
        "(Path(`/admin`) && PathPrefix(`/admin/`) || PathPrefix(`/api/`))"
    )

    assert routed("pyracast.example", "/api/session/")
    assert not routed("pyracast.example", "/admin/login/")
    assert not router_rule("!PathPrefix(`/api/`)")("pyracast.example", "/api/session/")


@pytest.mark.parametrize(
    "rule",
    [
        "Host(`a`) && Query(`x=1`)",
        "Host(`a`) && (PathPrefix(`/api/`)",
        "Host(`a`) &&",
        "Host(`a`) )",
        "( )",
    ],
)
def test_router_rule_evaluation_refuses_syntax_it_cannot_read(rule: str) -> None:
    with pytest.raises(ValueError, match="router rule"):
        router_rule(rule)


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


def test_an_unfinished_sign_in_reservation_counts_as_long_as_the_longest_request() -> None:
    from pyracast.accounts.login_limits import REQUEST_CEILING_SECONDS

    gunicorn = runpy.run_path(str(DEPLOY / "gunicorn_conf.py"))

    assert gunicorn["timeout"] == REQUEST_CEILING_SECONDS


def body_limit(router: str) -> int:
    (limit,) = [
        middleware(name)["buffering"]["maxRequestBodyBytes"]
        for name in routers()[router]["middlewares"]
        if "buffering" in middleware(name)
    ]
    assert isinstance(limit, int)
    return limit


def test_each_router_admits_its_largest_body_and_the_smoke_probes_one_byte_over() -> None:
    smoke = text(DEPLOY / "smoke.sh")

    assert {
        name: routed_router(path)
        for name, path in [
            ("api", "/api/tenants/1/sites/"),
            ("authentication", "/api/auth/browser/v1/auth/login"),
            ("admin", "/admin/historical/"),
            ("backend", "/health/ready/"),
            ("frontend", "/app/"),
        ]
    } == {name: name for name in ("api", "authentication", "admin", "backend", "frontend")}
    assert body_limit("admin") > MAX_UPLOAD_BYTES
    assert body_limit("api") == body_limit("authentication") == settings.DATA_UPLOAD_MAX_MEMORY_SIZE
    assert body_limit("backend") == body_limit("frontend") < body_limit("api")
    probed = {
        (routed_router(path), int(size))
        for size, path in re.findall(r"\$\(too_large (\d+) (\S+)\)", smoke)
    }
    assert probed == {(router, body_limit(router) + 1) for router in routers()}


def test_ci_job_names_apply_matrix_include_and_exclude() -> None:
    jobs = {
        "scientific": {
            "name": "Scientific (${{ matrix.os }})",
            "strategy": {
                "matrix": {
                    "os": ["ubuntu-latest", "windows-latest"],
                    "exclude": [{"os": "windows-latest"}],
                    "include": [{"os": "macos-latest"}, {"os": "ubuntu-latest", "extra": "x"}],
                }
            },
        },
        "shards": {
            "name": "Shard ${{ matrix.shard }}",
            "strategy": {"matrix": {"include": [{"shard": 1}, {"shard": 2}]}},
        },
        "lint": {},
    }

    assert ci_job_names(jobs) == {
        "Scientific (ubuntu-latest)",
        "Scientific (macos-latest)",
        "Shard 1",
        "Shard 2",
        "lint",
    }
    with pytest.raises(ValueError, match="Unsupported expression"):
        ci_job_names({"job": {"name": "Job ${{ matrix.missing }}"}})


def test_ci_job_whose_matrix_excludes_every_combination_defines_no_job() -> None:
    jobs = {
        "required": {
            "name": "Required test",
            "strategy": {"matrix": {"os": ["ubuntu-latest"], "exclude": [{"os": "ubuntu-latest"}]}},
        }
    }

    assert ci_job_names(jobs) == set()


def test_windows_tests_run_in_four_shards_that_the_verify_job_combines() -> None:
    jobs = ci_jobs()
    shards = jobs["verify_windows_shards"]
    indexes = shards["strategy"]["matrix"]["shard"]
    count = len(indexes)
    partition = "\n".join(step.get("run", "") for step in shards["steps"])
    combine = "\n".join(step.get("run", "") for step in jobs["verify_windows"]["steps"])

    assert count == 4
    assert indexes == list(range(1, count + 1))
    assert job_names("verify_windows_shards", shards) == {
        f"Windows tests ({index}/{count})" for index in indexes
    }
    assert f"--shard ${{{{ env.TEST_SHARD }}}}/{count} " in partition
    assert re.findall(r"windows-shards/(\d+)\"", combine) == [str(index) for index in indexes]
    assert jobs["verify_windows"]["needs"] == "verify_windows_shards"


def test_every_ci_job_caches_exactly_the_locked_dependencies_it_installs() -> None:
    installs_by_key: dict[str, set[tuple[str, ...]]] = {}
    for key, job in ci_jobs().items():
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        scripts = run_scripts(commands)
        installs = locked_installs(commands, scripts)
        actions = {
            step["uses"].split("@", 1)[0]: step.get("with", {})
            for step in job["steps"]
            if "uses" in step
        }
        if "astral-sh/setup-uv" in actions:
            uv = actions["astral-sh/setup-uv"]
            assert uv.get("enable-cache") is bool(installs), key
            if installs:
                assert all(
                    "--no-cache" not in source for source in [commands, *scripts.values()]
                ), key
                installs_by_key.setdefault(uv.get("cache-suffix", ""), set()).add(installs)
        if "npm ci" in commands:
            assert actions["actions/setup-node"].get("cache") == "npm", key

    # One key per OS and suffix serves every job; the first job to finish saves it.
    assert all(len(installs) == 1 for installs in installs_by_key.values()), installs_by_key


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
        r"host_config_base64 = base64encode\(jsonencode\(merge\(\{(.*?)\}, \{",
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


def shipped_unit(name: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in text(DEPLOY / "systemd" / name).splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, value = line.split("=", 1)
            values.setdefault(key, []).append(value)
    return values


def test_release_ships_the_watchdog_backup_and_rehearsal_units_and_the_host_keeps_its_own() -> None:
    shipped = {path.name for path in (DEPLOY / "systemd").iterdir()}
    script = text(DEPLOY / "install-units.sh")
    installed = re.search(r"^units=\((.*)\)$", script, re.M)
    template = text(TERRAFORM / "templates" / "startup.sh.tftpl")

    assert shipped == {
        "pyracast-backup.service",
        "pyracast-backup.timer",
        "pyracast-rehearsal.service",
        "pyracast-rehearsal.timer",
        "pyracast-watchdog.service",
        "pyracast-watchdog.timer",
    }
    assert installed
    assert set(installed[1].split()) == shipped
    enabled = re.search(r"^systemctl enable --now (.*)$", script, re.M)
    assert enabled
    assert set(enabled[1].split()) == {name for name in shipped if name.endswith(".timer")}
    assert not any(f"/etc/systemd/system/{name}" in template for name in shipped)
    assert "cat >/etc/systemd/system/pyracast.service" in template
    assert "deploy/install-units.sh install /opt/pyracast/current" in template


def test_the_weekly_rehearsal_runs_detached_from_the_site_and_outlasts_its_deadline() -> None:
    service = shipped_unit("pyracast-rehearsal.service")
    timer = shipped_unit("pyracast-rehearsal.timer")

    assert service["ExecStart"] == [
        "/usr/bin/python3 /opt/pyracast/current/deploy/rehearsal.py /etc/pyracast/host.json"
    ]
    assert service["ConditionPathExists"] == ["/opt/pyracast/current/deploy/rehearsal.py"]
    assert service["PrivateTmp"] == ["true"]
    assert service["Nice"] == ["19"]
    assert service["IOSchedulingClass"] == ["idle"]
    assert int(service["TimeoutStartSec"][0]) >= rehearsal.DEADLINE_SECONDS + 300
    assert "ConditionPathExists=!/var/lib/pyracast/release-blocked" not in text(
        DEPLOY / "systemd" / "pyracast-rehearsal.service"
    )
    assert timer["OnCalendar"] == ["Sun *-*-* 03:00:00"]
    assert timer["Persistent"] == ["true"]
    assert timer["OnBootSec"] == ["1h"]
    week = 7 * 86400
    assert week + int(timer["RandomizedDelaySec"][0]) + 3600 <= host.REHEARSAL_MAX_AGE_SECONDS


def test_backup_protection_that_costs_money_is_off_until_the_operator_turns_it_on() -> None:
    variables = {
        name: terraform_block(TERRAFORM / "variables.tf", f'variable "{name}"')
        for name in ("backup_retention_days", "backup_daily_tier", "data_disk_snapshot_days")
    }
    bucket = terraform_block(TERRAFORM / "main.tf", 'resource "google_storage_bucket" "backup"')
    policy = terraform_block(
        TERRAFORM / "main.tf", 'resource "google_compute_resource_policy" "data_snapshots"'
    )
    attachment = terraform_block(
        TERRAFORM / "main.tf",
        'resource "google_compute_disk_resource_policy_attachment" "data_snapshots"',
    )

    assert "type = number default = 0" in variables["backup_retention_days"]
    assert "var.backup_retention_days <= 7" in variables["backup_retention_days"]
    assert "type = bool default = false" in variables["backup_daily_tier"]
    assert "type = number default = 0" in variables["data_disk_snapshot_days"]
    assert "var.data_disk_snapshot_days <= 7" in variables["data_disk_snapshot_days"]
    assert (
        'dynamic "retention_policy" { for_each = var.backup_retention_days > 0 ? '
        "[var.backup_retention_days] : [] content { retention_period = retention_policy.value "
        "* 86400 is_locked = false } }"
    ) in bucket
    assert (
        'dynamic "lifecycle_rule" { for_each = var.backup_daily_tier ? [30] : [] content { '
        'condition { age = lifecycle_rule.value with_state = "ANY" matches_prefix = ["daily/"] } '
        'action { type = "Delete" } } }'
    ) in bucket
    assert "soft_delete_policy { retention_duration_seconds = 0 }" in bucket
    assert policy.startswith("count = var.data_disk_snapshot_days > 0 ? 1 : 0 ")
    assert "max_retention_days = var.data_disk_snapshot_days" in policy
    assert 'on_source_disk_delete = "APPLY_RETENTION_POLICY"' in policy
    assert "storage_locations = [var.region]" in policy
    assert attachment.startswith("count = var.data_disk_snapshot_days > 0 ? 1 : 0 ")
    assert "disk = google_compute_disk.data.name" in attachment
    assert "resource_policies" not in terraform_block(
        TERRAFORM / "main.tf", 'resource "google_compute_disk" "data"'
    )


def test_the_host_copies_daily_backups_only_when_the_daily_tier_keeps_them() -> None:
    main = text(TERRAFORM / "main.tf")

    assert (
        "}, { for key, value in { daily_backups = true } : key => value if var.backup_daily_tier })"
    ) in main
    assert backup.DAILY_PREFIX == "daily/"
    assert 'config.get("daily_backups") is True' in text(DEPLOY / "backup.py")


def test_watchdog_unit_outlasts_its_deadline_and_runs_while_a_release_is_blocked() -> None:
    unit = shipped_unit("pyracast-watchdog.service")
    application = systemd_unit("pyracast.service")
    drain = max(
        timeout for timeout, _ in stop_groups(text(DEPLOY / "install-release.sh"), cleanup=False)
    )

    assert int(unit["TimeoutStartSec"][0]) >= watchdog.DEADLINE_SECONDS + 60
    assert unit["ConditionPathExists"] == ["/opt/pyracast/current/deploy/watchdog.py"]
    assert int(application["TimeoutStopSec"]) > watchdog.DEADLINE_SECONDS + drain


def test_hourly_backup_unit_leaves_the_release_lock_and_block_to_the_backup() -> None:
    unit = shipped_unit("pyracast-backup.service")

    assert unit["ExecStart"] == [
        "/usr/bin/python3 /opt/pyracast/current/deploy/backup.py /etc/pyracast/host.json"
    ]
    assert unit["PrivateTmp"] == ["true"]
    assert unit["ConditionPathExists"] == ["/opt/pyracast/current/deploy/backup.py"]
    budget = int(unit["TimeoutStartSec"][0])
    assert budget > backup.HOURLY_LOCK_WAIT_SECONDS + backup.DUMP_TIMEOUT_SECONDS


def test_a_deliberately_skipped_backup_succeeds_and_every_other_exit_fails_the_unit() -> None:
    unit = shipped_unit("pyracast-backup.service")

    # systemd counts 0 and these statuses as success; everything else leaves the unit failed.
    assert unit["SuccessExitStatus"] == [str(backup.SKIPPED_EXIT_STATUS)]
    assert backup.SKIPPED_EXIT_STATUS not in (0, 1, 2, 143)
    assert text(DEPLOY / "backup.py").count("SKIPPED_EXIT_STATUS") == 2


def test_host_paths_agree_between_scripts_units_and_helpers() -> None:
    installer = text(DEPLOY / "install-release.sh")
    harness = text(DEPLOY / "test-release.sh")
    template = text(TERRAFORM / "templates" / "startup.sh.tftpl")
    application = systemd_unit("pyracast.service")
    helper = {field.name: field.default for field in dataclasses.fields(reset_database.Host)}
    lock = host.RELEASE_LOCK.as_posix()
    blocked = host.RELEASE_BLOCKED.as_posix()
    state = host.RELEASE_BLOCKED.parent.as_posix()
    mounted = template.index(f"mountpoint --quiet {state} || mount {state}\n")

    assert host.RELEASE_LOCK.parent == host.RELEASE_BLOCKED.parent
    assert f"install -d -o root -g root -m 0755 {state}\n" in template[mounted:]
    assert helper["lock"] == host.RELEASE_LOCK
    assert installer.count(lock) == 1
    assert f"exec 9>>{lock}\n" in installer
    assert template.count(lock) == 1
    assert application["ExecStop"].startswith(f"/usr/bin/flock {lock} ")
    assert application["UMask"] == "0077"
    assert f"['/usr/bin/flock', '{lock}', " in harness
    assert "/run/lock" not in installer + harness + template
    assert f"touch {blocked}" in installer
    assert application["ConditionPathExists"] == f"!{blocked}"
    for name in (
        "pyracast-backup.service",
        "pyracast-rehearsal.service",
        "pyracast-watchdog.service",
    ):
        (command,) = shipped_unit(name)["ExecStart"]
        assert f" {host.CURRENT_RELEASE.as_posix()}/deploy/" in command
    assert helper["state"] == host.RECOVERY_STATE
    assert host.RECOVERY_STATE.parent == host.LAST_REHEARSAL.parent == host.LAST_BACKUP.parent
    assert rehearsal.LOCK.parent == host.RELEASE_LOCK.parent
    assert rehearsal.LOCK != host.RELEASE_LOCK


def test_the_python_helpers_open_the_release_lock_for_root_alone_without_following_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "release.lock"
    lock.touch()
    opened: list[tuple[object, int, int]] = []
    real_open = os.open

    def recording_open(path: Path, flags: int, mode: int) -> int:
        opened.append((path, flags, mode))
        return real_open(path, flags, mode)

    free = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=lambda *_arguments: None)
    for module in (host, reset_database):
        monkeypatch.setattr(module, "importlib", SimpleNamespace(import_module=lambda _: free))
    monkeypatch.setattr(host, "RELEASE_LOCK", lock)
    helper = reset_database.Host(
        config=tmp_path / "host.json",
        release=tmp_path,
        bucket="private-backups",
        runtime_version="1",
        lock=lock,
    )
    monkeypatch.setattr(os, "open", recording_open)

    with host.release_lock(0) as locked, lock.open("a") as held:
        assert locked
        assert not host.holds_release_lock(held.fileno())
    with reset_database.release_lock(helper):
        pass

    assert opened == [(lock, os.O_WRONLY | os.O_APPEND | os.O_CREAT | NO_FOLLOW, 0o600)] * 3


def test_every_backup_generation_stays_recoverable_until_seven_days_after_its_creation() -> None:
    bucket = terraform_block(TERRAFORM / "main.tf", 'resource "google_storage_bucket" "backup"')
    rules = re.findall(r"lifecycle_rule \{ (.*?) \} \}", bucket)

    assert "versioning { enabled = true }" in bucket
    assert rules == [
        'condition { age = 7 with_state = "ANY" matches_prefix = var.backup_daily_tier ? '
        '["postgres/"] : null } action { type = "Delete"'
    ]
    assert "soft_delete_policy { retention_duration_seconds = 0 }" in bucket


def test_cloud_storage_reads_and_writes_reach_the_data_access_audit_log() -> None:
    audit = terraform_block(
        TERRAFORM / "iam.tf", 'resource "google_project_iam_audit_config" "storage"'
    )

    assert audit == (
        'project = var.project_id service = "storage.googleapis.com" '
        'audit_log_config { log_type = "DATA_READ" } audit_log_config { log_type = "DATA_WRITE" }'
    )


def test_the_application_subnet_records_sampled_flow_logs_with_all_metadata() -> None:
    subnet = terraform_block(
        TERRAFORM / "main.tf", 'resource "google_compute_subnetwork" "application"'
    )

    assert (
        'log_config { aggregation_interval = "INTERVAL_10_MIN" flow_sampling = 0.5 '
        'metadata = "INCLUDE_ALL_METADATA" }'
    ) in subnet


def test_dispatcher_is_unhealthy_once_its_heartbeat_is_older_than_the_delivery_lag() -> None:
    dispatcher = compose_services()["dispatcher"]
    heartbeat = dispatcher["command"][dispatcher["command"].index("--heartbeat-file") + 1]
    probe = dispatcher["healthcheck"]["test"][-1]
    threshold = re.search(r"> (\d+)\)$", probe)
    lag = (
        DeliveryHealthCommand()
        .create_parser("pyracast", "check_delivery_health")
        .get_default("max_lag_minutes")
    )

    assert Path(heartbeat).parent.as_posix() == dispatcher["tmpfs"][0].split(":", 1)[0]
    assert f"'{heartbeat}'" in probe
    assert threshold
    assert int(threshold[1]) == 60 * lag


def test_api_restarts_on_liveness_and_releases_wait_for_readiness() -> None:
    assert "/health/live/" in compose_services()["api"]["healthcheck"]["test"][-1]
    assert "/health/ready/" in text(DEPLOY / "install-release.sh")


def test_a_silent_watchdog_is_alerted() -> None:
    monitoring = text(TERRAFORM / "monitoring.tf")
    metric = re.search(
        r'resource "google_logging_metric" "watchdog_heartbeat" \{(.*?)\n\}', monitoring, re.S
    )
    policy = re.search(
        r'resource "google_monitoring_alert_policy" "watchdog_heartbeat" \{(.*?)\n\}',
        monitoring,
        re.S,
    )

    assert metric
    assert r"jsonPayload.event=\"pyracast_watchdog_heartbeat\"" in metric[1]
    assert policy
    assert "condition_absent" in policy[1]
    assert "google_logging_metric.watchdog_heartbeat.name" in policy[1]
    assert "pyracast_watchdog_heartbeat" in reported_events()["INFO"]


def test_release_drains_producers_and_workers_while_the_site_still_serves() -> None:
    drain = stop_groups(text(DEPLOY / "install-release.sh"), cleanup=False)

    assert [group for _, group in drain] == [
        ("dispatcher",),
        ("worker", "notifications"),
        ("api", "web"),
    ]


def test_reset_tool_stops_the_application_in_the_installers_drain_order() -> None:
    drain = stop_groups(text(DEPLOY / "install-release.sh"), cleanup=False)

    assert [(int(timeout), group) for timeout, group in reset_database.STOP_ORDER] == drain


def test_an_upgrade_records_the_maintenance_recovery_point_right_before_migrate() -> None:
    script = text(DEPLOY / "install-release.sh")
    record = script.index(
        "if [[ -n $previous ]]; then\n"
        '  python3 "$release/deploy/release_record.py" /etc/pyracast/host.json record '
        f'"$release/{host.PRERELEASE_RECORD}"\n'
        "  recorded=1\n"
        "fi\n"
        '"${compose[@]}" run --rm --no-deps migrate\n'
    )

    assert script.index("--kind prerelease") < script.index('"${compose[@]}" up -d rabbitmq')
    assert script.index('"${compose[@]}" up -d rabbitmq') < record
    assert script.count("release_record.py") == 5
    assert script.count(host.PRERELEASE_RECORD) == 2
    assert (
        f'rm -f "$release/{host.PRERELEASE_RECORD}"' in script[: script.index("--kind prerelease")]
    )


def test_a_release_keeps_no_pre_release_dump_the_held_session_does_not_name() -> None:
    script = text(DEPLOY / "install-release.sh")
    discard = 'python3 "$release/deploy/release_record.py" /etc/pyracast/host.json discard'
    path = failure_path()

    assert script.count(discard) == 2
    before = script.rindex(discard)
    assert script.index("if [[ ! -L /opt/pyracast/current ]]; then") < before
    assert before < script.index("\nbegin_maintenance\nif [[ -L /opt/pyracast/current")
    assert (
        "      if (( recorded == 0 )); then\n"
        f"        {discard} >&2 \\\n"
        "          || echo 'WARNING: pre-release files in /var/lib/pyracast/reset were not "
        "removed; see the line above.' >&2\n"
        "      fi\n"
    ) in path
    assert path.index(discard) < path.index("stop_application")


def test_a_successful_release_ends_the_maintenance_session_without_risking_the_release() -> None:
    script = text(DEPLOY / "install-release.sh")
    end = (
        'python3 "$release/deploy/release_record.py" /etc/pyracast/host.json end \\\n'
        "  || echo 'WARNING: the pre-release archives and the session in /var/lib/pyracast/reset "
        "were not removed; see the line above.' >&2\n"
    )

    assert end in script
    assert script.index("rm -f /var/lib/pyracast/release-blocked\nsync -f /var/lib/pyracast\n") < (
        script.index(end)
    )
    assert script.index("promoted=1") < script.index(end)
    assert script.index(end) < script.index('echo "Release $revision is healthy."')


def test_every_printed_recovery_reads_the_maintenance_session() -> None:
    script = text(DEPLOY / "install-release.sh")
    path = failure_path()
    printed = (
        '  elif ! python3 "$release/deploy/release_record.py" /etc/pyracast/host.json recovery >&2;'
        " then\n"
    )

    assert printed in path
    assert path.index("No release served before this one") < path.index(printed)
    for retired in (
        "$recovery",
        "prerelease_object",
        "record_unreadable",
        "migrating",
        "candidate",
    ):
        assert retired not in script
    assert "restore --confirm" not in script
    assert " abort" not in script


def test_the_printed_recovery_runs_the_helper_in_current_with_the_host_configuration(
    tmp_path: Path,
) -> None:
    config = tmp_path / "host.json"
    config.write_text('{"backup_bucket": "pyracast-backups", "runtime_secret_version": "1"}')
    deployment = text(ROOT / "docs" / "DEPLOYMENT.md")
    tool = "python3 /opt/pyracast/current/deploy/reset_database.py /etc/pyracast/host.json"

    loaded = reset_database.load_host(config)

    assert (loaded.config, loaded.release, loaded.marker) == (
        config,
        host.CURRENT_RELEASE,
        host.RELEASE_BLOCKED,
    )
    assert f"{tool} abort" in deployment
    assert f"{tool} restore --confirm OBJECT" in deployment
    assert "last-backup.json` and run" not in deployment


def test_a_release_that_cannot_get_the_lock_says_nothing_changed() -> None:
    script = text(DEPLOY / "install-release.sh")
    wait = re.search(
        r"^flock --timeout (\d+) 9 \|\| \{ echo '([^']*)' >&2; exit 1; \}$", script, re.M
    )

    assert wait
    assert "backup" in wait[2]
    assert "nothing was changed" in wait[2]
    assert script.index(wait[0]) < script.index("mkdir -p /run/pyracast")


def installer() -> str:
    return text(DEPLOY / "install-release.sh")


def test_gateway_serves_the_maintenance_notice_only_while_the_application_is_unreachable() -> None:
    services = compose_services()
    queries = {}
    for name, router in routers().items():
        (notice,) = [
            candidate
            for candidate in router["middlewares"]
            if middleware(candidate).get("errors", {}).get("status") == ["502"]
        ]
        errors = middleware(notice)["errors"]
        assert errors["statusRewrites"] == {"502": 503}
        assert errors["service"] == "maintenance"
        queries[name] = errors["query"]

    assert queries == {
        "api": "/api",
        "authentication": "/api",
        "admin": "/api",
        "backend": "/api",
        "frontend": "/page",
    }
    assert routes()["http"]["services"]["maintenance"]["loadBalancer"]["servers"] == [
        {"url": f"http://maintenance:{maintenance.PORT}"}
    ]
    assert services["gateway"]["depends_on"] == {"maintenance": {"condition": "service_healthy"}}
    assert services["maintenance"]["image"] == services["api"]["image"]
    assert "from pyracast.maintenance import main" in services["maintenance"]["command"][-1]
    assert "env_file" not in services["maintenance"]


def test_the_application_never_answers_the_status_the_gateway_reserves_for_maintenance() -> None:
    sources = "\n".join(text(path) for path in (ROOT / "src" / "pyracast").rglob("*.py"))

    assert "status=502" not in sources
    assert "HTTP_502" not in sources
    assert "status=413" not in sources
    assert "HTTP_413" not in sources
    assert ", 413" not in sources


def test_each_release_reads_the_secrets_staged_for_it() -> None:
    compose = text(DEPLOY / "compose.yaml")
    script = installer()
    variables = set(re.findall(r"\$\{([A-Z]+_ENV_FILE):-", compose))
    written = set(
        re.findall(
            r'"([A-Z]+_ENV_FILE)": f"\{secrets\}/[a-z]+\.env"',
            text(DEPLOY / "release_environment.py"),
        )
    )

    assert variables == {
        "RUNTIME_ENV_FILE",
        "BROKER_ENV_FILE",
        "DATABASE_ENV_FILE",
        "MIGRATION_ENV_FILE",
    }
    assert written == variables
    assert release_state.SECRETS == runtime_secrets.SECRETS
    assert f'secrets="{runtime_secrets.SECRETS.as_posix()}/${{release##*/}}"' in script
    assert (
        'python3 "$release/deploy/release_environment.py" /etc/pyracast/host.json '
        '"$release/deploy/release.env" "$secrets" "$backend" "$web" "$gateway" "$postgres" '
        '"$rabbitmq"\n'
    ) in script
    assert "/run/pyracast/runtime.env" not in script
    assert "environment_files" not in script


def test_the_new_gateway_answers_with_the_notice_before_api_and_web_stop() -> None:
    script = installer()
    edge = '"${compose[@]}" up -d --no-deps --wait --wait-timeout 180 maintenance gateway'
    drain = script.index('  "${old[@]}" stop -t 660 worker notifications\n')
    stop = script.index('  "${compose[@]}" stop -t 100 api web\n')
    cleanup = script.index('"${compose[@]}" stop -t 100 api web || stopped=0')

    assert drain < script.index(f"  {edge}\n") < stop
    assert script.index(f"{edge} || noticed=0") < cleanup
    assert '"${old[@]}" stop -t 100' not in script


def preflight() -> str:
    script = installer()
    return script[script.index("trap cleanup EXIT\n") : script.index("\nbegin_maintenance\n")]


def function_body(script: str, name: str) -> str:
    start = script.index(f"{name}() {{\n")
    return script[start : script.index("\n}\n", start)]


def test_preflight_changes_nothing_the_serving_release_reads() -> None:
    body = preflight()

    assert '"${old[@]}"' not in body
    assert "switch_runtime" not in body
    assert "switch-runtime" not in body
    assert "/opt/pyracast/current/deploy/runtime_secrets.py" not in body
    assert 'python3 "$release/deploy/bootstrap_database.py" converge "$release" "$secrets"' in body


def test_staged_secrets_that_cannot_read_the_stored_secrets_refuse_before_maintenance() -> None:
    body = preflight()
    check = '"${compose[@]}" run --rm --no-deps migrate pyracast check_secrets || {'

    assert check in body
    refusal = body[body.index(check) : body.index("\n}\n", body.index(check))]
    assert "status=$?" in refusal
    assert 'exit "$status"' in refusal
    assert "api pyracast check_secrets" not in installer()


def failure_path() -> str:
    script = installer()
    return script[
        script.index("elif (( maintenance_started == 1 )); then") : script.index(
            "trap cleanup EXIT"
        )
    ]


def test_the_normal_path_switches_the_runtime_role_once_the_serving_api_and_web_stopped() -> None:
    script = installer()
    body = script[script.index("\nbegin_maintenance\n") :]
    switch = body.index("\nswitch_runtime\nswitched=1\nprobe_database api\n")

    assert body.index('  "${compose[@]}" stop -t 100 api web\n') < switch
    assert switch < body.index("--kind prerelease")
    assert script.count("\nswitch_runtime\n") == 1
    assert function_body(script, "switch_runtime").endswith('switch-runtime "$release" "$secrets"')


def test_the_failure_path_switches_and_resumes_only_after_every_stop_succeeded() -> None:
    path = failure_path()
    stops = path.index("stop_application\n")
    switch = path.index("(( switched == 0 && stopped == 1 )) && switch_runtime")
    decided = path.index(
        "if (( resumable == 1 && recorded == 0 && stopped == 1 && switched == 1 )); then resume=1"
    )
    resume = path.index("(( resume == 1 )) && resume_previous")

    assert stops < switch < decided < resume
    assert path.count("switch_runtime") == 1
    assert "stopped=0" not in path
    # A resume that did not serve is stopped again before the report.
    assert path.index("stop_application\n", resume) < path.index("if (( stopped == 1 )); then")


def test_a_resume_starts_the_previous_release_as_pyracast_service_starts_it() -> None:
    resume = function_body(installer(), "resume_previous")
    unit = systemd_unit("pyracast.service")
    render = unit["ExecStartPre"].removeprefix("/usr/bin/")
    start = '"${old[@]}" up -d --wait --wait-timeout 180'

    assert (
        render == "python3 /opt/pyracast/current/deploy/runtime_secrets.py /etc/pyracast/host.json"
    )
    assert unit["ExecStart"].endswith("up --detach --wait --wait-timeout 180")
    assert resume.index(render) < resume.index(start) < resume.index("rm -f")


def test_a_failure_before_migrate_resumes_the_previous_release() -> None:
    script = installer()
    migrate = script.index('"${compose[@]}" run --rm --no-deps migrate\n')

    assert script.index("resumable=1") < script.index("--kind prerelease") < migrate
    assert script.rindex("recorded=1", 0, migrate) > script.index('"${compose[@]}" up -d rabbitmq')
    assert re.findall(r"recorded=\d", script) == ["recorded=0", "recorded=1"]
    assert "if [[ -e /var/lib/pyracast/release-blocked ]]; then blocked_before=1; fi" in script


def test_pruning_runs_after_promotion_and_never_fails_the_release() -> None:
    script = installer()
    promoted = script.index("promoted=1")
    record = script.index('release_state.py" record "$release" ${previous:+"$previous"}')
    prune = script.index('release_state.py" prune')

    assert promoted < record < prune
    assert re.search(r'release_state\.py" prune \|\| echo "WARNING', script)
    assert release_state.STATE.parent == host.RELEASE_BLOCKED.parent
    assert release_state.CURRENT == host.CURRENT_RELEASE


def test_preflight_proves_the_metadata_guard_and_the_stored_secrets_before_maintenance() -> None:
    script = installer()
    maintenance_begins = script.index("\nbegin_maintenance\n")

    assert script.index("169.254.169.254") < maintenance_begins
    assert script.index("run --rm --no-deps migrate pyracast check_secrets") < maintenance_begins
    for service in compose_services().values():
        assert "network_mode" not in service
        assert "dns" not in service


def test_every_release_converges_database_provisioning_before_and_after_migrate() -> None:
    script = installer()
    converge = 'python3 "$release/deploy/bootstrap_database.py" converge "$release" "$secrets"'
    first = script.index(converge)
    migrate = script.index('"${compose[@]}" run --rm --no-deps migrate\n')

    assert script.count(converge) == 2
    assert first < script.index("\nbegin_maintenance\n") < migrate < script.rindex(converge)
    assert "if [[ ! -L /opt/pyracast/current ]]; then\n  begin_maintenance\n" in script


def test_postgresql_admits_the_superuser_only_over_its_local_socket() -> None:
    postgres = compose_services()["postgres"]
    rules = [
        line.split()
        for line in text(DEPLOY / "postgres" / "pg_hba.conf").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert "hba_file=/etc/postgresql/pg_hba.conf" in postgres["command"]
    assert "COPY pg_hba.conf /etc/postgresql/pg_hba.conf" in text(
        DEPLOY / "postgres" / "Dockerfile"
    )
    assert rules == [
        ["local", "all", "postgres", "trust"],
        ["host", "pyracast", "pyracast_owner,pyracast_app", "all", "scram-sha-256"],
    ]
    assert "-h" not in postgres["healthcheck"]["test"]


def test_smoke_refusals_without_a_broker_name_their_cause() -> None:
    smoke = text(DEPLOY / "smoke.sh")

    assert "[[ $refused != 0 && $deploy_check == *background.E001* ]]" in smoke
    assert "[[ $refused != 0 && $missing_broker == *PYRACAST_RABBITMQ_URL* ]]" in smoke


def test_smoke_proves_the_maintenance_notice_and_the_restricted_roles() -> None:
    smoke = text(DEPLOY / "smoke.sh")

    assert "https://localhost:18443/app/) == 503 ]]" in smoke
    assert "https://localhost:18443/api/session/) == 503 ]]" in smoke
    assert f"retry-after: {maintenance.RETRY_AFTER_SECONDS}" in smoke
    assert f"== '{maintenance.api_body('en').decode()}' ]]" in smoke
    turkish = '"message": "Pyracast g\\u00fcncelleniyor.'
    assert turkish in maintenance.api_body("tr").decode()
    assert f"*'{turkish}'*" in smoke
    logged = smoke.index("ALTER SYSTEM SET log_statement = 'all'")
    switched = smoke.index('"${bootstrap[@]}" switch-runtime "$temporary" "$temporary/rotated"\n')
    refused = smoke.index("if runtime_connects; then exit 1; fi", switched)
    back = smoke.index('"${bootstrap[@]}" switch-runtime "$temporary" "$temporary"\n', refused)
    assert logged < switched
    assert smoke.index("\nruntime_connects\n", back) < smoke.index("run --rm --no-deps migrate\n")
    assert "INSERT INTO django_migrations" in smoke
    assert "== '42501'" in smoke
    assert "no pg_hba.conf entry" in smoke


def test_postgresql_collects_statement_statistics_from_its_first_start() -> None:
    command = compose_services()["postgres"]["command"]
    smoke = text(DEPLOY / "smoke.sh")
    converged = smoke.index('"${bootstrap[@]}" converge "$temporary" "$temporary"\n')

    assert "shared_preload_libraries=pg_stat_statements" in command
    assert "CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public;" in text(
        DEPLOY / "bootstrap.sql"
    )
    assert smoke.index("FROM pg_stat_statements") > converged


def test_gunicorn_keeps_idle_connections_longer_than_the_gateway_reuses_them() -> None:
    gunicorn = runpy.run_path(str(DEPLOY / "gunicorn_conf.py"))
    # Traefik closes idle backend connections after 90 s unless the gateway sets its own value.
    gateway_idle = seconds(
        gateway_environment().get(
            "TRAEFIK_SERVERSTRANSPORT_FORWARDINGTIMEOUTS_IDLECONNTIMEOUT", "90s"
        )
    )

    assert gunicorn["keepalive"] > gateway_idle


def per_second(limit: dict[str, Any]) -> float:
    period = str(limit.get("period", "1s"))
    unit = {"s": 1, "m": 60}[period[-1]]
    return float(limit["average"]) / (int(period[:-1]) * unit)


def test_every_router_limits_each_client_by_the_address_the_edge_appended() -> None:
    chains = {name: router["middlewares"] for name, router in routers().items()}

    assert chains == {
        "api": [
            "security",
            "api-refusals",
            "client-rate",
            "client-in-flight",
            "api-body-limit",
            "maintenance-api",
            "compression",
        ],
        "authentication": [
            "security",
            "api-refusals",
            "authentication-rate",
            "client-rate",
            "client-in-flight",
            "api-body-limit",
            "maintenance-api",
            "compression",
        ],
        "admin": [
            "security",
            "client-rate",
            "client-in-flight",
            "admin-body-limit",
            "maintenance-api",
            "compression",
        ],
        "backend": [
            "security",
            "client-rate",
            "client-in-flight",
            "page-body-limit",
            "maintenance-api",
            "compression",
        ],
        "frontend": [
            "security",
            "client-rate",
            "client-in-flight",
            "page-body-limit",
            "maintenance-page",
            "compression",
        ],
    }
    for name in ("client-rate", "authentication-rate", "client-in-flight"):
        (limit,) = middleware(name).values()
        assert limit["sourceCriterion"] == {"ipStrategy": {"depth": 1, "ipv6Subnet": 64}}, name
    assert per_second(middleware("authentication-rate")["rateLimit"]) < per_second(
        middleware("client-rate")["rateLimit"]
    )
    amount = middleware("client-in-flight")["inFlightReq"]["amount"]
    assert f"--env IN_FLIGHT={amount} header-probe" in text(DEPLOY / "smoke.sh")
    assert routers()["authentication"]["priority"] > routers()["api"]["priority"]


def test_the_smoke_spends_each_routers_own_budget_and_finds_the_next_router_untouched() -> None:
    smoke = text(DEPLOY / "smoke.sh")
    client = middleware("client-rate")["rateLimit"]
    sign_in = middleware("authentication-rate")["rateLimit"]
    in_flight = middleware("client-in-flight")["inFlightReq"]["amount"]
    budgets = {
        "RATE_AVERAGE": f"{per_second(client):g}",
        "RATE_BURST": client["burst"],
        "SIGN_IN_AVERAGE": f"{per_second(sign_in):g}",
        "SIGN_IN_BURST": sign_in["burst"],
        "IN_FLIGHT": in_flight,
    }
    start = smoke.index("ROUTERS = {\n")
    paths = dict(
        re.findall(r"^    '(\w+)': '([^']+)',$", smoke[start : smoke.index("}", start)], re.M)
    )

    assert " ".join(f"--env {name}={value}" for name, value in budgets.items()) in smoke
    assert {router: routed_router(path) for router, path in paths.items()} == {
        router: router for router in routers()
    }
    assert "for router, path in ROUTERS.items():" in smoke
    assert sign_in["burst"] < in_flight
    assert "if router != 'authentication'" in smoke


def test_a_body_over_the_api_limit_is_refused_with_the_api_error_body() -> None:
    errors = middleware("api-refusals")["errors"]
    smoke = text(DEPLOY / "smoke.sh")
    body = '{"code": "request_too_large", "message": "The request is too large to be accepted."}'

    assert errors == {"status": ["413"], "service": "maintenance", "query": "/api/413"}
    for path in ("/api/session/", "/api/auth/browser/v1/config"):
        assert (
            f"[[ $(too_large {body_limit('api') + 1} {path}) == 413 ]]\n"
            f"[[ $(<\"$temporary/too-large\") == '{body}' ]]\n"
        ) in smoke, path
    assert not [
        name
        for name, router in routers().items()
        if "api-refusals" in router["middlewares"] and not name.startswith(("api", "auth"))
    ]


def test_the_gateway_owns_the_transport_headers_and_the_applications_the_rest() -> None:
    headers = middleware("security")["headers"]
    next_config = text(ROOT / "web" / "next.config.ts")
    smoke = text(DEPLOY / "smoke.sh")

    assert {key.lower(): value for key, value in headers["customRequestHeaders"].items()} == {
        "forwarded": "",
        "x-forwarded-proto": "https",
        "x-pyracast-client-ip": "",
    }
    assert headers["customResponseHeaders"] == {
        "Server": "",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    assert headers["stsSeconds"] == settings.SECURE_HSTS_SECONDS
    assert headers["contentTypeNosniff"] is True
    assert headers["permissionsPolicy"] == "camera=(), microphone=(), geolocation=()"
    assert "referrerPolicy" not in headers
    assert "Permissions-Policy" not in next_config
    assert "X-Content-Type-Options" not in next_config
    assert '{ key: "Referrer-Policy", value: "same-origin" }' in next_config
    for path in ("/", "/app/", "/api/session/", "/admin/login/", "/health/live/"):
        assert f"exact_headers {path}\n" in smoke, path
    for header in (
        f"strict-transport-security: max-age={settings.SECURE_HSTS_SECONDS}",
        "x-content-type-options: nosniff",
        "permissions-policy: camera=(), microphone=(), geolocation=()",
        "cross-origin-opener-policy: same-origin",
        "referrer-policy: same-origin",
    ):
        assert f"'{header}'" in smoke, header


def test_the_origin_requires_the_edge_certificate_only_once_the_host_names_its_authority() -> None:
    client_auth = routes()["tls"]["options"]["edge"]["clientAuth"]
    gateway = compose_services()["gateway"]
    placeholder = text(DEPLOY / "origin-pull-off.pem")
    variables = text(TERRAFORM / "variables.tf")
    host_config = re.search(
        r"host_config_base64 = base64encode\(jsonencode\(merge\(\{(.*?)\}, \{",
        text(TERRAFORM / "main.tf"),
        re.S,
    )
    smoke = text(DEPLOY / "smoke.sh")

    assert client_auth == {
        "caFiles": ["/etc/traefik/origin-pull-ca.pem"],
        "clientAuthType": '{{ if eq (env "ORIGIN_PULL") "required" }}'
        "RequireAndVerifyClientCert{{ else }}NoClientCert{{ end }}",
    }
    assert gateway["environment"]["ORIGIN_PULL"] == "${ORIGIN_PULL:-off}"
    mount = "${ORIGIN_PULL_CA:-./origin-pull-off.pem}:/etc/traefik/origin-pull-ca.pem:ro"
    assert mount in gateway["volumes"]
    assert f"      - {mount}\n" in text(DEPLOY / "compose.smoke.yaml")
    assert placeholder.startswith("-----BEGIN CERTIFICATE-----\n")
    assert placeholder.count("-----BEGIN") == 1
    assert host_config
    assert re.search(r"^\s*origin_pull_ca\s*=\s*var\.origin_pull_ca$", host_config[1], re.M)
    assert re.search(r'variable "origin_pull_ca" \{.*?default\s*=\s*""', variables, re.S)
    assert "ORIGIN_PULL=required" in smoke
    assert smoke.index("ORIGIN_PULL=required") > smoke.index("restore --confirm")
    required = smoke[smoke.index("ORIGIN_PULL=required") :]
    proven = required.index('"${edge[@]}" https://localhost:18443/health/ready/) == ok ]]')
    assert proven < required.index("\nrefused_handshake\n")
    assert proven < required.index('\nrefused_handshake "${stranger[@]}"\n')
    assert required.rindex('"${edge[@]}" https://localhost:18443/health/ready/) == ok ]]') > (
        required.index('\nrefused_handshake "${stranger[@]}"\n')
    )
    assert "[[ $status == 35 || $status == 56 ]]" in smoke


def test_the_prerelease_backup_proves_it_runs_under_the_installers_lock() -> None:
    script = installer()

    assert f"exec 9>>{host.RELEASE_LOCK.as_posix()}\n" in script
    assert (
        'python3 "$release/deploy/backup.py" /etc/pyracast/host.json --release-dir "$release" '
        "--lock-fd 9 --kind prerelease\n"
    ) in script


def test_host_local_probes_use_the_loopback_entrypoint_without_the_edge_certificate() -> None:
    gateway = compose_services()["gateway"]
    environment = gateway["environment"]
    options = routes()["tls"]["options"]
    script = installer()
    smoke = text(DEPLOY / "smoke.sh")

    assert "${LOCAL_HTTPS_BIND:-127.0.0.1:8443}:8444" in gateway["ports"]
    assert environment["TRAEFIK_ENTRYPOINTS_LOCAL_ADDRESS"] == ":8444"
    assert environment["TRAEFIK_ENTRYPOINTS_WEBSECURE_HTTP_TLS_OPTIONS"] == "edge@file"
    assert "TRAEFIK_ENTRYPOINTS_LOCAL_HTTP_TLS_OPTIONS" not in environment
    assert options["default"] == {"minVersion": "VersionTLS12", "sniStrict": True}
    assert {name: router["entryPoints"] for name, router in routers().items()} == {
        name: ["websecure", "local"] for name in routers()
    }
    assert host.LOCAL_HTTPS_PORT == 8443
    assert f"url=https://$site_address:{host.LOCAL_HTTPS_PORT}/health/ready/" in script
    assert "&& ready /opt/pyracast/current" in script
    assert '\nready "$release"\n' in script
    assert "LOCAL_HTTPS_BIND=127.0.0.1:18444" in smoke
    assert "https://localhost:18444/health/ready/" in smoke


def shell_function(script: str, name: str) -> str:
    start = script.index(f"\n{name}() {{\n")
    return script[start : script.index("\n}\n", start)]


def test_with_origin_pulls_required_readiness_needs_an_enforcing_public_entrypoint() -> None:
    script = installer()
    gateway = '"${compose[@]}" up -d --no-deps --wait --wait-timeout 180 maintenance gateway\n'

    enforced = shell_function(script, "edge_enforced")
    assert "if grep -qsx 'ORIGIN_PULL=required' \"$1/deploy/release.env\"; then" in enforced
    assert 'python3 "$release/deploy/probe_origin_pull.py" "$site_address"' in enforced
    assert '[[ $response == ok ]] && edge_enforced "$1"' in shell_function(script, "ready")
    assert f'  {gateway}  edge_enforced "$release"\n  "${{compose[@]}}" stop -t 100 api web\n' in (
        script
    )
    assert script.index('edge_enforced "$release"\n') < script.index("recorded=1\n")
    assert script.index('edge_enforced "$release"\n') < script.index(
        '"${compose[@]}" run --rm --no-deps migrate\n'
    )


def test_the_harness_resumes_the_previous_release_when_origin_pulls_are_not_enforced() -> None:
    harness = text(DEPLOY / "test-release.sh")
    modes = re.search(r"^for MODE in (.*); do$", harness, re.M)
    resumed = re.search(r"^      (drain\|backup\|[^)]*)\)$", harness, re.M)
    released = re.search(r"^      (success\|[^)]*)\)$", harness, re.M)

    assert modes
    assert resumed
    assert released
    assert {"origin-pull", "origin-pull-open"} <= set(modes[1].split())
    assert "origin-pull-open" in resumed[1].split("|")
    assert "origin-pull" in released[1].split("|")
    assert "*/probe_origin_pull.py" in harness
    assert "/source/origin-pull-off.pem" in harness


def test_the_smoke_tells_an_enforcing_entrypoint_from_one_whose_authority_is_rejected() -> None:
    smoke = text(DEPLOY / "smoke.sh")
    required = smoke[smoke.index("ORIGIN_PULL=required") :]
    invalid = 'ORIGIN_PULL=required ORIGIN_PULL_CA="$invalid_authority"'

    assert (
        '"$root/deploy/probe_origin_pull.py" localhost --port 18443 --cafile "$temporary/ca.pem"'
        in smoke
    )
    assert required.index('"${probe_origin_pull[@]}" --attempts 1\n') < required.index(invalid)
    rejected = required[required.index(invalid) :]
    assert r"printf -- '-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n'" in required
    loopback = rejected.index("https://localhost:18444/health/ready/) == ok ]]")
    observed = rejected.index(f"[[ $refused == 1 && $observed == *'{probe_origin_pull.CLOSED}'* ]]")
    assert loopback < observed < rejected.index('refused_handshake "${edge[@]}"\n')
    assert rejected.index('refused_handshake "${edge[@]}"\n') < rejected.index("\nready\n")
