# ruff: noqa: S603 -- Fixed Docker executables with argument lists; no shell interpolation.
import argparse
import subprocess
from pathlib import Path


def read_environment(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line)


def initialize_database(release: Path, directory: Path) -> None:
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(release / "deploy/release.env"),
        "-f",
        str(release / "deploy/compose.yaml"),
    ]
    psql = [
        *compose,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        "pyracast",
    ]
    result = subprocess.run(
        [*psql, "-Atc", "SELECT count(*) FROM pg_roles WHERE rolname = 'pyracast_owner'"],
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip() == "1":
        return
    runtime = read_environment(directory / "runtime.env")
    migration = read_environment(directory / "migration.env")
    if runtime["PGUSER"] != "pyracast_app" or migration["PGUSER"] != "pyracast_owner":
        raise ValueError("Database role names must match the bootstrap contract.")
    statements = (release / "deploy/bootstrap.sql").read_text()
    statements = statements.removesuffix("\n").removesuffix("COMMIT;")
    for role, values in (("pyracast_app", runtime), ("pyracast_owner", migration)):
        password = values["PGPASSWORD"].replace("'", "''")
        statements += f"\nALTER ROLE {role} PASSWORD '{password}';"
    statements += "\nCOMMIT;\n"
    subprocess.run(psql, input=statements, text=True, check=True, capture_output=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release", type=Path)
    parser.add_argument("--environment-directory", type=Path, default=Path("/run/pyracast"))
    arguments = parser.parse_args()
    initialize_database(arguments.release, arguments.environment_directory)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Database bootstrap failed; inspect database state and secret configuration."
        ) from None
