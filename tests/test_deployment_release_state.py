import json
import subprocess
from pathlib import Path

import pytest

from deploy import release_state

REPOSITORY = "us-docker.pkg.dev/project/repo"


def images(digit: str, *, web: str | None = None) -> dict[str, str]:
    values = {
        f"{name.upper()}_IMAGE": f"{REPOSITORY}/{name}@sha256:{digit * 64}"
        for name in ("backend", "web", "gateway", "postgres")
    }
    if web is not None:
        values["WEB_IMAGE"] = f"{REPOSITORY}/web@sha256:{web * 64}"
    return values


class Host:
    def __init__(self, root: Path) -> None:
        self.releases = root / "releases"
        self.secrets = root / "secrets"
        self.current = root / "current"
        self.state = root / "release-state.json"
        self.removed: list[str] = []
        self.refused: set[str] = set()

    def release(self, name: str, values: dict[str, str]) -> Path:
        directory = self.releases / name
        (directory / "deploy").mkdir(parents=True)
        (directory / "deploy/release.env").write_text(
            "".join(f"{key}={value}\n" for key, value in values.items())
            + f"RUNTIME_ENV_FILE=/run/pyracast/releases/{name}/runtime.env\n"
        )
        (self.secrets / name).mkdir(parents=True)
        (self.secrets / name / "runtime.env").write_text("PGUSER=pyracast_app\n")
        return directory

    def remove_image(self, reference: str) -> None:
        if reference in self.refused:
            raise subprocess.CalledProcessError(1, ["docker", "image", "rm", reference])
        self.removed.append(reference)

    def prune(self) -> list[str]:
        return release_state.prune(
            self.state, self.releases, self.secrets, self.current, self.remove_image
        )


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def test_record_names_the_serving_and_the_previous_release(host: Host) -> None:
    serving = host.release("b" * 40 + "-222222222222", images("2"))
    previous = host.release("a" * 40 + "-111111111111", images("1"))

    release_state.record(host.state, serving, previous)

    assert json.loads(host.state.read_text()) == {
        "current": serving.name,
        "previous": previous.name,
    }
    assert [path.name for path in host.state.parent.iterdir() if path.suffix == ".tmp"] == []


def test_prune_keeps_the_serving_and_the_previous_release_with_their_images(host: Host) -> None:
    oldest = host.release("oldest", images("1"))
    older = host.release("older", images("2", web="4"))
    previous = host.release("previous", images("3"))
    serving = host.release("serving", images("4"))
    release_state.record(host.state, serving, previous)

    assert host.prune() == []

    assert sorted(path.name for path in host.releases.iterdir()) == ["previous", "serving"]
    assert sorted(path.name for path in host.secrets.iterdir()) == ["previous", "serving"]
    kept = {*images("3").values(), *images("4").values()}
    expected = ({*images("1").values(), *images("2", web="4").values()}) - kept
    assert sorted(host.removed) == sorted(expected)
    assert f"{REPOSITORY}/web@sha256:{'4' * 64}" not in host.removed
    assert not oldest.exists()
    assert not older.exists()


def test_prune_after_a_first_release_keeps_only_the_serving_release(host: Host) -> None:
    serving = host.release("serving", images("4"))
    release_state.record(host.state, serving, None)

    assert host.prune() == []

    assert [path.name for path in host.releases.iterdir()] == ["serving"]
    assert host.removed == []


def test_prune_also_keeps_the_release_that_current_points_to(host: Host) -> None:
    linked = host.release("linked", images("1"))
    previous = host.release("previous", images("2"))
    serving = host.release("serving", images("3"))
    release_state.record(host.state, serving, previous)

    assert (
        release_state.prune(
            host.state,
            host.releases,
            host.secrets,
            host.current,
            host.remove_image,
            resolve=lambda _link: linked,
        )
        == []
    )

    assert sorted(path.name for path in host.releases.iterdir()) == [
        "linked",
        "previous",
        "serving",
    ]


def test_prune_reports_an_image_it_cannot_remove_and_continues(host: Host) -> None:
    host.release("old", images("1"))
    previous = host.release("previous", images("2"))
    serving = host.release("serving", images("3"))
    release_state.record(host.state, serving, previous)
    refused = images("1")["BACKEND_IMAGE"]
    host.refused.add(refused)

    problems = host.prune()

    assert problems == [f"Image {refused} was not removed."]
    assert sorted(host.removed) == sorted(set(images("1").values()) - {refused})
    assert sorted(path.name for path in host.releases.iterdir()) == ["previous", "serving"]


@pytest.mark.parametrize(
    "content",
    [None, "not json", "[]", '{"current": "serving"}', '{"current": "../x", "previous": null}'],
)
def test_prune_without_a_valid_state_removes_nothing(host: Host, content: str | None) -> None:
    host.release("old", images("1"))
    host.release("serving", images("3"))
    if content is not None:
        host.state.write_text(content)

    problems = host.prune()

    assert problems == [f"{host.state} is missing or invalid; nothing was pruned."]
    assert sorted(path.name for path in host.releases.iterdir()) == ["old", "serving"]
    assert host.removed == []


def test_prune_removes_an_unkept_release_without_images_and_leaves_files(host: Host) -> None:
    previous = host.release("previous", images("2"))
    serving = host.release("serving", images("3"))
    (host.releases / "broken" / "deploy").mkdir(parents=True)
    (host.releases / "stray.txt").write_text("operator note\n")
    release_state.record(host.state, serving, previous)

    assert host.prune() == []

    assert sorted(path.name for path in host.releases.iterdir()) == [
        "previous",
        "serving",
        "stray.txt",
    ]
    assert host.removed == []


def test_command_line_records_and_prunes_with_docker(
    host: Host, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    host.release("old", images("1"))
    previous = host.release("previous", images("2"))
    serving = host.release("serving", images("3"))
    monkeypatch.setattr(release_state, "STATE", host.state)
    monkeypatch.setattr(release_state, "RELEASES", host.releases)
    monkeypatch.setattr(release_state, "SECRETS", host.secrets)
    monkeypatch.setattr(release_state, "CURRENT", host.current)
    commands: list[list[str]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[-1].endswith("1" * 64) and "/web@" in command[-1]:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)

    assert release_state.main(["record", str(serving), str(previous)]) == 0
    assert release_state.main(["prune"]) == 1

    assert sorted(command[-1] for command in commands) == sorted(images("1").values())
    assert all(command[:3] == ["docker", "image", "rm"] for command in commands)
    assert "was not removed" in capsys.readouterr().err
    assert release_state.main(["record", str(serving)]) == 0
    assert json.loads(host.state.read_text())["previous"] is None
