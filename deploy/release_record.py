import dataclasses
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import reset_database
from deploy.reset_database import Host, Session

# The installer prints the name into a shell command: no whitespace, quotes, NUL or dot segments.
OBJECT = re.compile(r"postgres(?:/[A-Za-z0-9_.-]+)+\.dump")


def valid_object(value: object) -> str | None:
    if not isinstance(value, str) or not OBJECT.fullmatch(value):
        return None
    if any(part in {".", ".."} for part in value.split("/")):
        return None
    return value


def object_name(record: Path) -> str | None:
    try:
        value = json.loads(record.read_text(encoding="utf-8"))["object"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return valid_object(value)


def held_session(host: Host) -> Session | None:
    session = reset_database.current_session(host)
    if session is not None and valid_object(session.object) is None:
        raise ValueError("it names no valid archive object")
    return session


def record_session(host: Host, record: Path) -> Session:
    """Before a release changes the database, the session names what the previous release served.

    A session that holds the marker keeps its object; without one the database is unchanged in
    this maintenance, so this run's pre-release dump holds it.
    """
    session = held_session(host)
    if session is not None and session.replaced:
        return session
    if session is None:
        name = object_name(record)
        if name is None:
            raise ValueError(f"{record} names no valid pre-release object")
        session = Session(
            marker=reset_database.marker_identity(host),
            object=name,
            release=host.release.resolve().name,
            replaced=False,
        )
    recorded = dataclasses.replace(session, replaced=True)
    reset_database.save_session(host, recorded)
    return recorded


def recovery(host: Host) -> str:
    """The recovery the helper accepts from this maintenance's session, as its guards read it."""
    try:
        session = held_session(host)
    except (OSError, ValueError, TypeError) as error:
        return (
            f"The maintenance session {host.session} cannot be read ({error}); the recovery "
            "helper accepts no command while the marker exists and that file cannot be read."
        )
    if session is None or not session.replaced:
        return (
            f"The database is unchanged in this maintenance. Recover with: {host.command('abort')}"
        )
    return (
        f"The database changed in this maintenance: {host.session} names {session.object}, "
        "which holds the data the previous release served.\n"
        "Release a forward fix, or recover with: "
        f"{host.command(f'restore --confirm {session.object}')}"
    )


def main(arguments: list[str] | None = None) -> int:
    match sys.argv[1:] if arguments is None else arguments:
        case [config, "record", record]:
            prerelease: Path | None = Path(record)
        case [config, "recovery"]:
            prerelease = None
        case _:
            return 2
    try:
        host = reset_database.load_host(Path(config))
    except Exception as error:
        print(f"Cannot read the host configuration {config}: {error!r}", file=sys.stderr)
        return 1
    if prerelease is None:
        print(recovery(host))
        return 0
    try:
        session = record_session(host, prerelease)
    except Exception as error:
        print(
            f"The recovery point was not recorded in {host.session}: "
            f"{reset_database.describe(error)}",
            file=sys.stderr,
        )
        return 1
    print(f"Recovery point of this maintenance: {session.object} in {host.session}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
