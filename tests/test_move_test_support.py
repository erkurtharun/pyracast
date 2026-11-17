import ast
from pathlib import Path

import pytest

from scripts import move_test_support

ALPHA = """\
import pytest

LIMIT = 3


def _double(value: int) -> int:
    return value * 2


# Builds the shared subject.
def helper() -> int:
    return _double(LIMIT)


def test_helper() -> None:
    assert helper() == 6
"""
# Built from PACKAGE so that a rerun of the codemod over tests/ leaves this fixture unchanged.
PACKAGE = "tests"
BETA = f"""\
from {PACKAGE}.test_alpha import helper

BACKEND = "tests.test_alpha.helper"


def test_beta() -> None:
    assert helper() == 6
"""


def _names(path: Path) -> set[str]:
    return {
        node.name
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, ast.FunctionDef)
    }


def test_imported_builders_move_to_the_support_package_once(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_alpha.py").write_text(ALPHA, encoding="utf-8")
    (tests / "test_beta.py").write_text(BETA, encoding="utf-8")

    assert move_test_support.main(tests) == {"alpha": {"helper", "_double", "LIMIT"}}

    support = tests / "support" / "alpha.py"
    assert _names(support) == {"_double", "helper"}
    assert "# Builds the shared subject." in support.read_text(encoding="utf-8")
    assert _names(tests / "test_alpha.py") == {"test_helper"}
    assert "from tests.support.alpha import helper" in (tests / "test_alpha.py").read_text(
        encoding="utf-8"
    )
    beta = (tests / "test_beta.py").read_text(encoding="utf-8")
    assert "from tests.support.alpha import helper" in beta
    assert '"tests.support.alpha.helper"' in beta
    snapshot = {path: path.read_bytes() for path in tests.rglob("*.py")}

    move_test_support.main(tests)

    assert {path: path.read_bytes() for path in tests.rglob("*.py")} == snapshot


def test_moved_fixtures_remain_discoverable_in_the_original_test_module(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    source = """\
import pytest

@pytest.fixture
def value():
    return 3

def helper(value):
    return value

def test_helper(value):
    assert helper(value) == 3
"""
    (tests / "test_alpha.py").write_text(source, encoding="utf-8")
    (tests / "test_beta.py").write_text(BETA, encoding="utf-8")

    move_test_support.main(tests)

    names = {
        alias.name
        for node in ast.parse((tests / "test_alpha.py").read_text(encoding="utf-8")).body
        if isinstance(node, ast.ImportFrom) and node.module == "tests.support.alpha"
        for alias in node.names
    }
    assert names == {"helper", "value"}


def test_relative_imports_are_refused_before_any_file_is_written(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    source = "from .helpers import value\n\ndef helper():\n    return value\n"
    path = tests / "test_alpha.py"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(RuntimeError, match="Relative imports"):
        move_test_support.move(tests, "alpha", {"helper"})

    assert path.read_text(encoding="utf-8") == source
    assert not (tests / "support").exists()
