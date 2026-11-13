import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

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
    }
)
ROOT_ARTIFACT_DIRECTORIES = frozenset({"htmlcov", "build", "dist", "data", "media", "staticfiles"})


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


def edited_python_files(payload: JsonObject, root: Path) -> list[Path]:
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
    return sorted(path for path in paths if path and path.suffix == ".py" and path.is_file())


def _run(command: list[str], root: Path, timeout: int) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 -- argument lists; no shell or executable from hook input.
        command,
        cwd=root,
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


def post_tool(payload: JsonObject, root: Path) -> None:
    paths = [str(path) for path in edited_python_files(payload, root)]
    if paths:
        _require_success(_run([sys.executable, "-m", "ruff", "format", "--", *paths], root, 60))


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


def denied_read(payload: JsonObject, root: Path) -> bool:
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str):
        return False
    name = tool_name.rsplit("__", 1)[-1].casefold()
    if name not in {
        "read",
        "read_file",
        "read_text_file",
        "read_multiple_files",
        "get_file_text_by_path",
        "view_image",
    }:
        return False
    cwd = _working_directory(payload, root)
    arguments = _object(payload.get("tool_input"))
    project_path = arguments.get("projectPath")
    if isinstance(project_path, str):
        project = Path(project_path)
        cwd = project if project.is_absolute() else cwd / project
    for value in _read_paths(arguments):
        path = Path(value)
        candidate = path if path.is_absolute() else cwd / path
        # Check both the named directory and a symlink's target, without blocking external paths.
        for resolved in (Path(os.path.abspath(candidate)), candidate.resolve()):
            if resolved.is_relative_to(root):
                parts = resolved.relative_to(root).parts
                if (parts and parts[0].casefold() in ROOT_ARTIFACT_DIRECTORIES) or any(
                    part.casefold() in EXCLUDED_DIRECTORIES for part in parts
                ):
                    return True
    return False


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("event", choices=("post-tool", "pre-read"))
    event = parser.parse_args(argv).event
    root = (root or Path(__file__).resolve().parents[2]).resolve()
    try:
        value: object = json.load(sys.stdin)
        if not isinstance(value, dict):
            raise ValueError("Hook input must be a JSON object.")
        if event == "post-tool":
            post_tool(value, root)
        elif denied_read(value, root):
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                "Direct reads of generated, environment and data directories "
                                "are excluded from project context."
                            ),
                        }
                    }
                )
            )
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print("\n".join(str(error).splitlines()[-40:]), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
