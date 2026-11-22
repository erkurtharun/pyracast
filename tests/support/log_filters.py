import re
from collections.abc import Mapping
from typing import Any

# The part of the Logging query language monitoring.tf uses: comparisons joined by AND, each
# `field=value`, `field:value`, `field:*` or `field=~"RE2 pattern"`, optionally preceded by NOT.
# The rules follow docs.cloud.google.com/logging/docs/view/logging-query-language (read
# 2026-11-21): the right-hand value is first converted to the field's type and a failed
# conversion fails the comparison; `=` and `:` on strings ignore letter case; `:` applies to
# strings; a comparison on a missing field fails and NOT of it holds; `:*` tests presence;
# regular expressions are quoted strings read as RE2, case sensitive and unanchored. A term those
# rules leave open, or one RE2 and Python read apart, raises UnsupportedTermError instead of
# being guessed.
TERM = re.compile(
    r'(?P<negated>NOT )?(?P<field>[\w.]+)(?P<operator>=~|=|:)(?P<value>"(?:[^"\\]|\\.)*"|\S+)'
)
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
PLACEHOLDER = re.compile(r"\$\{([\w.]+)\}")
NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
WORD = re.compile(r"[A-Za-z]+")
# The service needs quotes around a value with other characters, such as `/` or `:`.
UNQUOTED = re.compile(r"\w+")
# Words Python or Logging might read as a number; the documentation does not say.
NUMBER_WORDS = {"nan", "inf", "infinity"}
# RE2's largest repetition count.
REPEAT_BOUND = 1000
REPEAT = re.compile(r"[{]([0-9]+)(?:,([0-9]*))?[}]")
MISSING = object()


class UnsupportedTermError(ValueError):
    """A filter term whose outcome the query language documentation does not settle."""


def unescaped(literal: str) -> str:
    """A quoted string's text; an escape other than of a quote or a backslash is refused."""

    def escaped(match: re.Match[str]) -> str:
        if match[1] not in '"\\':
            raise UnsupportedTermError(f"the escape {match[0]!r}")
        return match[1]

    return re.sub(r"\\(.)", escaped, literal, flags=re.S)


def filter_terms(source: str, values: Mapping[str, str]) -> list[str]:
    """The terms of the first `join(" AND ", [...])` in a Terraform source, interpolated."""
    joined = re.search(r'join\(" AND ", \[\n(.*?)\n\s*\]\)', source, re.S)
    assert joined, source
    terms = [unescaped(literal) for literal in STRING.findall(joined[1])]
    return [PLACEHOLDER.sub(lambda name: values[name[1]], term) for term in terms]


def filter_of(block: str, source: str, values: Mapping[str, str]) -> list[str]:
    """The filter terms of a Terraform block, through the local it names if it names one."""
    if named := re.search(r"\bfilter\s*=\s*local\.(\w+)", block):
        local = re.search(rf"^\s+{named[1]}\s*=\s*(join\(.*?\n\s*\]\))", source, re.S | re.M)
        assert local, named[1]
        block = local[1]
    return filter_terms(block, values)


def resources(source: str, kind: str) -> dict[str, str]:
    return dict(re.findall(rf'^resource "{kind}" "(\w+)" \{{\n(.*?)\n\}}$', source, re.S | re.M))


def field(entry: Mapping[str, Any], path: str) -> object:
    value: object = entry
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return MISSING
        value = value[part]
    return value


def converted(text: str, quoted: bool, actual: object) -> object:
    """The right-hand value in the type of the field, or MISSING when the conversion fails."""
    if isinstance(actual, bool):
        if quoted or not WORD.fullmatch(text):
            raise UnsupportedTermError(f"{text!r} against a boolean")
        return {"true": True, "false": False}.get(text.lower(), MISSING)
    if isinstance(actual, int | float):
        if NUMBER.fullmatch(text):
            return float(text)
        if WORD.fullmatch(text) and text.lower() not in NUMBER_WORDS:
            return MISSING
        raise UnsupportedTermError(f"{text!r} against a number")
    if isinstance(actual, str):
        if not (text.isascii() and actual.isascii()):
            raise UnsupportedTermError("letter case beyond ASCII")
        return text
    raise UnsupportedTermError(f"a comparison with {type(actual).__name__}")


def searched(pattern: str, actual: object) -> bool:
    if not isinstance(actual, str):
        raise UnsupportedTermError("a regular expression on a value that is not a string")
    if any(part in pattern for part in ("\\", "(?", "[:", "{,")) or "$" in pattern[:-1]:
        raise UnsupportedTermError(f"{pattern!r} may read differently in RE2")
    if any(
        int(count or 0) > REPEAT_BOUND for bounds in REPEAT.findall(pattern) for count in bounds
    ):
        raise UnsupportedTermError(f"{pattern!r} repeats more often than RE2 allows")
    # RE2's `$` without the m flag matches only at the end of the text, as Python's \Z does.
    if pattern.endswith("$"):
        pattern = pattern[:-1] + r"\Z"
    return re.search(pattern, actual) is not None


def compared(operator: str, literal: str, actual: object) -> bool:
    quoted = literal.startswith('"')
    text = unescaped(literal[1:-1]) if quoted else literal
    if operator == "=~":
        if not quoted:
            raise UnsupportedTermError("a regular expression that is not a quoted string")
        return searched(text, actual)
    if not quoted and not (UNQUOTED.fullmatch(text) or NUMBER.fullmatch(text)):
        raise UnsupportedTermError(f"the unquoted value {text!r}")
    if operator == ":" and not isinstance(actual, str):
        raise UnsupportedTermError("`:` on a value that is not a string")
    expected = converted(text, quoted, actual)
    if expected is MISSING:
        return False
    if isinstance(actual, str):
        assert isinstance(expected, str)
        if operator == ":":
            return expected.lower() in actual.lower()
        return expected.lower() == actual.lower()
    return expected == actual


def term_matches(term: str, entry: Mapping[str, Any]) -> bool:
    parsed = TERM.fullmatch(term)
    assert parsed, term
    actual = field(entry, parsed["field"])
    if parsed["operator"] == ":" and parsed["value"] == "*":
        found = actual is not MISSING
    elif actual is MISSING:
        found = False
    else:
        found = compared(parsed["operator"], parsed["value"], actual)
    return found != bool(parsed["negated"])


def matches(terms: list[str], entry: Mapping[str, Any]) -> bool:
    return all(term_matches(term, entry) for term in terms)
