import ast
import re
from pathlib import Path

import pytest
import yaml
from django.conf import settings

TESTS = Path(__file__).resolve().parent
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


def _imported_modules(path: Path, root: Path) -> set[str]:
    package = ["tests", *path.relative_to(root).parent.parts]
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *([node.module] if node.module else [])])
            modules.add(module)
            modules.update(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def _test_module_imports(root: Path) -> dict[str, list[str]]:
    importers = {
        path.relative_to(root).as_posix(): sorted(
            {
                match.group()
                for module in _imported_modules(path, root)
                if (match := re.match(r"tests\.test_\w+", module))
            }
        )
        for path in sorted(root.rglob("*.py"))
    }
    return {path: modules for path, modules in importers.items() if modules}


def test_no_test_module_imports_another_test_module() -> None:
    assert _test_module_imports(TESTS) == {}


def test_the_import_ratchet_resolves_relative_and_package_imports(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    (tests / "support").mkdir(parents=True)
    (tests / "test_beta.py").write_text(
        "from . import test_alpha\nfrom .test_alpha import helper\n", encoding="utf-8"
    )
    (tests / "test_gamma.py").write_text(
        "from tests import test_alpha, conftest\nimport tests.test_alpha\n", encoding="utf-8"
    )
    (tests / "support" / "builders.py").write_text(
        "from ..test_alpha import helper\nfrom . import domain\nfrom tests.support import x\n",
        encoding="utf-8",
    )
    (tests / "test_alpha.py").write_text("from tests.support.domain import NOW\n", encoding="utf-8")

    assert _test_module_imports(tests) == {
        "support/builders.py": ["tests.test_alpha"],
        "test_beta.py": ["tests.test_alpha"],
        "test_gamma.py": ["tests.test_alpha"],
    }


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
        ("migration", r"MigrationExecutor|from tests\.support\.migrations import"),
        ("subprocess", r"subprocess\.(run|Popen|check_output|check_call)\("),
    ],
)
def test_selection_markers_cover_every_module_that_needs_them(marker: str, pattern: str) -> None:
    from tests.conftest import MODULE_MARKERS

    marked = {module for module, markers in MODULE_MARKERS.items() if marker in markers}

    assert marked == _modules_matching(pattern) - {"test_suite_conventions"}


def test_the_invariant_audit_names_existing_database_modules() -> None:
    from tests.conftest import INVARIANT_AUDIT

    assert _modules_matching(r"django_db") >= INVARIANT_AUDIT


def test_selection_markers_are_applied_by_module(request: pytest.FixtureRequest) -> None:
    assert request.node.get_closest_marker("tooling") is not None
    assert request.node.get_closest_marker("migration") is None


@pytest.mark.parametrize("name", ["qodana.yaml", "qodana-js.yaml"])
def test_each_language_scan_enables_dependency_vulnerability_inspections(name: str) -> None:
    configuration = yaml.safe_load((TESTS.parent / name).read_text(encoding="utf-8"))

    assert any(
        inspection.get("inspection") == "VulnerableLibrariesLocal"
        and inspection.get("enabled") is True
        for inspection in configuration["profile"]["inspections"]
    )
