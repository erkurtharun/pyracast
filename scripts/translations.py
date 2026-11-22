import argparse
import ast
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

import allauth
import django
import polib
from django.utils.translation.template import templatize

_PLACEHOLDERS = re.compile(r"(?<!%)%\(([^)]+)\)")
# templatize writes each translatable template string as one of these calls.
_TEMPLATE_CALL = re.compile(r"\b(?:n?p?gettext|_)\(")
_SINGULAR = frozenset({"_", "gettext", "gettext_lazy", "gettext_noop", "ngettext", "ngettext_lazy"})
_CONTEXTUAL = frozenset({"pgettext", "pgettext_lazy", "npgettext", "npgettext_lazy"})

type Msgid = tuple[str | None, str]


def _literal(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _calls(tree: ast.AST) -> Iterator[Msgid]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", None)
        arguments = [_literal(argument) for argument in node.args]
        if name in _SINGULAR and arguments and arguments[0] is not None:
            yield None, arguments[0]
        elif name in _CONTEXTUAL and len(arguments) > 1 and arguments[1] is not None:
            yield arguments[0], arguments[1]


def _template_calls(text: str) -> Iterator[Msgid]:
    for match in _TEMPLATE_CALL.finditer(text):
        end = match.end()
        while (end := text.find(")", end) + 1) > 0:
            try:
                yield from _calls(ast.parse(text[match.start() : end], mode="eval"))
            except SyntaxError:
                continue
            break


def source_msgids(root: Path) -> set[Msgid]:
    """Every msgid the project's Python sources and templates mark for translation."""
    package = root / "src" / "pyracast"
    found: set[Msgid] = set()
    for path in sorted(package.rglob("*.py")):
        found.update(_calls(ast.parse(path.read_text(encoding="utf-8"))))
    for path in sorted(package.rglob("*")):
        if "templates" in path.parts and path.suffix in {".html", ".txt"}:
            found.update(_template_calls(templatize(path.read_text(encoding="utf-8"), str(path))))
    return found


def _entries(sources: Iterable[Path]) -> dict[Msgid, Path]:
    return {
        (entry.msgctxt, entry.msgid): source
        for source in sources
        for entry in polib.pofile(str(source))
        if not entry.obsolete
    }


def catalog_problems(root: Path) -> list[str]:
    """Msgids no catalogue translates and catalogue msgids no source uses. A project msgid that
    one of Django's or allauth's Turkish catalogues also holds overrides their translation."""
    project = _entries(sorted(root.glob("src/**/locale/tr/LC_MESSAGES/*.po")))
    upstream = _entries(
        source
        for library in (django, allauth)
        for source in sorted(Path(library.__path__[0]).rglob("locale/tr/LC_MESSAGES/*.po"))
    )
    used = source_msgids(root)
    problems = [
        f"Untranslated msgid: {msgid!r}"
        for context, msgid in sorted(used - project.keys() - upstream.keys(), key=repr)
    ]
    problems += [
        f"Orphan msgid: {project[key].relative_to(root).as_posix()}: {key[1]!r}"
        for key in sorted(project.keys() - used - upstream.keys(), key=repr)
    ]
    return problems


def compile_catalogs(root: Path, *, check: bool) -> int:
    for source in sorted(root.glob("src/**/locale/*/LC_MESSAGES/*.po")):
        catalog = polib.pofile(str(source), check_for_duplicates=True)
        if catalog.untranslated_entries() or catalog.fuzzy_entries():
            print(f"Incomplete translation catalog: {source}")
            return 1
        for entry in catalog:
            expected = set(_PLACEHOLDERS.findall(entry.msgid + entry.msgid_plural))
            translations = entry.msgstr_plural.values() if entry.msgid_plural else [entry.msgstr]
            if any(set(_PLACEHOLDERS.findall(text)) != expected for text in translations):
                print(f"Translation placeholders differ: {source}: {entry.msgid}")
                return 1
        compiled = catalog.to_binary()
        if not check:
            source.with_suffix(".mo").write_bytes(compiled)
    if check:
        problems = catalog_problems(root)
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    for target in root.glob("src/**/locale/*/LC_MESSAGES/*.mo"):
        if not target.with_suffix(".po").exists():
            target.unlink()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    return compile_catalogs(Path(__file__).resolve().parents[1], check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
