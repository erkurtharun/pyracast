import ast
import re
from pathlib import Path

import pytest
from django.conf import settings

TESTS = Path(__file__).resolve().parent
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_no_test_module_imports_another_test_module() -> None:
    importers = {
        str(path.relative_to(TESTS)): sorted(
            module for module in _imported_modules(path) if re.match(r"tests\.test_", module)
        )
        for path in sorted(TESTS.rglob("*.py"))
    }

    assert {path: modules for path, modules in importers.items() if modules} == {}


def test_mail_is_captured_in_memory_for_every_mailer() -> None:
    assert settings.MAILERS
    assert {mailer["BACKEND"] for mailer in settings.MAILERS.values()} == {LOCMEM}


def _modules_matching(pattern: str) -> set[str]:
    return {
        path.stem
        for path in TESTS.glob("test_*.py")
        if re.search(pattern, path.read_text(encoding="utf-8"))
    }


@pytest.mark.parametrize(
    ("marker", "pattern"),
    [
        ("migration", r"MigrationExecutor"),
        ("subprocess", r"subprocess\.(run|Popen|check_output|check_call)\("),
    ],
)
def test_selection_markers_cover_every_module_that_needs_them(marker: str, pattern: str) -> None:
    from tests.conftest import MODULE_MARKERS

    marked = {module for module, markers in MODULE_MARKERS.items() if marker in markers}

    assert marked == _modules_matching(pattern) - {"test_suite_conventions"}


def test_selection_markers_are_applied_by_module(request: pytest.FixtureRequest) -> None:
    assert request.node.get_closest_marker("tooling") is not None
    assert request.node.get_closest_marker("migration") is None
