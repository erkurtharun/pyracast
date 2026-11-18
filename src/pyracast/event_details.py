from collections.abc import Mapping

type DetailShape = Mapping[str, tuple[type, ...]]

INTEGER: tuple[type, ...] = (int,)
OPTIONAL_INTEGER: tuple[type, ...] = (int, type(None))
TEXT: tuple[type, ...] = (str,)
INTEGERS: tuple[type, ...] = (list,)


def shaped(
    trail: str, action: str, details: Mapping[str, object], shapes: Mapping[str, DetailShape]
) -> dict[str, object]:
    """Return `details` when they carry exactly the keys and value types `shapes` declares for
    the action; anything else is a programming error and is refused before it is stored."""
    shape = shapes.get(action)
    if shape is None:
        raise ValueError(f"The {trail} action '{action}' declares no details.")
    if details.keys() != shape.keys():
        raise ValueError(
            f"The {trail} action '{action}' takes the details {sorted(shape)}, "
            f"not {sorted(details)}."
        )
    for key, kinds in shape.items():
        value = details[key]
        if not isinstance(value, kinds) or (isinstance(value, bool) and bool not in kinds):
            raise ValueError(
                f"The {trail} action '{action}' takes '{key}' as "
                f"{' or '.join(kind.__name__ for kind in kinds)}."
            )
    return dict(details)
