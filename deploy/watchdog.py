# ruff: noqa: S603 -- Fixed Docker and systemd executables with argument lists; no shell interpolation.
import argparse
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import host, probe_origin_pull
from deploy.backup import compose_command
from deploy.operations_log import report

SUPPORT_SERVICES = ("postgres", "rabbitmq")
APPLICATION_SERVICES = (
    "api",
    "web",
    "worker",
    "notifications",
    "dispatcher",
    "gateway",
    "maintenance",
)
SERVICES = (*SUPPORT_SERVICES, *APPLICATION_SERVICES)
HEALTHY = ("running", "healthy")
# The unit's TimeoutStartSec leaves 90 s after this deadline for the reports.
DEADLINE_SECONDS = 210
DELIVERY_INTERVAL_SECONDS = 300
DELIVERY_CHECKED = Path("/var/lib/pyracast/delivery-checked")
MEMINFO = Path("/proc/meminfo")
# Traefik renews at one third of the lifetime; below a quarter its renewal has failed for days.
CERTIFICATE_WARNING_FRACTION = 0.25
ORIGIN_PULL_WAIT_SECONDS = 5.0


class Run:
    def __init__(self) -> None:
        self.expires = time.monotonic() + DEADLINE_SECONDS
        self.failed: list[str] = []

    def remaining(self) -> float:
        return self.expires - time.monotonic()

    def command(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        remaining = self.remaining()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        return subprocess.run(command, text=True, timeout=remaining, **options)

    def step(self, name: str, check: Callable[..., Any], *arguments: Any) -> Any:
        try:
            return check(*arguments)
        except subprocess.TimeoutExpired:
            self.failed.append(name)
            report("pyracast_runtime_unhealthy", "ERROR", step=name, outcome="timeout")
        except Exception as error:
            self.failed.append(name)
            report(
                "pyracast_runtime_unhealthy",
                "ERROR",
                step=name,
                outcome="error",
                error_type=type(error).__name__,
            )
        return None


def inspect_services(run: Run, compose: list[str]) -> dict[str, str]:
    listed = run.command([*compose, "ps", "--all", "--quiet"], capture_output=True, check=True)
    identifiers = listed.stdout.split()
    states = dict.fromkeys(SERVICES, "missing")
    if not identifiers:
        return states
    inspected = run.command(["docker", "inspect", *identifiers], capture_output=True, check=True)
    for container in json.loads(inspected.stdout):
        service = container["Config"]["Labels"].get("com.docker.compose.service")
        if service in states:
            state = container["State"]
            states[service] = state.get("Health", {}).get("Status", state["Status"])
    return states


def failed_services(states: dict[str, str]) -> list[str]:
    return [
        service
        for service, state in states.items()
        if state not in (*HEALTHY, "starting")
        and (service in APPLICATION_SERVICES or state == "missing")
    ]


def unit_state(run: Run) -> str:
    return run.command(
        ["systemctl", "is-active", "pyracast.service"], capture_output=True
    ).stdout.strip()


def check_capacity() -> None:
    boot_disk = shutil.disk_usage("/")
    data_disk = shutil.disk_usage("/var/lib/pyracast")
    memory = dict(line.split(":", 1) for line in MEMINFO.read_text().splitlines())
    available = int(memory["MemAvailable"].split()[0]) / int(memory["MemTotal"].split()[0])
    boot_available = boot_disk.free / boot_disk.total
    data_available = data_disk.free / data_disk.total
    if min(boot_available, data_available) < 0.15 or available < 0.1:
        report(
            "pyracast_host_capacity",
            "WARNING",
            boot_disk_free_percent=round(100 * boot_available, 1),
            data_disk_free_percent=round(100 * data_available, 1),
            memory_available_percent=round(available * 100, 1),
        )


def recorded(path: Path) -> dict[str, Any]:
    try:
        content = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return content if isinstance(content, dict) else {}


def check_backup_age() -> None:
    if host.minimum_age(recorded(host.LAST_BACKUP).get("mark")) > host.BACKUP_MAX_AGE_SECONDS:
        report(
            "pyracast_backup_failed", "ERROR", reason="last_verified_backup_older_than_90_minutes"
        )


def schedule_rehearsal() -> None:
    # A host without a record starts the rehearsal's window at the first check after the units
    # are installed; linking never replaces a record the rehearsal wrote meanwhile.
    temporary = host.LAST_REHEARSAL.with_name(f"{host.LAST_REHEARSAL.name}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps({"status": "scheduled", "mark": host.clock_mark()}))
    try:
        os.link(temporary, host.LAST_REHEARSAL)
    except FileExistsError:
        pass
    finally:
        temporary.unlink()


def check_rehearsal() -> None:
    if not host.LAST_REHEARSAL.exists():
        schedule_rehearsal()
        return
    status = recorded(host.LAST_REHEARSAL)
    if status.get("status") == "failed":
        step = str(status.get("step"))
        report("pyracast_rehearsal_unhealthy", "WARNING", state="failed", step=step)
        return
    mark = status.get("mark") if status.get("status") in ("passed", "scheduled") else None
    if host.minimum_age(mark) > host.REHEARSAL_MAX_AGE_SECONDS:
        report("pyracast_rehearsal_unhealthy", "WARNING", state="overdue")


def recover(run: Run, compose: list[str]) -> dict[str, str]:
    with host.release_lock(0) as locked:
        if not locked:
            return {"state": "lock_busy"}
        if host.RELEASE_BLOCKED.exists():
            return {"state": "blocked"}
        unit = unit_state(run)
        if unit != "active":
            return {"state": "unit_failed", "unit_state": unit}
        states = inspect_services(run, compose)
        failed = failed_services(states)
        if not failed:
            return {"state": "ok"}
        existing = [service for service in failed if states[service] != "missing"]
        if existing:
            run.command([*compose, "restart", "--timeout", "60", *existing], capture_output=True)
        wait = max(1, int(run.remaining()) - 10)
        # Without --no-deps, up also starts stopped dependencies such as PostgreSQL.
        run.command(
            [*compose, "up", "-d", "--no-deps", "--wait", "--wait-timeout", str(wait), *failed],
            capture_output=True,
        )
        unrecovered = {
            service: state
            for service, state in inspect_services(run, compose).items()
            if state not in HEALTHY
        }
    if unrecovered:
        report("pyracast_runtime_unhealthy", "ERROR", services=unrecovered)
    else:
        report("pyracast_runtime_restarted", "WARNING", services=failed)
    return {"state": "ok"}


def check_runtime(run: Run, compose: list[str]) -> dict[str, str]:
    states = inspect_services(run, compose)
    unhealthy_support = {
        service: state
        for service, state in states.items()
        if service in SUPPORT_SERVICES and state not in (*HEALTHY, "starting", "missing")
    }
    if unhealthy_support:
        report("pyracast_runtime_unhealthy", "ERROR", services=unhealthy_support)
    if not failed_services(states):
        return {"state": "ok"}
    return recover(run, compose)


def check_delivery(run: Run, compose: list[str]) -> None:
    if (
        DELIVERY_CHECKED.exists()
        and time.time() - DELIVERY_CHECKED.stat().st_mtime < DELIVERY_INTERVAL_SECONDS
    ):
        return
    DELIVERY_CHECKED.touch()
    result = run.command(
        [*compose, "exec", "-T", "api", "pyracast", "check_delivery_health"], capture_output=True
    )
    if result.returncode:
        report("pyracast_runtime_unhealthy", "ERROR", reason="delivery_health")


def certificate_validity(
    server_name: str, address: tuple[str, int], *, authority: str | None = None
) -> tuple[float, float]:
    context = ssl.create_default_context(cadata=authority)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with (
        socket.create_connection(address, timeout=10) as connection,
        context.wrap_socket(connection, server_hostname=server_name) as secure,
    ):
        certificate = secure.getpeercert() or {}
    not_before = ssl.cert_time_to_seconds(str(certificate["notBefore"]))
    not_after = ssl.cert_time_to_seconds(str(certificate["notAfter"]))
    return not_after - time.time(), not_after - not_before


def release_settings(release: Path) -> dict[str, str]:
    lines = (release / "deploy/release.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def check_certificate(release: Path) -> None:
    site = release_settings(release)["SITE_ADDRESS"]
    remaining, lifetime = certificate_validity(site, ("127.0.0.1", host.LOCAL_HTTPS_PORT))
    if remaining < lifetime * CERTIFICATE_WARNING_FRACTION:
        report(
            "pyracast_certificate_expiring", "WARNING", days_remaining=round(remaining / 86400, 1)
        )


def check_origin_pull(release: Path) -> None:
    settings = release_settings(release)
    if settings.get("ORIGIN_PULL") != "required":
        return
    observation = probe_origin_pull.observe(
        "127.0.0.1",
        host.PUBLIC_HTTPS_PORT,
        settings["SITE_ADDRESS"],
        None,
        ORIGIN_PULL_WAIT_SECONDS,
    )
    if observation != probe_origin_pull.ENFORCED:
        report("pyracast_origin_pull_unenforced", "ERROR", observation=observation)


def supervise(run: Run, release: Path) -> dict[str, str]:
    run.step("capacity", check_capacity)
    if host.RELEASE_BLOCKED.exists():
        return {"state": "blocked"}
    run.step("backup_age", check_backup_age)
    run.step("rehearsal", check_rehearsal)
    unit = run.step("unit", unit_state, run)
    if unit != "active":
        return {"state": "unit_failed", "unit_state": str(unit or "unknown")}
    compose = compose_command(release)
    heartbeat = run.step("runtime", check_runtime, run, compose) or {"state": "ok"}
    run.step("delivery", check_delivery, run, compose)
    run.step("certificate", check_certificate, release)
    run.step("origin_pull", check_origin_pull, release)
    return dict(heartbeat)


def main(arguments: Sequence[str] = ()) -> int:
    argparse.ArgumentParser(
        description="Check the production host once, restart failed application services and "
        "report the outcome."
    ).parse_args(arguments)
    run = Run()
    heartbeat = supervise(run, host.CURRENT_RELEASE)
    report("pyracast_watchdog_heartbeat", "INFO", **heartbeat)
    return 1 if run.failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
