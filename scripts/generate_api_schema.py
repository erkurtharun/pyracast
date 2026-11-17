import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory

WEB = Path(__file__).resolve().parents[1] / "web"
FIX = "npm --prefix web run api:generate"


def run(
    command: list[str],
    cwd: Path | str,
    *,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        command, cwd=cwd, env=env, input=stdin, capture_output=capture, check=False
    )


def generate_openapi(output: Path) -> int:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PYRACAST_", "DJANGO_", "PG"))
    }
    environment["PYRACAST_SECRET_KEY"] = token_urlsafe(48)
    # A clean working directory prevents the application from loading a local .env file.
    with TemporaryDirectory(prefix="pyracast-schema-") as directory:
        result = run(
            [
                sys.executable,
                "-m",
                "django",
                "spectacular",
                "--settings=pyracast.settings",
                "--validate",
                "--fail-on-warn",
                "--format=openapi-json",
                f"--file={output}",
            ],
            directory,
            env=environment,
        )
    return result.returncode


def web_tool(*arguments: str, stdin: bytes | None = None) -> bytes:
    npx = shutil.which("npx")
    if npx is None:
        raise RuntimeError(
            "npx is missing; install Node.js and run `npm --prefix web ci --ignore-scripts`."
        )
    result = run([npx, "--no-install", *arguments], WEB, stdin=stdin, capture=True)
    result.check_returncode()
    return result.stdout


def formatted(content: bytes, path: str) -> bytes:
    # The same prettier call as api:generate; web/.prettierignore lists both generated files.
    return web_tool(
        "prettier", "--ignore-path", "../.gitignore", "--stdin-filepath", path, stdin=content
    )


def committed(name: str) -> bytes | None:
    try:
        return (WEB / name).read_bytes()
    except FileNotFoundError:
        return None


def same_document(fresh: bytes, current: bytes | None) -> bool:
    if current is None:
        return False
    try:
        return bool(json.loads(fresh) == json.loads(current))
    except ValueError:
        return False


def stale_files(openapi: Path) -> list[str]:
    if not same_document(openapi.read_bytes(), committed("openapi.json")):
        return ["openapi.json"]
    generated = {
        "openapi.json": formatted(openapi.read_bytes(), "openapi.json"),
        "src/lib/schema.d.ts": formatted(
            web_tool("openapi-typescript", str(openapi)), "src/lib/schema.d.ts"
        ),
    }
    return [name for name, content in generated.items() if committed(name) != content]


def check() -> int:
    with TemporaryDirectory(prefix="pyracast-schema-check-") as directory:
        openapi = Path(directory) / "openapi.json"
        if code := generate_openapi(openapi):
            return code
        try:
            stale = stale_files(openapi)
        except subprocess.CalledProcessError as error:
            sys.stderr.write(error.stderr.decode(errors="replace"))
            return error.returncode
        except RuntimeError as error:
            print(error, file=sys.stderr)
            return 1
    for name in stale:
        print(f"web/{name} is stale.")
    if stale:
        print(f"Run `{FIX}` and commit the result.")
        return 1
    print("API schema and generated types are current.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--output", type=Path, default=WEB / "openapi.json")
    mode.add_argument(
        "--check",
        action="store_true",
        help="compare freshly generated web/openapi.json and web/src/lib/schema.d.ts with the "
        "committed files without writing them",
    )
    args = parser.parse_args(argv)
    if args.check:
        return check()
    return generate_openapi(args.output.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
