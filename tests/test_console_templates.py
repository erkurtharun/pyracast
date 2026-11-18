import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORM_ERRORS = re.compile(r"\b\w*form\.errors\b")
CONDITION = re.compile(r"\{%\s*(?:el)?if\b.*?%\}", re.S)


def printed_error_dictionaries(template: str) -> list[str]:
    conditions = [match.span() for match in CONDITION.finditer(template)]
    return [
        match.group()
        for match in FORM_ERRORS.finditer(template)
        if not any(start <= match.start() < end for start, end in conditions)
    ]


def test_no_template_prints_a_form_error_dictionary() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): found
        for path in sorted(ROOT.glob("src/pyracast/**/templates/**/*.html"))
        if (found := printed_error_dictionaries(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}


def test_the_error_dictionary_ratchet_allows_it_only_in_conditions() -> None:
    allowed = (
        "{% if form.errors %}{% elif setup_form.errors or other %}{% endif %}"
        "{{ form.non_field_errors }}{{ field.errors }}{{ form.site_id.errors }}"
    )
    printed = "{{ form.errors }}{{ action_form.errors.as_text }}{% for key in review_form.errors %}"

    assert printed_error_dictionaries(allowed) == []
    assert printed_error_dictionaries(printed) == [
        "form.errors",
        "action_form.errors",
        "review_form.errors",
    ]
