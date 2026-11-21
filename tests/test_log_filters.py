from typing import Any

import pytest

from tests.support.log_filters import UnsupportedTermError, matches, term_matches


def entry(**payload: Any) -> dict[str, Any]:
    return {"resource": {"type": "gce_instance"}, "jsonPayload": payload}


def test_a_comparison_on_a_missing_field_fails_and_its_negation_holds() -> None:
    line = entry(event="pyracast_backup_failed")

    assert not term_matches("jsonPayload.test=true", line)
    assert term_matches("NOT jsonPayload.test=true", line)
    assert not term_matches('jsonPayload.missing:"x"', line)
    assert not term_matches('jsonPayload.missing=~"x"', line)
    assert not term_matches("jsonPayload.missing.deeper=1", line)


def test_presence_holds_only_for_a_field_the_entry_carries() -> None:
    line = entry(event="pyracast_backup_failed", test=False)

    assert term_matches("jsonPayload.test:*", line)
    assert not term_matches("jsonPayload.other:*", line)
    assert term_matches("NOT jsonPayload.other:*", line)


def test_equality_and_substring_on_strings_ignore_letter_case() -> None:
    line = entry(event="Pyracast_Backup_Failed")

    assert term_matches('jsonPayload.event="pyracast_backup_failed"', line)
    assert term_matches("jsonPayload.event=PYRACAST_BACKUP_FAILED", line)
    assert term_matches('jsonPayload.event:"BACKUP"', line)
    assert not term_matches('NOT jsonPayload.event="PYRACAST_BACKUP_FAILED"', line)


def test_a_regular_expression_keeps_letter_case() -> None:
    line = entry(event="Pyracast_Backup_Failed")

    assert term_matches('jsonPayload.event=~"Backup"', line)
    assert not term_matches('jsonPayload.event=~"backup"', line)


@pytest.mark.parametrize(
    ("value", "found"),
    [(True, True), (False, False), ("true", True), ("TRUE", True), ("false", False), (1, False)],
)
def test_true_is_converted_to_the_type_the_field_holds(value: object, found: bool) -> None:
    line = entry(test=value)

    assert term_matches("jsonPayload.test=true", line) is found
    assert term_matches("NOT jsonPayload.test=true", line) is not found


def test_a_number_compares_with_a_numeric_literal_and_fails_against_text() -> None:
    line = entry(count=3)

    assert term_matches("jsonPayload.count=3", line)
    assert term_matches('jsonPayload.count="3"', line)
    assert term_matches("jsonPayload.count=3.0", line)
    assert not term_matches("jsonPayload.count=4", line)
    assert not term_matches("jsonPayload.count=three", line)


def test_an_anchor_matches_only_at_the_start_or_end_of_the_whole_value() -> None:
    line = entry(message="first line\nsecond line\n")

    assert term_matches('jsonPayload.message=~"^first"', line)
    assert not term_matches('jsonPayload.message=~"^second"', line)
    assert not term_matches('jsonPayload.message=~"line$"', line)
    assert term_matches('jsonPayload.message=~"line$"', entry(message="first line"))


@pytest.mark.parametrize(
    ("term", "value"),
    [
        ('jsonPayload.test="true"', True),
        ('jsonPayload.test:"true"', True),
        ('jsonPayload.count:"3"', 3),
        ('jsonPayload.items="a"', ["a"]),
        ('jsonPayload.items="a"', None),
        ('jsonPayload.items="münchen"', "münchen"),
        ('jsonPayload.items=~"(?i)a"', "a"),
        ('jsonPayload.items=~"a\\\\d"', "a1"),
        ('jsonPayload.items=~"a$b"', "a"),
        ('jsonPayload.count=~"3"', 3),
        ("jsonPayload.count=nan", 3),
        ("jsonPayload.count=1_0", 10),
    ],
)
def test_a_term_the_documentation_does_not_settle_is_refused(term: str, value: object) -> None:
    field = term.split(".", 1)[1].split("=")[0].split(":")[0]

    with pytest.raises(UnsupportedTermError):
        term_matches(term, entry(**{field: value}))


def test_every_term_of_a_filter_must_hold() -> None:
    line = entry(event="pyracast_backup_failed")

    assert matches(
        ['resource.type="gce_instance"', 'jsonPayload.event="pyracast_backup_failed"'], line
    )
    assert not matches(['resource.type="gce_instance"', "jsonPayload.test=true"], line)
