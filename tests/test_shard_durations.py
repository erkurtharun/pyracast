import json
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from scripts import shard_durations
from scripts.shard_durations import junit_cases, module_durations, module_file


def junit(path: Path, cases: list[tuple[str, str, str]]) -> Path:
    rows = "".join(
        f"<testcase classname={quoteattr(classname)} name={quoteattr(name)}"
        f" time={quoteattr(time)} />"
        for classname, name, time in cases
    )
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?><testsuites>{rows}</testsuites>', encoding="utf-8"
    )
    return path


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for module in ("tests/test_a.py", "tests/test_b.py", "tests/e2e/test_c.py"):
        (tmp_path / module).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / module).write_text("", encoding="utf-8")
    durations = tmp_path / "tests" / "shard-durations.json"
    durations.write_text(
        '{"Windows": {"tests/test_a.py": 9.0}, "Linux": {"tests/test_old.py": 1.0}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(shard_durations, "DURATIONS", durations)
    return tmp_path


def test_each_module_records_the_rounded_sum_of_its_tests_across_results(checkout: Path) -> None:
    first = junit(
        checkout / "first.xml",
        [
            ("tests.test_a", "test_one", "1.04"),
            ("tests.test_b.TestGroup", "test_grouped", "0.5"),
            ("tests.e2e.test_c", "test_page", "2"),
        ],
    )
    second = junit(checkout / "second.xml", [("tests.test_a", "test_two[x-1]", "0.02")])

    assert module_durations([first, second], checkout) == {
        "tests/e2e/test_c.py": 2.0,
        "tests/test_a.py": 1.1,
        "tests/test_b.py": 0.5,
    }


@pytest.mark.parametrize(
    ("classname", "expected"),
    [
        ("tests.test_a", "tests/test_a.py"),
        ("tests.test_a.TestGroup", "tests/test_a.py"),
        ("tests.e2e.test_c", "tests/e2e/test_c.py"),
        ("tests.test_gone", None),
        ("tests.test-a", None),
        ("", None),
    ],
)
def test_a_class_name_resolves_to_the_test_file_of_the_checkout(
    checkout: Path, classname: str, expected: str | None
) -> None:
    assert module_file(classname, checkout) == expected


def test_a_test_recorded_in_two_results_is_refused(checkout: Path) -> None:
    case = [("tests.test_a", "test_one", "1")]
    results = [junit(checkout / "first.xml", case), junit(checkout / "second.xml", case)]

    with pytest.raises(ValueError, match="more than one result"):
        module_durations(results, checkout)


def test_a_result_with_a_document_type_declaration_is_refused(tmp_path: Path) -> None:
    result = tmp_path / "result.xml"
    result.write_text(
        '<?xml version="1.0"?><!DOCTYPE testsuites [<!ENTITY name "x">]>'
        '<testsuites><testcase classname="tests.test_a" name="&name;" time="1" /></testsuites>',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="document type"):
        junit_cases(result, tmp_path)


@pytest.mark.parametrize(
    "attributes",
    [
        'classname="tests.test_a" name="test_one"',
        'classname="tests.test_a" time="1"',
        'name="test_one" time="1"',
        'classname="tests.test_a" name="test_one" time="-1"',
        'classname="tests.test_a" name="test_one" time="nan"',
        'classname="tests.test_a" name="test_one" time="inf"',
        'classname="tests.test_a" name="test_one" time="soon"',
    ],
)
def test_a_testcase_without_a_valid_name_or_time_is_refused(
    tmp_path: Path, attributes: str
) -> None:
    result = tmp_path / "result.xml"
    result.write_text(f"<testsuites><testcase {attributes} /></testsuites>", encoding="utf-8")

    with pytest.raises(ValueError, match=r"testcase|time|float"):
        junit_cases(result, tmp_path)


def test_recording_rewrites_one_system_and_keeps_the_others(checkout: Path) -> None:
    result = junit(
        checkout / "result.xml",
        [("tests.test_b", "test_two", "3.25"), ("tests.test_a", "test_one", "0.04")],
    )
    durations = checkout / "tests" / "shard-durations.json"

    assert shard_durations.main(["Linux", str(result)]) == 0
    written = durations.read_bytes()
    assert shard_durations.main(["Linux", str(result)]) == 0

    assert durations.read_bytes() == written
    assert written.decode("utf-8") == (
        "{\n"
        '  "Linux": {\n'
        '    "tests/test_a.py": 0.0,\n'
        '    "tests/test_b.py": 3.2\n'
        "  },\n"
        '  "Windows": {\n'
        '    "tests/test_a.py": 9.0\n'
        "  }\n"
        "}\n"
    )


@pytest.mark.parametrize(
    "content",
    [
        "<testsuites><testcase",
        '<testsuites><testcase classname="tests.test_gone" name="t" time="1" /></testsuites>',
    ],
    ids=["malformed", "no-module-of-the-checkout"],
)
def test_recording_refuses_results_it_cannot_use_and_keeps_the_file(
    checkout: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    result = checkout / "result.xml"
    result.write_text(content, encoding="utf-8")
    durations = checkout / "tests" / "shard-durations.json"
    before = durations.read_bytes()

    assert shard_durations.main(["Windows", str(result)]) == 2

    assert "Cannot record test durations" in capsys.readouterr().err
    assert durations.read_bytes() == before


def test_recording_refuses_a_duration_file_without_systems(checkout: Path) -> None:
    durations = checkout / "tests" / "shard-durations.json"
    durations.write_text(json.dumps([]), encoding="utf-8")
    result = junit(checkout / "result.xml", [("tests.test_a", "test_one", "1")])

    assert shard_durations.main(["Windows", str(result)]) == 2
    assert durations.read_text(encoding="utf-8") == "[]"


def test_duration_command_keeps_its_direct_entrypoint() -> None:
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/shard_durations.py", "--help"],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "{Linux,Windows}" in result.stdout


def test_a_result_outside_the_checkout_is_refused(
    checkout: Path, tmp_path_factory: pytest.TempPathFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = junit(
        tmp_path_factory.mktemp("outside") / "result.xml", [("tests.test_a", "test_x", "1.0")]
    )

    assert shard_durations.main(["Windows", str(outside)]) == 2

    assert "inside the checkout" in capsys.readouterr().err
