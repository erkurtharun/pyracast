import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pika  # type: ignore[import-untyped]
import psutil
import psycopg

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
RABBIT_ENV = ("RABBITMQ_BASE", "RABBITMQ_CONFIG_FILE", "RABBITMQ_NODENAME", "ERLANG_HOME")


def _postgres_probe() -> str | None:
    with psycopg.connect(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ.get("PGDATABASE", "pyracast"),
        user=os.environ.get("PGUSER", "pyracast"),
        password=os.environ.get("PGPASSWORD", ""),
        connect_timeout=3,
        autocommit=True,
    ) as connection:
        connection.execute("SELECT 1")
        try:
            row = connection.execute("SHOW data_directory").fetchone()
        except psycopg.Error:
            return None
        return str(row[0]) if row else None


def _postgres_recipe(data_dir: str | None, host: str, port: int) -> dict[str, Any]:
    if sys.platform != "win32" or host not in LOCAL_HOSTS or data_dir is None:
        return {}
    try:
        data = Path(data_dir)
        pid = int((data / "postmaster.pid").read_text(encoding="utf-8").splitlines()[0])
        executable = Path(psutil.Process(pid).exe())
        pg_ctl = executable.with_name("pg_ctl.exe")
        if executable.name.lower() != "postgres.exe" or not pg_ctl.is_file():
            return {}
        return {"data_dir": str(data), "pg_ctl": str(pg_ctl), "host": host, "port": port}
    except OSError, ValueError, IndexError, psutil.Error:
        return {}


def _restart_postgres(recipe: Any, host: str, port: int) -> None:
    if (
        sys.platform != "win32"
        or host not in LOCAL_HOSTS
        or not isinstance(recipe, dict)
        or recipe.get("host") != host
        or recipe.get("port") != port
    ):
        raise RuntimeError("PostgreSQL is unavailable; start the existing configured database.")
    data = Path(str(recipe.get("data_dir", "")))
    pg_ctl = Path(str(recipe.get("pg_ctl", "")))
    if not (data / "PG_VERSION").is_file() or not pg_ctl.is_file():
        raise RuntimeError("The recorded PostgreSQL files are missing; no database was created.")
    log = Path(".dev/postgres.log").resolve()
    log.parent.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(
        (str(pg_ctl.parent), str(pg_ctl.parent.parent / "lib"), env.get("PATH", ""))
    )
    try:
        subprocess.run(  # noqa: S603 -- recorded existing local PostgreSQL executable.
            [
                str(pg_ctl),
                "-D",
                str(data),
                "-l",
                str(log),
                "-o",
                f"-h {host} -p {port}",
                "-w",
                "-t",
                "30",
                "start",
            ],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=40,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError, subprocess.SubprocessError:
        raise RuntimeError("The existing PostgreSQL database could not be restarted.") from None


def _ensure_postgres(state: dict[str, Any]) -> None:
    host = os.environ.get("PGHOST", "127.0.0.1")
    port = int(os.environ.get("PGPORT", "5432"))
    try:
        data_dir = _postgres_probe()
    except psycopg.Error:
        _restart_postgres(state.get("postgres"), host, port)
        try:
            data_dir = _postgres_probe()
        except psycopg.Error:
            raise RuntimeError("PostgreSQL did not become available after restart.") from None
    state["postgres"] = _postgres_recipe(data_dir, host, port)


def _rabbit_parameters() -> Any:
    parameters = pika.URLParameters(
        os.environ.get("PYRACAST_RABBITMQ_URL", "amqp://guest:guest@127.0.0.1:5672/%2F")
    )
    parameters.socket_timeout = 3
    parameters.stack_timeout = 5
    parameters.connection_attempts = 1
    parameters.blocked_connection_timeout = 3
    return parameters


def _rabbit_ready(parameters: Any) -> bool:
    try:
        connection = pika.BlockingConnection(parameters)
        connection.close()
    except pika.exceptions.AMQPError, OSError:
        return False
    return True


def _rabbit_recipe(host: str, port: int) -> dict[str, Any]:
    if sys.platform != "win32" or host not in LOCAL_HOSTS:
        return {}
    try:
        for listener in psutil.net_connections(kind="tcp"):
            if (
                listener.status != psutil.CONN_LISTEN
                or not listener.laddr
                or listener.laddr.port != port
                or listener.pid is None
            ):
                continue
            environment = psutil.Process(listener.pid).environ()
            values = {key: environment[key] for key in RABBIT_ENV if key in environment}
            if len(values) != len(RABBIT_ENV):
                continue
            server = (
                Path(values["ERLANG_HOME"]).parent
                / "rabbitmq_server-4.4.0/sbin/rabbitmq-server.bat"
            )
            if server.is_file():
                return {"server": str(server), "env": values, "host": host, "port": port}
    except OSError, psutil.Error:
        pass
    return {}


def _restart_rabbit(recipe: Any, host: str, port: int, parameters: Any) -> None:
    if (
        sys.platform != "win32"
        or host not in LOCAL_HOSTS
        or not isinstance(recipe, dict)
        or recipe.get("host") != host
        or recipe.get("port") != port
        or not isinstance(recipe.get("env"), dict)
    ):
        raise RuntimeError("RabbitMQ is unavailable; start the existing configured broker.")
    values = recipe["env"]
    server = Path(str(recipe.get("server", "")))
    if (
        not server.is_file()
        or any(not isinstance(values.get(key), str) for key in RABBIT_ENV)
        or not Path(values["RABBITMQ_BASE"]).is_dir()
        or not Path(values["ERLANG_HOME"]).is_dir()
        or not Path(values["RABBITMQ_CONFIG_FILE"]).is_file()
    ):
        raise RuntimeError("The recorded RabbitMQ files are missing; no broker was created.")
    env = os.environ.copy()
    env.update({key: values[key] for key in RABBIT_ENV})
    log = Path(".dev/rabbitmq.log")
    log.parent.mkdir(exist_ok=True)
    try:
        with log.open("a", encoding="utf-8") as output:
            process = subprocess.Popen(  # noqa: S603 -- recorded existing local broker script.
                [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(server)],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if _rabbit_ready(parameters):
                return
            if process.poll() is not None:
                break
            time.sleep(0.5)
    except OSError:
        raise RuntimeError("The existing RabbitMQ broker could not be restarted.") from None
    raise RuntimeError("RabbitMQ did not become available; inspect .dev/rabbitmq.log.")


def ensure_dependencies(state: dict[str, Any]) -> None:
    _ensure_postgres(state)
    try:
        parameters = _rabbit_parameters()
    except ValueError, TypeError:
        raise RuntimeError("The configured RabbitMQ connection is invalid.") from None
    if not _rabbit_ready(parameters):
        _restart_rabbit(state.get("rabbitmq"), parameters.host, parameters.port, parameters)
    state["rabbitmq"] = _rabbit_recipe(parameters.host, parameters.port)
