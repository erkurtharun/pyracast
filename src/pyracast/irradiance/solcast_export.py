import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from pathlib import Path
from typing import Any

MAX_JSON_DEPTH = 32


@dataclass(frozen=True)
class PowerFileInterval:
    start: datetime
    end: datetime
    power_kw: float


@dataclass(frozen=True)
class PowerFile:
    intervals: tuple[PowerFileInterval, ...]
    raw_rows: tuple[dict[str, Any], ...]
    sha256: str


def _nonfinite(value: str) -> object:
    raise ValueError("Nonfinite JSON values are not supported.")


def _finite(value: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError("Nonfinite JSON values are not supported.")
    return number


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON object keys are not supported.")
    return result


def _checked_shape(value: object) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise ValueError("The JSON export is nested too deeply.")
        if isinstance(item, str) and "\x00" in item:
            raise ValueError("The file contains a NUL character.")
        if isinstance(item, dict):
            pending.extend((nested, depth + 1) for nested in (*item, *item.values()))
        elif isinstance(item, list):
            pending.extend((nested, depth + 1) for nested in item)


def parse_json(text: str) -> object:
    """Strict JSON for provider exports: no duplicate keys, nonfinite numbers or NUL characters
    (which PostgreSQL cannot store) and at most MAX_JSON_DEPTH levels; every refusal is a
    ValueError."""
    try:
        value = json.loads(
            text, parse_constant=_nonfinite, parse_float=_finite, object_pairs_hook=_unique
        )
    except RecursionError:
        raise ValueError("The JSON export is nested too deeply.") from None
    _checked_shape(value)
    return value


def read_trusted_file(path: Path, *, suffixes: frozenset[str], trusted_root: Path) -> bytes:
    """Reads a file only when it resolves, links included, inside the trusted root."""
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(trusted_root.resolve(strict=True))
        if resolved.suffix.lower() not in suffixes or not resolved.is_file():
            raise ValueError("Invalid input file")
        return resolved.read_bytes()
    except OSError, ValueError:
        kinds = " or ".join(sorted(suffix.lstrip(".").upper() for suffix in suffixes))
        raise ValueError(
            f"Input must be an existing {kinds} file within the trusted directory"
        ) from None


def load_json_file(path: Path, *, trusted_root: Path | None = None) -> tuple[object, str]:
    raw = read_trusted_file(
        path, suffixes=frozenset({".json"}), trusted_root=trusted_root or Path.cwd()
    )
    return parse_json(raw.decode("utf-8")), sha256(raw).hexdigest()
