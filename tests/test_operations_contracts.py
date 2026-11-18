import ast
import ipaddress
import itertools
import re
import runpy
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import User
from django.urls import URLPattern, URLResolver
from dramatiq.brokers.stub import StubBroker

from deploy import (
    authorize_gmail,
    backup,
    host,
    release_record,
    release_state,
    reset_database,
    runtime_secrets,
    watchdog,
)
from pyracast import maintenance
from pyracast.accounts.authentication import email_identity
from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.forecasts.historical_upload import MAX_UPLOAD_BYTES
from pyracast.forecasts.management.commands.check_delivery_health import (
    Command as DeliveryHealthCommand,
)
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
EDGE_SERVICES = {"gateway", "maintenance"}


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


def ci_jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = yaml.safe_load(text(WORKFLOWS / "ci.yml"))["jobs"]
    return jobs


def matrix_combinations(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    axes = {key: values for key, values in matrix.items() if key not in {"include", "exclude"}}
    original: list[dict[str, Any]] = []
    for values in itertools.product(*axes.values()) if axes else ():
        combination = dict(zip(axes, values, strict=True))
        if not any(
            all(combination.get(key) == value for key, value in rule.items())
            for rule in matrix.get("exclude", [])
        ):
            original.append(combination)
    added: list[dict[str, Any]] = []
    for included in matrix.get("include", []):
        extended = [
            combination
            for combination in original
            if all(combination[key] == value for key, value in included.items() if key in axes)
        ]
        for combination in extended:
            combination.update(included)
        if not extended:
            added.append(dict(included))
    return [*original, *added]


def job_names(identifier: str, job: dict[str, Any]) -> set[str]:
    matrix = job.get("strategy", {}).get("matrix")
    names: set[str] = set()
    for combination in [{}] if matrix is None else matrix_combinations(matrix):
        name = str(job.get("name", identifier))
        for key, value in combination.items():
            name = name.replace(f"${{{{ matrix.{key} }}}}", str(value))
        if "${{" in name:
            raise ValueError(f"Unsupported expression in CI job name: {name}")
        names.add(name)
    return names


def ci_job_names(jobs: dict[str, Any]) -> set[str]:
    return {name for identifier, job in jobs.items() for name in job_names(identifier, job)}


def run_scripts(commands: str) -> dict[str, str]:
    return {path: text(ROOT / path) for path in set(re.findall(r"scripts/\w+\.py", commands))}


def locked_installs(commands: str, scripts: dict[str, str]) -> tuple[str, ...]:
    direct = re.findall(r"uv sync --locked.*", commands)
    delegated = [
        path for path, source in scripts.items() if re.search(r'"sync",\s*"--locked"', source)
    ]
    return tuple(sorted({*direct, *delegated}))


def release_required_jobs(workflow: str) -> set[str]:
    listed = re.search(r"const required = \[(.*?)\];", workflow, re.S)
    if listed is None:
        raise ValueError("The release workflow lists no required CI job.")
    entries = [entry.strip() for entry in listed[1].split(",")]
    names = set()
    for entry in [*entries, *re.findall(r"required\.push\((.*?)\)", workflow)]:
        literal = re.fullmatch(r"'([^'\\]+)'", entry)
        if literal is None:
            raise ValueError(f"Unsupported required CI job entry: {entry}")
        names.add(literal[1])
    return names


def top_level_route_prefixes() -> set[str]:
    prefixes = set()
    for pattern in urlpatterns:
        assert isinstance(pattern, URLPattern | URLResolver)
        prefixes.add("/" + str(pattern.pattern).split("/", 1)[0] + "/")
    prefixes.add(settings.STATIC_URL)
    return prefixes


Route = Callable[[str, str], bool]
Token = str | tuple[str, str]
RULE_TOKEN = re.compile(r"\s*(?:(\|\||&&|!|\(|\))|(Host|Path|PathPrefix)\(`([^`]*)`\))")
MATCHERS: dict[str, Callable[[str], Route]] = {
    "Host": lambda value: lambda host, _path: host == value,
    "Path": lambda value: lambda _host, path: path == value,
    "PathPrefix": lambda value: lambda _host, path: path.startswith(value),
}


def either(left: Route, right: Route) -> Route:
    return lambda host, path: left(host, path) or right(host, path)


def both(left: Route, right: Route) -> Route:
    return lambda host, path: left(host, path) and right(host, path)


def negated(route: Route) -> Route:
    return lambda host, path: not route(host, path)


def rule_disjunction(tokens: list[Token]) -> tuple[Route, list[Token]]:
    route, tokens = rule_conjunction(tokens)
    while tokens[:1] == ["||"]:
        right, tokens = rule_conjunction(tokens[1:])
        route = either(route, right)
    return route, tokens


def rule_conjunction(tokens: list[Token]) -> tuple[Route, list[Token]]:
    route, tokens = rule_term(tokens)
    while tokens[:1] == ["&&"]:
        right, tokens = rule_term(tokens[1:])
        route = both(route, right)
    return route, tokens


def rule_term(tokens: list[Token]) -> tuple[Route, list[Token]]:
    if not tokens:
        raise ValueError("Incomplete router rule.")
    head, rest = tokens[0], tokens[1:]
    if head == "!":
        route, rest = rule_term(rest)
        return negated(route), rest
    if head == "(":
        route, rest = rule_disjunction(rest)
        if rest[:1] != [")"]:
            raise ValueError("Unbalanced router rule.")
        return route, rest[1:]
    if isinstance(head, tuple):
        return MATCHERS[head[0]](head[1]), rest
    raise ValueError(f"Unexpected router rule token: {head}")


def router_rule(rule: str) -> Route:
    tokens: list[Token] = []
    rule = rule.strip()
    position = 0
    while position < len(rule):
        match = RULE_TOKEN.match(rule, position)
        if match is None:
            raise ValueError(f"Unsupported router rule syntax: {rule[position:]}")
        tokens.append(match[1] or (match[2], match[3]))
        position = match.end()
    route, rest = rule_disjunction(tokens)
    if rest:
        raise ValueError("Unbalanced router rule.")
    return route


def backend_route() -> Route:
    routes = yaml.safe_load(text(DEPLOY / "routes.yml"))
    rule: str = routes["http"]["routers"]["backend"]["rule"]
    return router_rule(rule.replace('{{ env "SITE_ADDRESS" }}', "pyracast.example"))


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

    assert email_identity(actor.email) == actor
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

    assert stop_groups(script, cleanup=True) == drain
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


def test_gateway_body_limit_admits_the_largest_upload_and_smoke_probes_one_byte_over() -> None:
    routes = yaml.safe_load(text(DEPLOY / "routes.yml"))
    limit = routes["http"]["middlewares"]["body-limit"]["buffering"]["maxRequestBodyBytes"]

    assert limit > MAX_UPLOAD_BYTES
    assert f"head -c {limit + 1} /dev/zero" in text(DEPLOY / "smoke.sh")


def test_release_waits_only_for_jobs_that_ci_defines() -> None:
    required = release_required_jobs(text(WORKFLOWS / "release.yml"))

    assert required
    assert required <= ci_job_names(ci_jobs())


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


@pytest.mark.parametrize(
    "workflow",
    [
        "const required = ['A', \"Missing CI job\"];",
        "const required = ['A', NAME];",
        "const required = ['A'];\nrequired.push(name);",
        "const jobs = ['A'];",
    ],
)
def test_required_job_parser_refuses_entries_it_cannot_read(workflow: str) -> None:
    with pytest.raises(ValueError, match="required CI job"):
        release_required_jobs(workflow)


def test_required_job_parser_reads_conditional_additions() -> None:
    workflow = "const required = ['A',\n  'B'];\nif (x) required.push('C');"

    assert release_required_jobs(workflow) == {"A", "B", "C"}


def test_release_requires_every_ci_job_directly_or_through_needs() -> None:
    jobs = ci_jobs()
    required = release_required_jobs(text(WORKFLOWS / "release.yml"))
    direct = [key for key, job in jobs.items() if job_names(key, job) & required]
    gating: set[str] = set()
    pending = list(direct)
    while pending:
        key = pending.pop()
        if key not in gating:
            gating.add(key)
            needs = jobs[key].get("needs", [])
            pending.extend([needs] if isinstance(needs, str) else needs)

    assert all(job_names(key, jobs[key]) <= required for key in direct)
    assert gating == set(jobs)


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
    assert re.findall(r"windows-shard-\$\{\{ github\.run_attempt \}\}-(\d+)\"", combine) == [
        str(index) for index in indexes
    ]
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


def shipped_unit(name: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in text(DEPLOY / "systemd" / name).splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, value = line.split("=", 1)
            values.setdefault(key, []).append(value)
    return values


def test_release_ships_the_watchdog_and_backup_units_and_the_host_keeps_its_own() -> None:
    shipped = {path.name for path in (DEPLOY / "systemd").iterdir()}
    installed = re.search(r"^units=\((.*)\)$", text(DEPLOY / "install-units.sh"), re.M)
    template = text(TERRAFORM / "templates" / "startup.sh.tftpl")

    assert shipped == {
        "pyracast-backup.service",
        "pyracast-backup.timer",
        "pyracast-watchdog.service",
        "pyracast-watchdog.timer",
    }
    assert installed
    assert set(installed[1].split()) == shipped
    assert not any(f"/etc/systemd/system/{name}" in template for name in shipped)
    assert "cat >/etc/systemd/system/pyracast.service" in template
    assert "deploy/install-units.sh install /opt/pyracast/current" in template


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


def test_host_paths_agree_between_scripts_units_and_helpers() -> None:
    installer = text(DEPLOY / "install-release.sh")
    application = systemd_unit("pyracast.service")
    lock = host.RELEASE_LOCK.as_posix()
    blocked = host.RELEASE_BLOCKED.as_posix()

    assert f"exec 9>{lock}" in installer
    assert application["ExecStop"].startswith(f"/usr/bin/flock {lock} ")
    assert f"touch {blocked}" in installer
    assert application["ConditionPathExists"] == f"!{blocked}"
    for name in ("pyracast-backup.service", "pyracast-watchdog.service"):
        (command,) = shipped_unit(name)["ExecStart"]
        assert f" {host.CURRENT_RELEASE.as_posix()}/deploy/" in command


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


def test_failed_maintenance_names_the_recorded_prerelease_object_for_the_reset_tool() -> None:
    script = text(DEPLOY / "install-release.sh")
    deployment = text(ROOT / "docs" / "DEPLOYMENT.md")
    tool = "python3 /opt/pyracast/current/deploy/reset_database.py /etc/pyracast/host.json"
    record = f'"$release/{release_record.RECORD}"'
    dump = script.index("--kind prerelease")

    assert script.index(record) < dump < script.index(record, dump)
    assert script.index(record, dump) < script.index("run --rm --no-deps migrate\n")
    assert f"recovery='{tool}'" in script
    assert "$recovery abort" in script
    assert "$recovery restore --confirm $prerelease_object" in script
    assert f"{tool} abort" in deployment
    assert f"{tool} restore --confirm OBJECT" in deployment
    assert f"/opt/pyracast/releases/<sha>-<hash>/{release_record.RECORD}" in deployment
    assert "last-backup.json` and run" not in deployment


def test_installer_takes_the_recorded_object_only_from_the_validating_reader() -> None:
    script = text(DEPLOY / "install-release.sh")
    reader = (
        f'if ! candidate=$(python3 "$release/deploy/release_record.py" '
        f'"$release/{release_record.RECORD}"); then'
    )

    assert re.findall(r"prerelease_object=\S*", script) == [
        "prerelease_object=",
        "prerelease_object=$candidate",
    ]
    assert reader in script
    assert script.index(reader) < script.index("prerelease_object=$candidate")
    assert script.count(release_record.RECORD) == script.count(f"$release/{release_record.RECORD}")
    assert '["object"]' not in script


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
    routes = yaml.safe_load(text(DEPLOY / "routes.yml"))["http"]
    services = compose_services()
    queries = {}
    for name in ("backend", "frontend"):
        (notice,) = [
            middleware
            for middleware in routes["routers"][name]["middlewares"]
            if "errors" in routes["middlewares"][middleware]
        ]
        errors = routes["middlewares"][notice]["errors"]
        assert errors["status"] == ["502"]
        assert errors["statusRewrites"] == {"502": 503}
        assert errors["service"] == "maintenance"
        queries[name] = errors["query"]

    assert queries == {"backend": "/api", "frontend": "/page"}
    assert routes["services"]["maintenance"]["loadBalancer"]["servers"] == [
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


def test_each_release_reads_the_secrets_staged_for_it() -> None:
    compose = text(DEPLOY / "compose.yaml")
    script = installer()
    variables = set(re.findall(r"\$\{([A-Z]+_ENV_FILE):-", compose))
    written = set(re.findall(r"'([A-Z]+_ENV_FILE)': f'\{secrets\}/[a-z]+\.env'", script))

    assert variables == {
        "RUNTIME_ENV_FILE",
        "BROKER_ENV_FILE",
        "DATABASE_ENV_FILE",
        "MIGRATION_ENV_FILE",
    }
    assert written == variables
    assert release_state.SECRETS == runtime_secrets.SECRETS
    assert f'secrets="{runtime_secrets.SECRETS.as_posix()}/${{release##*/}}"' in script
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
    assert 'python3 "$release/deploy/bootstrap_database.py" converge "$release" "$secrets"' in body
    assert "/opt/pyracast/current/deploy/runtime_secrets.py" not in installer()


def test_staged_secrets_that_cannot_read_the_stored_secrets_refuse_before_maintenance() -> None:
    body = preflight()
    check = '"${compose[@]}" run --rm --no-deps migrate pyracast check_secrets || {'

    assert check in body
    refusal = body[body.index(check) : body.index("\n}\n", body.index(check))]
    assert "status=$?" in refusal
    assert "cannot read every stored secret" in refusal
    assert "unchanged" in refusal
    assert 'exit "$status"' in refusal
    assert "api pyracast check_secrets" not in installer()


def test_the_runtime_role_switches_only_after_the_serving_release_stopped() -> None:
    script = installer()
    body = script[script.index("\nbegin_maintenance\n") :]
    switch = body.index("\nswitch_runtime\nprobe_database api\n")

    assert body.index("resumable=1") < switch
    assert body.index('  "${compose[@]}" stop -t 100 api web\n') < switch
    assert switch < body.index("--kind prerelease")
    assert 'switch-runtime "$release" "$secrets" "$verifier"' in function_body(
        script, "switch_runtime"
    )
    assert 'verifier="$session/runtime-verifier"' in script


def test_a_resume_gives_the_runtime_role_its_previous_password_before_the_release_starts() -> None:
    script = installer()
    resume = function_body(script, "resume_previous")
    postgres = resume.index('"${old[@]}" up -d --wait --wait-timeout 180 postgres')
    revert = resume.index('revert-runtime "$release" "$verifier"')

    assert "if [[ -e $verifier ]]; then" in resume
    assert postgres < revert < resume.index('"${old[@]}" up -d --wait --wait-timeout 180 && ready')
    assert "restarting pyracast.service" in script


def test_a_stopped_release_leaves_the_runtime_role_on_the_secret_that_recovery_renders() -> None:
    script = installer()
    stop_path = script[
        script.index("elif (( maintenance_started == 1 )); then") : script.index(
            "trap cleanup EXIT"
        )
    ]

    assert "switch_runtime || switched=0" in stop_path
    assert "switch-runtime $release $secrets $secrets/runtime-verifier" in stop_path


def test_a_failure_before_migrate_resumes_the_previous_release() -> None:
    script = installer()
    migrate = script.index('"${compose[@]}" run --rm --no-deps migrate\n')

    assert script.index("resumable=1") < script.index("--kind prerelease") < migrate
    assert script.rindex("resumable=0", 0, migrate) > script.index('"${compose[@]}" up -d rabbitmq')
    assert '"${old[@]}" up -d --wait --wait-timeout 180' in script
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


def test_smoke_proves_the_maintenance_notice_and_the_restricted_roles() -> None:
    smoke = text(DEPLOY / "smoke.sh")

    assert "https://localhost:18443/app/) == 503 ]]" in smoke
    assert "https://localhost:18443/api/session/) == 503 ]]" in smoke
    assert f"retry-after: {maintenance.RETRY_AFTER_SECONDS}" in smoke
    assert f"== '{maintenance.api_body('en').decode()}' ]]" in smoke
    turkish = '"message": "Pyracast g\\u00fcncelleniyor.'
    assert turkish in maintenance.api_body("tr").decode()
    assert f"*'{turkish}'*" in smoke
    switched = smoke.index('"${bootstrap[@]}" switch-runtime "$temporary" "$temporary/rotated"')
    refused = smoke.index("if runtime_connects; then exit 1; fi", switched)
    reverted = smoke.index('"${bootstrap[@]}" revert-runtime "$temporary"', refused)
    assert smoke.index("\nruntime_connects\n", reverted) < smoke.index(
        "run --rm --no-deps migrate\n"
    )
    assert "INSERT INTO django_migrations" in smoke
    assert "== '42501'" in smoke
    assert "no pg_hba.conf entry" in smoke
