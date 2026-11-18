import re
from typing import Literal, cast, get_args

Language = Literal["en", "tr"]
LANGUAGES: tuple[Language, ...] = get_args(Language)
LANGUAGE_COOKIE = "pyracast_language"
_LANGUAGE_RANGE = re.compile(
    r"\s*([a-z]{1,8}(?:-[a-z0-9]{1,8})*|\*)\s*"
    r"(?:;\s*q=(0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)\s*)?",
    re.IGNORECASE,
)


def explicit_language(value: str | None) -> Language | None:
    return value if value in LANGUAGES else None


def browser_language(header: str) -> tuple[Language | None, frozenset[str]]:
    if len(header) > 8192:
        return None, frozenset({"*"})
    ranked: list[tuple[float, Language]] = []
    rejected: set[str] = set()
    for item in header.split(","):
        match = _LANGUAGE_RANGE.fullmatch(item)
        if match is None:
            continue
        tag, quality = match.groups()
        base = tag.lower().split("-", 1)[0]
        weight = float(quality) if quality is not None else 1.0
        if weight == 0:
            rejected.add(base)
        elif base in LANGUAGES:
            ranked.append((weight, cast(Language, base)))
    if ranked:
        return max(ranked, key=lambda candidate: candidate[0])[1], frozenset(rejected)
    return None, frozenset(rejected)


def negotiated_language(cookie: str | None, accept_language: str, country: str | None) -> Language:
    chosen = explicit_language(cookie)
    if chosen is not None:
        return chosen
    browser, rejected = browser_language(accept_language)
    if browser is not None:
        return browser
    fallback: Language = "tr" if country == "TR" else "en"
    return fallback if fallback not in rejected and "*" not in rejected else "en"
