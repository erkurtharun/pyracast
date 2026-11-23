import ast
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from django.conf import settings

from pyracast import settings as project_settings

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


def test_signups_answer_without_the_minimum_answer_time_in_the_suite() -> None:
    assert project_settings.PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS == 1
    assert settings.PYRACAST_SIGNUP_MINIMUM_ANSWER_SECONDS == 0


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


TOOLING_PATTERN = (
    r"(?m)^\s*(?:from|import) (?:deploy|scripts)\b"
    r'|/ "(?:deploy|\.agents|\.github)\b'
    r'|"\.(?:github|agents)/'
    r"|tests\.support\.workflows"
)


def test_tooling_marks_every_module_that_exercises_repository_tooling() -> None:
    from tests.conftest import TOOLING

    assert _modules_matching(TOOLING_PATTERN) | {"test_suite_conventions"} == TOOLING


def test_browser_scenarios_are_marked_and_left_out_of_the_default_run() -> None:
    from tests.conftest import MODULE_MARKERS

    scenarios = {path.stem for path in (TESTS / "e2e").glob("test_*.py")}
    pytest_options = tomllib.loads((TESTS.parent / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = pytest_options["tool"]["pytest"]["addopts"]

    assert scenarios
    assert {module for module, markers in MODULE_MARKERS.items() if "browser" in markers} == (
        scenarios
    )
    assert not scenarios & {path.stem for path in TESTS.glob("test_*.py")}
    assert addopts[addopts.index("-m") + 1] == "not browser"


def test_the_invariant_audit_names_existing_database_modules() -> None:
    from tests.conftest import INVARIANT_AUDIT

    assert _modules_matching(r"django_db") >= INVARIANT_AUDIT
    assert {"test_admin_health_queries", "test_admin_health_views"} <= INVARIANT_AUDIT


def test_selection_markers_are_applied_by_module(request: pytest.FixtureRequest) -> None:
    assert request.node.get_closest_marker("tooling") is not None
    assert request.node.get_closest_marker("migration") is None


def test_the_coverage_floor_and_every_scanner_read_the_deployment_code() -> None:
    root = TESTS.parent
    coverage = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]
    sonar = dict(
        line.split("=", 1)
        for line in (root / "sonar-project.properties").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    qodana = yaml.safe_load((root / "qodana.yaml").read_text(encoding="utf-8"))
    (skipped,) = [item["paths"] for item in qodana["exclude"] if item["name"] == "All"]

    assert coverage["coverage"]["run"]["source"] == ["src", "deploy"]
    assert coverage["coverage"]["report"]["fail_under"] == 100
    assert {"src", "scripts", "deploy", ".github"} <= set(sonar["sonar.sources"].split(","))
    assert not [
        pattern
        for pattern in sonar["sonar.coverage.exclusions"].split(",")
        if pattern.startswith(("src", "deploy"))
    ]
    assert {"deploy", "scripts", ".agents/hooks.py"} <= set(coverage["mypy"]["files"])
    assert skipped == ["web"]


@pytest.mark.parametrize("name", ["qodana.yaml", "qodana-js.yaml"])
def test_each_language_scan_fails_on_a_critical_or_high_finding(name: str) -> None:
    configuration = yaml.safe_load((TESTS.parent / name).read_text(encoding="utf-8"))

    assert configuration["failureConditions"]["severityThresholds"] == {"critical": 0, "high": 0}


@pytest.mark.parametrize("name", ["qodana.yaml", "qodana-js.yaml"])
def test_each_language_scan_enables_dependency_vulnerability_inspections(name: str) -> None:
    configuration = yaml.safe_load((TESTS.parent / name).read_text(encoding="utf-8"))

    assert any(
        inspection.get("inspection") == "VulnerableLibrariesLocal"
        and inspection.get("enabled") is True
        for inspection in configuration["profile"]["inspections"]
    )


def test_the_web_scan_adds_defect_inspections_to_the_recommended_profile_not_style_groups() -> None:
    configuration = yaml.safe_load((TESTS.parent / "qodana-js.yaml").read_text(encoding="utf-8"))
    enabled = [item for item in configuration["profile"]["inspections"] if item.get("enabled")]
    groups = {item["group"] for item in enabled if "group" in item}
    inspections = {item["inspection"] for item in enabled if "inspection" in item}

    assert configuration["profile"]["base"]["name"] == "qodana.recommended"
    assert "category:JavaScript and TypeScript" not in groups
    assert (
        not {
            f"category:JavaScript and TypeScript/{name}"
            for name in ("Code style issues", "Function metrics", "Naming conventions")
        }
        & groups
    )
    assert {
        "JSUnresolvedReference",
        "JSValidateTypes",
        "JSCheckFunctionSignatures",
        "JSDeprecatedSymbols",
    } <= inspections
    assert {
        "category:JavaScript and TypeScript/Unused symbols",
        "category:JavaScript and TypeScript/Probable bugs",
        "category:RegExp",
    } <= groups
    assert {"inspection": "RegExpAnonymousGroup", "enabled": False} in configuration["profile"][
        "inspections"
    ]
    assert {"name": "Eslint"} in configuration["include"]
