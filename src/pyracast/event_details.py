from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Kind:
    name: str
    accepts: Callable[[object], bool]


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: object) -> bool:
    return isinstance(value, str)


def _integers(value: object) -> bool:
    return isinstance(value, list) and all(map(_integer, value))


INTEGER = Kind("an integer", _integer)
OPTIONAL_INTEGER = Kind("an integer or null", lambda value: value is None or _integer(value))
TEXT = Kind("text", _text)
OPTIONAL_TEXT = Kind("text or null", lambda value: value is None or _text(value))
INTEGERS = Kind("a list of integers", _integers)

type DetailShape = Mapping[str, Kind]


def shaped(
    trail: str, action: str, details: Mapping[str, object], shapes: Mapping[str, DetailShape]
) -> dict[str, object]:
    shape = shapes.get(action)
    if shape is None:
        raise ValueError(f"The {trail} action '{action}' declares no details.")
    if details.keys() != shape.keys():
        raise ValueError(
            f"The {trail} action '{action}' takes the details {sorted(shape)}, "
            f"not {sorted(details)}."
        )
    for key, kind in shape.items():
        if not kind.accepts(details[key]):
            raise ValueError(f"The {trail} action '{action}' takes '{key}' as {kind.name}.")
    return dict(details)
