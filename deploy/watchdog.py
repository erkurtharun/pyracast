# ruff: noqa: S603, S607 -- Fixed Docker executables with argument lists; no shell interpolation.
import importlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy.backup import compose_command
from deploy.operations_log import report

SERVICES = (
    "postgres",
    "rabbitmq",
    "api",
    "web",
    "worker",
    "notifications",
    "dispatcher",
    "gateway",
)


def inspect_services(compose: list[str]) -> dict[str, str]:
    identifiers = subprocess.check_output([*compose, "ps", "--all", "--quiet"], text=True).split()
    if not identifiers:
        return dict.fromkeys(SERVICES, "missing")
    containers = json.loads(subprocess.check_output(["docker", "inspect", *identifiers], text=True))
    states = dict.fromkeys(SERVICES, "missing")
    for container in containers:
        service = container["Config"]["Labels"].get("com.docker.compose.service")
        if service in states:
            state = container["State"]
            states[service] = state.get("Health", {}).get("Status", state["Status"])
    return states


def check_capacity() -> None:
    boot_disk = shutil.disk_usage("/")
    data_disk = shutil.disk_usage("/var/lib/pyracast")
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
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


def supervise(release: Path) -> None:
    if Path("/var/lib/pyracast/release-blocked").exists() or not release.is_dir():
        return
    active = subprocess.run(
        ["systemctl", "is-active", "pyracast.service"], capture_output=True, text=True
    )
    if active.stdout.strip() != "active":
        return
    compose = compose_command(release)
    states = inspect_services(compose)
    failed = [
        service
        for service, state in states.items()
        if state not in ("running", "healthy", "starting")
    ]
    if failed:
        # Reset exhausted restart budgets; Compose restores missing containers in dependency order.
        existing = [service for service in failed if states[service] != "missing"]
        if existing:
            subprocess.run([*compose, "restart", *existing], check=True, timeout=240)
        subprocess.run(
            [*compose, "up", "-d", "--wait", "--wait-timeout", "180"], check=True, timeout=240
        )
        remaining = {
            name: state
            for name, state in inspect_services(compose).items()
            if state not in ("running", "healthy")
        }
        if remaining:
            report("pyracast_runtime_unhealthy", "ERROR", services=remaining)
            raise RuntimeError("Runtime did not recover.")
        report("pyracast_runtime_restarted", "WARNING", services=failed)
    delivery = subprocess.run(
        [*compose, "exec", "-T", "api", "pyracast", "check_delivery_health"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if delivery.returncode:
        report("pyracast_runtime_unhealthy", "ERROR", reason="delivery_health")
    marker = Path("/var/lib/pyracast/last-backup.json")
    last_backup = (
        marker.stat().st_mtime
        if marker.exists()
        else (release / "deploy/release.env").stat().st_mtime
    )
    if time.time() - last_backup > 90 * 60:
        report(
            "pyracast_backup_failed", "ERROR", reason="last_verified_backup_older_than_90_minutes"
        )
    check_capacity()


def main() -> None:
    locks = importlib.import_module("fcntl")
    with Path("/run/lock/pyracast-release.lock").open("a") as lock:
        try:
            locks.flock(lock, locks.LOCK_EX | locks.LOCK_NB)
        except BlockingIOError:
            return
        supervise(Path("/opt/pyracast/current"))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        report("pyracast_runtime_unhealthy", "ERROR")
        raise SystemExit(1) from None
