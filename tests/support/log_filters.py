import json
import re
from collections.abc import Mapping
from typing import Any

# The part of the Logging query language monitoring.tf uses: comparisons joined by AND, each
# `field=value`, `field:"substring"` or `field=~"RE2 pattern"`, optionally preceded by NOT.
TERM = re.compile(
    r'(?P<negated>NOT )?(?P<field>[\w.]+)(?P<operator>=~|=|:)(?P<value>"(?:[^"\\]|\\.)*"|\S+)'
)
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
PLACEHOLDER = re.compile(r"\$\{([\w.]+)\}")
MISSING = object()


def unescaped(literal: str) -> str:
    return re.sub(r"\\(.)", r"\1", literal)


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


def term_matches(term: str, entry: Mapping[str, Any]) -> bool:
    parsed = TERM.fullmatch(term)
    assert parsed, term
    actual = field(entry, parsed["field"])
    literal = parsed["value"]
    expected = unescaped(literal[1:-1]) if literal.startswith('"') else json.loads(literal)
    # A comparison of a missing field fails, so NOT of it holds.
    if actual is MISSING:
        found = False
    elif parsed["operator"] == "=":
        found = actual == expected
    elif not isinstance(actual, str):
        found = False
    elif parsed["operator"] == ":":
        found = str(expected) in actual
    else:
        found = re.search(str(expected), actual) is not None
    return found != bool(parsed["negated"])


def matches(terms: list[str], entry: Mapping[str, Any]) -> bool:
    return all(term_matches(term, entry) for term in terms)
