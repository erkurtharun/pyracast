import argparse
import os
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# Paths that no image, release bundle, release step or runtime reads. An entry ending in "/**"
# covers its directory; any other entry matches only that exact path.
ALLOWLIST = (
    "docs/**",
    "tests/**",
    "scripts/**",
    "web/tests/**",
    "web/integration/**",
    "web/production-tests/**",
    ".agents/**",
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/settings.json",
    ".codex/hooks.json",
    ".github/dependabot.yml",
    "qodana.yaml",
    "qodana-js.yaml",
    "sonar-project.properties",
    ".gitignore",
    "web/.gitignore",
    "web/.prettierignore",
)
SHA = re.compile(r"[0-9a-f]{40}")
# Both git calls must finish inside the one-minute limit of the classification step.
GIT_TIMEOUT_SECONDS = 25


@dataclass(frozen=True)
class Decision:
    release: bool
    reason: str


def _matches(path: str, entry: str) -> bool:
    if entry.endswith("/**"):
        return path.startswith(entry.removesuffix("**"))
    return path == entry


def unshipped(path: str) -> bool:
    return any(_matches(path, entry) for entry in ALLOWLIST)


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("git")
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    return subprocess.run(  # noqa: S603 -- Resolved git executable; arguments are validated SHAs.
        [git, "-C", str(root), *arguments],
        capture_output=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
        env=environment,
    )


def _failed(result: subprocess.CompletedProcess[bytes]) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        result.returncode, result.args, result.stdout, result.stderr
    )


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = _git(root, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode not in (0, 1):
        raise _failed(result)
    return result.returncode == 0


def _changed_paths(root: Path, base: str, head: str) -> list[str]:
    result = _git(root, "diff", "--name-only", "--no-renames", "-z", base, head)
    if result.returncode != 0:
        raise _failed(result)
    return [path for path in result.stdout.decode("utf-8").split("\0") if path]


def decide(event: str, deployed_sha: str, head_sha: str, root: Path) -> Decision:
    if event != "workflow_run":
        return Decision(True, f"Event {event!r} always releases.")
    if SHA.fullmatch(deployed_sha) is None:
        return Decision(True, "No usable deployed commit was found.")
    if SHA.fullmatch(head_sha) is None:
        return Decision(True, "The authorized commit is not a full SHA.")
    try:
        if not _is_ancestor(root, deployed_sha, head_sha):
            return Decision(True, f"The deployed commit {deployed_sha} is not an ancestor.")
        paths = _changed_paths(root, deployed_sha, head_sha)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as error:
        return Decision(True, f"Git could not compare the commits: {error}")
    if not paths:
        return Decision(True, f"No change since {deployed_sha}; releasing it again.")
    released = [path for path in paths if not unshipped(path)]
    if released:
        listed = ", ".join(released[:10])
        return Decision(
            True,
            f"{len(released)} changed paths since {deployed_sha} lie outside the unshipped "
            f"allowlist, including {listed}.",
        )
    return Decision(
        False, f"Nothing to release: changes since {deployed_sha} touch only unshipped paths."
    )


def sparse_patterns(entries: Sequence[str]) -> list[str]:
    excluded = [
        f"!/{entry.removesuffix('**')}" if entry.endswith("/**") else f"!/{entry}"
        for entry in entries
    ]
    return ["/*", *excluded]


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    classify = commands.add_parser("classify")
    classify.add_argument("event")
    classify.add_argument("deployed_sha")
    classify.add_argument("head_sha")
    commands.add_parser("sparse-patterns")
    values = parser.parse_args(arguments)
    if values.command == "sparse-patterns":
        print("\n".join(sparse_patterns(ALLOWLIST)))
        return
    decision = decide(values.event, values.deployed_sha, values.head_sha, Path.cwd())
    print(decision.reason)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(decision.reason + "\n")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(f"release={str(decision.release).lower()}\n")


if __name__ == "__main__":
    main()
