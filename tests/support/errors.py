from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from pyracast.errors import DomainError


@contextmanager
def raises_code(code: str) -> Iterator[None]:
    with pytest.raises(DomainError) as error:
        yield
    assert error.value.code == code
