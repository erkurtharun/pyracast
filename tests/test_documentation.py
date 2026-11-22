import ast
import os
import re
from collections.abc import Iterator
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    "README.md",
    "AGENTS.md",
    "docs/README.md",
    "docs/STACK.md",
    "docs/ARCHITECTURE.md",
    "docs/DEPLOYMENT.md",
    "docs/IRRADIANCE.md",
    "docs/DATA_MODEL.md",
    *sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "docs" / "flows").glob("*.md")),
)
# A backticked word that starts with one of these directories is read as a repository path.
TOP_DIRECTORIES = (
    ".agents",
    ".claude",
    ".codex",
    ".github",
    "deploy",
    "docs",
    "infra",
    "scripts",
    "src",
    "tests",
    "web",
)
# Files that exist only on a host, next to a copy of the tree; none is tracked or ignored here.
HOST_FILES = {
    "deploy/release.env": "the release installer writes it into each release directory",
}
# Code that may spell a non-module `pyracast.` name: a logger, a systemd unit, a container label,
# a browser storage key or the site domain. Ignored paths are skipped and only these suffixes are
# read, which keeps Terraform variables, plans and state out.
CODE_ROOTS = ("src", "web", "deploy", "infra", "scripts", ".github")
CODE_SUFFIXES = frozenset(
    {".py", ".ts", ".tsx", ".js", ".mjs", ".sh", ".yaml", ".yml", ".tf", ".tftpl", ".service"}
    | {".timer", ".sql", ".conf"}
)

FENCE = re.compile(r"^\s*(```|~~~)")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$")
HTML_ANCHOR = re.compile(r"<a\s[^>]*\b(?:id|name)=\"([^\"]+)\"")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
LINE_ANCHOR = re.compile(r"^L(\d+)(?:C\d+)?(?:-L(\d+)(?:C\d+)?)?$")
BRACES = re.compile(r"\{([^{}]*,[^{}]*)\}")
PLACEHOLDER = re.compile(r"<[^<>/]+>")
DOTTED = re.compile(r"^pyracast(?:\.[A-Za-z_]\w*)+")


def _lines(document: str) -> Iterator[tuple[int, str]]:
    fenced = False
    for number, line in enumerate((ROOT / document).read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            yield number, line


def _words(document: str) -> Iterator[tuple[int, str]]:
    for number, line in _lines(document):
        for span in CODE_SPAN.finditer(line):
            for word in span.group(2).split():
                word = word.lstrip("(\"'").rstrip(".,;\"'")
                if word.count(")") > word.count("("):
                    word = word.rstrip(")")
                yield number, word


@cache
def _entries(directory: Path) -> frozenset[str]:
    return frozenset(os.listdir(directory)) if directory.is_dir() else frozenset()


def _exists(relative: PurePosixPath) -> bool:
    """Case-sensitive on every system, so a document passes on Windows only where Ubuntu agrees."""
    current = ROOT
    for part in relative.parts:
        if part not in _entries(current):
            return False
        current /= part
    return True


@cache
def _ignore_rules(directory: PurePosixPath) -> tuple[tuple[bool, bool, tuple[str, ...]], ...]:
    """(negated, anchored, segments) of the `.gitignore` in a repository directory."""
    file = ROOT / directory / ".gitignore"
    if not file.is_file():
        return ()
    rules = []
    for line in file.read_text(encoding="utf-8").splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negated = pattern.startswith("!")
        pattern = pattern.removeprefix("!").rstrip("/")
        anchored = "/" in pattern
        rules.append((negated, anchored, tuple(pattern.lstrip("/").split("/"))))
    return tuple(rules)


def _ignored(relative: PurePosixPath) -> bool:
    """Git's rule without `**`: the last matching pattern wins and nothing under an ignored
    directory comes back."""
    parts = relative.parts
    for end in range(1, len(parts) + 1):
        ignored = False
        for start in range(end):
            for negated, anchored, segments in _ignore_rules(PurePosixPath(*parts[:start])):
                candidate = parts[start:end]
                if anchored:
                    matches = len(segments) == len(candidate) and all(
                        fnmatchcase(name, segment)
                        for name, segment in zip(candidate, segments, strict=True)
                    )
                else:
                    matches = fnmatchcase(candidate[-1], segments[0])
                if matches:
                    ignored = not negated
        if ignored:
            return True
    return False


def _alternatives(path: str) -> list[str]:
    match = BRACES.search(path)
    if match is None:
        return [path]
    return [
        expanded
        for choice in match.group(1).split(",")
        for expanded in _alternatives(path[: match.start()] + choice + path[match.end() :])
    ]


def _path_problem(token: str) -> str | None:
    """A placeholder segment such as `<release>` stands for any name, so it and a glob must match
    at least one entry; a `file::test` node id must name a test function of that file."""
    path, _, node = token.partition("::")
    path = re.sub(r":\d+(?:-\d+)?$", "", path)
    if path in HOST_FILES:
        return None
    for alternative in _alternatives(path):
        pattern = PLACEHOLDER.sub("*", alternative)
        relative = PurePosixPath(pattern)
        if "*" in pattern or "?" in pattern:
            escaped = pattern.rstrip("/").replace("[", "[[]")
            if not any(ROOT.glob(escaped, case_sensitive=True)):
                return f"no file matches {alternative}"
        elif _ignored(relative):
            continue
        elif not _exists(relative):
            return f"{alternative} does not exist"
        elif alternative.endswith("/") and not (ROOT / relative).is_dir():
            return f"{alternative} is not a directory"
    if node and not re.search(
        rf"^\s*(?:async\s+)?def {re.escape(node.split('::')[-1].split('[')[0])}\(",
        (ROOT / path).read_text(encoding="utf-8"),
        re.MULTILINE,
    ):
        return f"{path} has no test {node}"
    return None


def _slug(heading: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"<[^>]+>", "", text).replace("`", "").replace("*", "")
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


@cache
def _anchors(document: str) -> frozenset[str]:
    """GitHub's heading anchors: the slug of each heading, `-1`, `-2` ... for repeats."""
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    for _, line in _lines(document):
        anchors.update(HTML_ANCHOR.findall(line))
        if heading := HEADING.match(line):
            slug = _slug(heading.group(1))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            anchors.add(f"{slug}-{count}" if count else slug)
    return frozenset(anchors)


def _link_problem(document: str, target: str) -> str | None:
    path, _, anchor = target.partition("#")
    path = unquote(path)
    base = PurePosixPath(document).parent
    resolved = PurePosixPath(path.lstrip("/")) if path.startswith("/") else base / path
    parts: list[str] = []
    for part in resolved.parts if path else PurePosixPath(document).parts:
        if part == "..":
            if not parts:
                return f"{target} leaves the repository"
            parts.pop()
        elif part != ".":
            parts.append(part)
    file = PurePosixPath(*parts)
    if not _exists(file):
        return f"{target} does not exist"
    if not anchor:
        return None
    if file.suffix == ".md":
        return None if unquote(anchor) in _anchors(file.as_posix()) else f"{target} has no heading"
    lines = LINE_ANCHOR.match(anchor)
    count = len((ROOT / file).read_text(encoding="utf-8").splitlines())
    if lines is None or max(int(number) for number in lines.groups() if number) > count:
        return f"{target} has no such line"
    return None


def _module(parts: list[str]) -> tuple[Path | None, list[str]]:
    current = PurePosixPath("src")
    for index, part in enumerate(parts):
        if _exists(current / part) and (ROOT / current / part).is_dir():
            current /= part
        elif _exists(current / f"{part}.py"):
            return ROOT / current / f"{part}.py", parts[index + 1 :]
        else:
            return (ROOT / current / "__init__.py" if index > 1 else None), parts[index:]
    return ROOT / current / "__init__.py", []


@cache
def _tree(module: Path) -> ast.Module:
    return ast.parse(module.read_text(encoding="utf-8")) if module.is_file() else ast.Module()


def _definitions(body: list[ast.stmt]) -> Iterator[tuple[str, ast.stmt]]:
    for node in body:
        match node:
            case ast.FunctionDef() | ast.AsyncFunctionDef() | ast.ClassDef():
                yield node.name, node
            case ast.Assign(targets=targets):
                for target in targets:
                    if isinstance(target, ast.Name):
                        yield target.id, node
            case ast.AnnAssign(target=ast.Name(id=name)):
                yield name, node
            case ast.Import(names=names) | ast.ImportFrom(names=names):
                for alias in names:
                    yield (alias.asname or alias.name).split(".")[0], node
            case ast.If(body=inner, orelse=other) | ast.Try(body=inner, orelse=other):
                yield from _definitions([*inner, *other])


def _attribute_problem(module: Path, names: list[str]) -> str | None:
    """Resolves an attribute chain by reading source: module names, class members and names a
    module imports from another `pyracast` module. Importing would need Django and a database."""
    body: list[ast.stmt] = _tree(module).body
    for index, name in enumerate(names):
        found = dict(_definitions(body)).get(name)
        if found is None:
            return f"{name} is not defined in {module.relative_to(ROOT).as_posix()}"
        if isinstance(found, ast.ClassDef):
            body = found.body
        elif isinstance(found, ast.ImportFrom) and (found.module or "").startswith("pyracast"):
            source = next(
                alias.name for alias in found.names if (alias.asname or alias.name) == name
            )
            target, rest = _module([*(found.module or "").split("."), source])
            if target is None:
                return f"{found.module}.{source} is not a module"
            return _attribute_problem(target, [*rest, *names[index + 1 :]])
        elif index < len(names) - 1:
            return f"{name} in {module.relative_to(ROOT).as_posix()} has no members to read"
    return None


@cache
def _code_text() -> str:
    texts: list[str] = []
    for top in CODE_ROOTS:
        for directory, children, files in os.walk(ROOT / top):
            relative = PurePosixPath(Path(directory).relative_to(ROOT).as_posix())
            children[:] = [child for child in children if not _ignored(relative / child)]
            texts.extend(
                (Path(directory) / file).read_text(encoding="utf-8", errors="replace")
                for file in files
                if Path(file).suffix in CODE_SUFFIXES and not _ignored(relative / file)
            )
    return "\n".join([*texts, (ROOT / "Dockerfile").read_text(encoding="utf-8")])


@cache
def _name_problem(token: str) -> str | None:
    """A name that is no module or attribute must be spelled whole by the code itself."""
    name = DOTTED.match(token)
    assert name is not None
    module, rest = _module(name.group().split("."))
    problem = (
        f"{name.group()} is not a module" if module is None else _attribute_problem(module, rest)
    )
    if problem and re.search(rf"(?<![\w.]){re.escape(name.group())}(?!\w|\.\w)", _code_text()):
        return None
    return problem


def _problems(document: str) -> list[str]:
    found = []
    for number, line in _lines(document):
        prose = CODE_SPAN.sub("code", line)
        targets = [*LINK.findall(prose), *REFERENCE.findall(prose)]
        for target in targets:
            if not SCHEME.match(target) and (problem := _link_problem(document, target)):
                found.append(f"{document}:{number}: link {problem}")
    for number, word in _words(document):
        if word.split("/")[0] in TOP_DIRECTORIES and "/" in word:
            if problem := _path_problem(word):
                found.append(f"{document}:{number}: `{word}`: {problem}")
        elif DOTTED.match(word) and (problem := _name_problem(word)):
            found.append(f"{document}:{number}: `{word}`: {problem}")
    return found


@pytest.mark.parametrize("document", DOCUMENTS)
def test_links_paths_and_names_of_a_document_match_the_tree(document: str) -> None:
    problems = _problems(document)

    assert not problems, "\n".join(problems)


def test_every_named_top_directory_is_a_repository_directory() -> None:
    assert [name for name in TOP_DIRECTORIES if not (ROOT / name).is_dir()] == []
    assert [path for path in HOST_FILES if _exists(PurePosixPath(path))] == []


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        ("2. Add secrets", "2-add-secrets"),
        ("Database invariants and rollout", "database-invariants-and-rollout"),
        ("`check.py` stages, skips & modes", "checkpy-stages-skips--modes"),
        ("[Flow](flows/x.md) map_name", "flow-map_name"),
    ],
)
def test_heading_anchors_follow_githubs_slug_rule(heading: str, anchor: str) -> None:
    assert _slug(heading) == anchor


@pytest.mark.parametrize(
    ("token", "problem"),
    [
        ("tests/e2e/test_*_spec.py", None),
        ("web/messages/{en,tr}/shell.json", None),
        ("web/src/app/app/w/[tenantId]/layout.tsx", None),
        ("docs/flows/<flow>.md", None),
        ("web/node_modules/.bin/next", None),
        ("deploy/release.env", None),
        ("tests/test_documentation.py:12", None),
        ("tests/test_documentation.py::test_heading_anchors_follow_githubs_slug_rule[x]", None),
        ("docs/flows/<flow>.txt", "no file matches docs/flows/<flow>.txt"),
        ("web/messages/{en,xx}/shell.json", "web/messages/xx/shell.json does not exist"),
        ("docs/readme.md", "docs/readme.md does not exist"),
        ("docs/README.md/", "docs/README.md/ is not a directory"),
        ("tests/test_documentation.py::test_absent", "tests/test_documentation.py has no test"),
    ],
)
def test_path_rules_accept_globs_placeholders_and_ignored_outputs_only(
    token: str, problem: str | None
) -> None:
    found = _path_problem(token)

    assert found == problem if problem is None else (found or "").startswith(problem)


@pytest.mark.parametrize(
    ("token", "resolves"),
    [
        ("pyracast.settings", True),
        ("pyracast.cli.main", True),
        ("pyracast.platform_admin.site.PlatformAdminSite.get_urls", True),
        ("pyracast.security", True),
        ("pyracast.service", True),
        ("pyracast.cli.absent", False),
        ("pyracast.absent", False),
        ("pyracast.platform_admin.site.PlatformAdminSite.absent", False),
        ("pyracast.Settings", False),
        ("pyracast.PLATFORM_ADMIN.site", False),
    ],
)
def test_dotted_names_resolve_by_source_or_by_a_spelling_in_code(
    token: str, resolves: bool
) -> None:
    assert (_name_problem(token) is None) is resolves
