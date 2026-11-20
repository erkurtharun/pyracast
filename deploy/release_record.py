import dataclasses
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import reset_database
from deploy.reset_database import Host, Record, Session

# The installer prints the name into a shell command: no whitespace, quotes, NUL or dot segments.
OBJECT = re.compile(r"postgres(?:/[A-Za-z0-9_.-]+)+\.dump")
PRERELEASE = "-prerelease-"


def valid_object(value: object) -> str | None:
    if not isinstance(value, str) or not OBJECT.fullmatch(value):
        return None
    if any(part in {".", ".."} for part in value.split("/")):
        return None
    return value


def dumped_backup(record: Path) -> tuple[str, dict[str, int]] | None:
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
        name = valid_object(data["object"])
        counts = {str(table): int(count) for table, count in data["counts"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if name is None or PRERELEASE not in name:
        return None
    return name, counts


def held_session(host: Host) -> Session | None:
    session = reset_database.current_session(host)
    if session is not None and valid_object(session.object) is None:
        raise ValueError("it names no valid archive object")
    return session


def discard(host: Host, name: str) -> None:
    archive = reset_database.archive_path(host, name)
    for path in (archive, archive.with_suffix(".partial"), reset_database.record_path(host, name)):
        path.unlink(missing_ok=True)


def counted_record(host: Host, name: str, dumped: dict[str, int]) -> Record:
    archive = reset_database.archive_path(host, name)
    counts = reset_database.verify_archive(host, archive)
    reset_database.require_equal_counts(dumped, counts, "Dumped and restored")
    if not counts.get("django_migrations"):
        raise ValueError(f"{name} holds no applied migration, so no release could serve it again.")
    return Record(object=name, sha256=reset_database.file_sha256(archive), counts=counts)


def record_session(host: Host, record: Path) -> Session:
    """Before a release changes the database, the session names what the previous release served.

    A session that holds the marker keeps its object, and this run's dump goes. Without one the
    database is unchanged in this maintenance: this run's dump, kept on the host by backup.py and
    counted by a throwaway restore, holds it, so recovery needs neither the bucket nor a token.
    """
    session = held_session(host)
    dumped = dumped_backup(record)
    if session is not None:
        if dumped is not None and dumped[0] != session.object:
            discard(host, dumped[0])
        if session.replaced:
            return session
        recorded = dataclasses.replace(session, replaced=True)
        reset_database.save_session(host, recorded)
        return recorded
    if dumped is None:
        raise ValueError(f"{record} names no valid pre-release object")
    name, counts = dumped
    try:
        kept = counted_record(host, name, counts)
    except BaseException:
        discard(host, name)
        raise
    reset_database.save_record(host, kept)
    recorded = Session(
        marker=reset_database.marker_identity(host),
        object=name,
        release=host.release.resolve().name,
        replaced=True,
    )
    reset_database.save_session(host, recorded)
    return recorded


def end(host: Host) -> int:
    """After a successful release no maintenance is open: its session and pre-release dumps go."""
    if host.marker.exists():
        print(f"{host.marker} exists: a maintenance is open; nothing was removed.", file=sys.stderr)
        return 1
    removed = sorted(host.state.glob(f"*{PRERELEASE}*"))
    for path in removed:
        path.unlink()
    host.session.unlink(missing_ok=True)
    print(f"Removed {len(removed)} pre-release files and the ended session from {host.state}.")
    return 0


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
            action = "record"
        case [config, "recovery"]:
            action, record = "recovery", ""
        case [config, "end"]:
            action, record = "end", ""
        case _:
            return 2
    try:
        host = reset_database.load_host(Path(config))
    except Exception as error:
        print(f"Cannot read the host configuration {config}: {error!r}", file=sys.stderr)
        return 1
    if action == "recovery":
        print(recovery(host))
        return 0
    if action == "end":
        return end(host)
    try:
        session = record_session(host, Path(record))
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
