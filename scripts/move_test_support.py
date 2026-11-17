# Rerun after a rebase that brings new `from tests.test_<x> import ...` lines; names already moved
# only get their imports rewritten.
import ast
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_MODULE = re.compile(r"tests\.test_(\w+)")
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)


@dataclass(frozen=True)
class Definition:
    name: str
    start: int
    end: int
    uses: frozenset[str]


def support_path(tests: Path, stem: str) -> Path:
    return tests / "support" / f"{stem}.py"


def _bound_name(node: ast.stmt) -> str | None:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.name
    targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
    names = [target.id for target in targets if isinstance(target, ast.Name)]
    return names[0] if len(names) == 1 else None


def _start_line(node: ast.stmt, lines: list[str]) -> int:
    decorators: list[ast.expr] = getattr(node, "decorator_list", [])
    start = min([node.lineno, *(decorator.lineno for decorator in decorators)])
    while start > 1 and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return start


def definitions(source: str) -> dict[str, Definition]:
    lines = source.splitlines()
    found: dict[str, Definition] = {}
    for node in ast.parse(source).body:
        name = _bound_name(node) if isinstance(node, DEFINITIONS) else None
        if name is None:
            continue
        if name in found:
            raise RuntimeError(f"{name} is defined twice; move it by hand.")
        uses = {child.id for child in ast.walk(node) if isinstance(child, ast.Name)} - {name}
        found[name] = Definition(
            name, _start_line(node, lines), node.end_lineno or 0, frozenset(uses)
        )
    return found


def closure(names: set[str], found: dict[str, Definition]) -> set[str]:
    pending, moved = list(names), set[str]()
    while pending:
        name = pending.pop()
        if name in moved:
            continue
        if name not in found:
            raise RuntimeError(f"{name} is not a top-level definition; move it by hand.")
        moved.add(name)
        pending.extend(use for use in found[name].uses if use in found)
    return moved


def import_block(source: str) -> str:
    lines = source.splitlines()
    statements = [
        "\n".join(lines[node.lineno - 1 : node.end_lineno])
        for node in ast.parse(source).body
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    return "\n".join(statements)


def imported_names(tests: Path) -> dict[str, set[str]]:
    names: dict[str, set[str]] = defaultdict(set)
    for path in sorted(tests.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                match = TEST_MODULE.fullmatch(node.module)
                if match:
                    names[match.group(1)].update(alias.name for alias in node.names)
    return names


def _defined_in(path: Path) -> set[str]:
    return set(definitions(path.read_text(encoding="utf-8"))) if path.is_file() else set()


def move(tests: Path, stem: str, names: set[str]) -> set[str]:
    source_path = tests / f"test_{stem}.py"
    target = support_path(tests, stem)
    pending = names - _defined_in(target)
    if not pending:
        return set()
    source = source_path.read_text(encoding="utf-8")
    found = definitions(source)
    moved = closure(pending, found)
    lines = source.splitlines()
    ordered = sorted((found[name] for name in moved), key=lambda item: item.start)
    blocks = ["\n".join(lines[item.start - 1 : item.end]) for item in ordered]
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    target.parent.mkdir(exist_ok=True)
    (target.parent / "__init__.py").touch()
    header = import_block(source)
    target.write_bytes(
        "\n\n\n".join(part for part in (header, existing, *blocks) if part).encode() + b"\n"
    )
    removed = {line for item in ordered for line in range(item.start, item.end + 1)}
    remaining = "\n".join(line for number, line in enumerate(lines, 1) if number not in removed)
    used = {node.id for node in ast.walk(ast.parse(remaining)) if isinstance(node, ast.Name)}
    still_used = sorted(moved & used)
    if still_used:
        remaining = (
            f"from tests.support.{stem} import {', '.join(still_used)}\n" + remaining.lstrip("\n")
        )
    source_path.write_bytes(remaining.encode() + b"\n")
    return moved


def rewrite_references(tests: Path, moved: dict[str, set[str]]) -> list[Path]:
    changed: list[Path] = []

    def dotted(match: re.Match[str]) -> str:
        stem, name = match.group(1), match.group(2)
        return f"tests.support.{stem}.{name}" if name in moved.get(stem, ()) else match.group(0)

    for path in sorted(tests.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        updated = re.sub(
            r"\bfrom tests\.test_(\w+) import\b", r"from tests.support.\1 import", text
        )
        updated = re.sub(r"\btests\.test_(\w+)\.(\w+)\b", dotted, updated)
        if updated != text:
            path.write_bytes(updated.encode())
            changed.append(path)
    return changed


def tidy(paths: list[Path]) -> None:
    arguments = [str(path) for path in sorted(set(paths))]
    if not arguments:
        return
    for command in (["check", "--fix", "--select", "F401,I001"], ["format"]):
        subprocess.run(  # noqa: S603 -- fixed ruff invocation on files this script wrote.
            [sys.executable, "-m", "ruff", *command, "--quiet", *arguments],
            cwd=ROOT,
            check=command[0] == "format",
        )


def main(tests: Path = ROOT / "tests") -> dict[str, set[str]]:
    moved: dict[str, set[str]] = {}
    touched: list[Path] = []
    for stem, names in sorted(imported_names(tests).items()):
        newly = move(tests, stem, names)
        if newly:
            touched += [tests / f"test_{stem}.py", support_path(tests, stem)]
        moved[stem] = names | newly | _defined_in(support_path(tests, stem))
    touched += rewrite_references(tests, moved)
    tidy(touched)
    return moved


if __name__ == "__main__":
    for stem, names in sorted(main().items()):
        print(f"tests/support/{stem}.py: {', '.join(sorted(names))}")
