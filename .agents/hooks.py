import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".venv",
        "venv",
        "__pycache__",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
        ".next",
    }
)
ROOT_ARTIFACT_DIRECTORIES = frozenset(
    {"htmlcov", "build", "dist", "data", "media", "staticfiles", "env", "__pypackages__"}
)
WEB_ARTIFACT_DIRECTORIES = frozenset({"out", "build", "dist"})
READ_TOOLS = frozenset(
    {
        "read",
        "read_file",
        "read_text_file",
        "read_multiple_files",
        "get_file_text_by_path",
        "view_image",
    }
)
CREDENTIAL_SUFFIXES = (".tfvars", ".tfplan", ".tfstate", ".tfstate.backup")
CONTEXT_REASON = (
    "Direct reads of generated, environment and data directories are excluded from project context."
)
SENSITIVE_REASON = "Credential, secret and backup files are not read by agents."
PUBLISH_MARKER = "PYRACAST_ORCHESTRATOR_PUSH"
PUBLISH_REASON = (
    "Agent sessions push only pull request branches (pr/*) and do not merge pull requests or "
    f"dispatch workflows; the orchestrator sets {PUBLISH_MARKER}=1 for that."
)
PUSH_SCOPE_OPTIONS = frozenset({"--all", "--mirror", "--tags"})


def _object(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _working_directory(payload: JsonObject, root: Path) -> Path:
    cwd = payload.get("cwd")
    return Path(cwd).resolve() if isinstance(cwd, str) else root


def _contained_path(value: str, root: Path, cwd: Path) -> Path | None:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    candidate = candidate.resolve()
    return candidate if candidate.is_relative_to(root) else None


def _patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        for prefix in ("*** Add File: ", "*** Update File: ", "*** Move to: "):
            if line.startswith(prefix):
                paths.append(line.removeprefix(prefix))
    return paths


def _response_paths(response: JsonValue) -> list[str]:
    if isinstance(response, str):
        if "Success. Updated the following files:" not in response:
            return []
        return re.findall(r"^[AM] (.+)$", response, re.MULTILINE)
    if isinstance(response, list):
        return [path for item in response for path in _response_paths(item)]
    if isinstance(response, dict):
        return [path for value in response.values() for path in _response_paths(value)]
    return []


def edited_files(payload: JsonObject, root: Path) -> list[Path]:
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    arguments = _object(tool_input)
    candidates: list[str] = []
    if tool_name == "apply_patch":
        patch = arguments.get("command", arguments.get("patch", tool_input))
        if isinstance(patch, str):
            candidates.extend(_patch_paths(patch))
        candidates.extend(_response_paths(payload.get("tool_response")))
    elif tool_name in {"Edit", "Write", "MultiEdit"}:
        path = arguments.get("file_path")
        if isinstance(path, str):
            candidates.append(path)
        edits = arguments.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                path = _object(edit).get("file_path")
                if isinstance(path, str):
                    candidates.append(path)
    cwd = _working_directory(payload, root)
    paths = {_contained_path(candidate, root, cwd) for candidate in candidates}
    return sorted(path for path in paths if path and path.is_file())


def _run(command: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 -- argument lists; no shell or executable from hook input.
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def _require_success(result: subprocess.CompletedProcess[bytes]) -> None:
    if result.returncode:
        output = result.stdout.decode("utf-8", errors="replace")
        raise RuntimeError(output or f"Hook command exited with status {result.returncode}.")


def _format_web_files(paths: list[Path], root: Path) -> None:
    web = root / "web"
    prettier = web / "node_modules" / "prettier" / "bin" / "prettier.cjs"
    node = shutil.which("node")
    sources = [path for path in paths if path.is_relative_to(web)]
    if sources and node and prettier.is_file():
        names = [str(path.relative_to(web)) for path in sources]
        command = [node, str(prettier), "--write", "--ignore-unknown", "--", *names]
        _require_success(_run(command, web, 60))


def post_tool(payload: JsonObject, root: Path) -> None:
    paths = edited_files(payload, root)
    python = [str(path) for path in paths if path.suffix == ".py"]
    if python:
        _require_success(_run([sys.executable, "-m", "ruff", "format", "--", *python], root, 60))
    _format_web_files(paths, root)


def _read_paths(arguments: JsonObject) -> list[str]:
    paths: list[str] = []
    for key in (
        "file_path",
        "filePath",
        "pathInProject",
        "relativePath",
        "path",
        "filename",
        "paths",
    ):
        value = arguments.get(key)
        if isinstance(value, str):
            paths.append(value)
        elif isinstance(value, list):
            paths.extend(item for item in value if isinstance(item, str))
    return paths


def _sensitive_name(name: str) -> bool:
    name = name.casefold()
    if name.endswith(".example"):
        return False
    return name == ".env" or name.startswith(".env.") or name.endswith(CREDENTIAL_SUFFIXES)


def _sensitive(parts: tuple[str, ...]) -> bool:
    folded = tuple(part.casefold() for part in parts)
    return folded[:1] == ("secrets",) or folded[:2] == (".dev", "backups")


def _context_noise(parts: tuple[str, ...]) -> bool:
    folded = tuple(part.casefold() for part in parts)
    return (
        (bool(folded) and folded[0] in ROOT_ARTIFACT_DIRECTORIES)
        or any(part in EXCLUDED_DIRECTORIES for part in folded)
        or any(part.startswith((".venv-", ".next-")) for part in folded[:-1])
        or (len(folded) > 1 and folded[0] == "web" and folded[1] in WEB_ARTIFACT_DIRECTORIES)
    )


def denied_path(value: str, base: Path, root: Path) -> str | None:
    path = Path(value.replace("\\", "/"))
    candidate = path if path.is_absolute() else base / path
    if _sensitive_name(PurePosixPath(value.replace("\\", "/")).name):
        return SENSITIVE_REASON
    # Check both the named directory and a symlink's target, without blocking external paths.
    for resolved in (Path(os.path.abspath(candidate)), candidate.resolve()):
        if resolved.is_relative_to(root):
            parts = resolved.relative_to(root).parts
            if _sensitive(parts):
                return SENSITIVE_REASON
            if _context_noise(parts):
                return CONTEXT_REASON
    return None


def _command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command.replace("\\", "/"))
    except ValueError:
        return command.split()


def _ide_command_paths(command: str) -> list[str]:
    paths: list[str] = []
    for token in _command_tokens(command):
        value = re.sub(r"^--?[\w-]+=", "", token)
        if not value or value.startswith("-"):
            continue
        if "/" in value or _sensitive_name(value):
            paths.append(value)
    return paths


def denied_read(payload: JsonObject, root: Path) -> str | None:
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str):
        return None
    name = tool_name.rsplit("__", 1)[-1].casefold()
    arguments = _object(payload.get("tool_input"))
    base = _working_directory(payload, root)
    project_path = arguments.get("projectPath")
    if isinstance(project_path, str):
        project = Path(project_path)
        base = project if project.is_absolute() else base / project
    if name in READ_TOOLS:
        values = _read_paths(arguments)
    elif name == "execute_tool" and isinstance(command := arguments.get("command"), str):
        values = _ide_command_paths(command)
    else:
        return None
    reasons = {denied_path(value, base, root) for value in values} - {None}
    if SENSITIVE_REASON in reasons:
        return SENSITIVE_REASON
    return next(iter(reasons), None)


def _segments(command: str) -> list[list[str]]:
    return [_command_tokens(part) for part in re.split(r"&&|\|\||[;|&\n]", command)]


def _program(token: str) -> str:
    return PurePosixPath(token).name.casefold().removesuffix(".exe")


def _git_push_refused(arguments: list[str]) -> bool:
    if PUSH_SCOPE_OPTIONS.intersection(arguments):
        return True
    refspecs = [argument for argument in arguments if not argument.startswith("-")][1:]
    destinations = [refspec.split(":")[-1].lstrip("+") for refspec in refspecs]
    return not destinations or not all(
        destination.removeprefix("refs/heads/").startswith("pr/") for destination in destinations
    )


def _git_subcommand(arguments: list[str]) -> tuple[str, list[str]]:
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        index += 2 if arguments[index] in {"-C", "-c"} else 1
    if index >= len(arguments):
        return "", []
    return arguments[index], arguments[index + 1 :]


def _gh_refused(arguments: list[str]) -> bool:
    words = [
        argument
        for index, argument in enumerate(arguments)
        if not argument.startswith("-")
        and (index == 0 or arguments[index - 1] not in {"-R", "--repo"})
    ]
    return words[:2] in (["pr", "merge"], ["workflow", "run"]) or (
        words[:1] == ["api"] and any(word.endswith("/dispatches") for word in words)
    )


def _segment_publishes(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        program = _program(token)
        if program == "git":
            subcommand, arguments = _git_subcommand(tokens[index + 1 :])
            return subcommand == "push" and _git_push_refused(arguments)
        if program == "gh":
            return _gh_refused(tokens[index + 1 :])
    return False


def denied_shell(payload: JsonObject) -> str | None:
    command = _object(payload.get("tool_input")).get("command")
    if isinstance(command, list):
        command = " ".join(part for part in command if isinstance(part, str))
    if not isinstance(command, str) or os.environ.get(PUBLISH_MARKER) == "1":
        return None
    if re.search(rf"{PUBLISH_MARKER}\s*=\s*['\"]?1\b", command):
        return None
    if any(_segment_publishes(tokens) for tokens in _segments(command)):
        return PUBLISH_REASON
    return None


def _deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("event", choices=("post-tool", "pre-read", "pre-shell"))
    event = parser.parse_args(argv).event
    root = (root or Path(__file__).resolve().parents[1]).resolve()
    try:
        value: object = json.load(sys.stdin)
        if not isinstance(value, dict):
            raise ValueError("Hook input must be a JSON object.")
        if event == "post-tool":
            post_tool(value, root)
            return 0
        reason = denied_read(value, root) if event == "pre-read" else denied_shell(value)
        if reason:
            _deny(reason)
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print("\n".join(str(error).splitlines()[-40:]), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
