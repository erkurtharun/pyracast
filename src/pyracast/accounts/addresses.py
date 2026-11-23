import re
from functools import cache

from django.core.exceptions import ValidationError
from django.core.mail.backends.smtp import EmailBackend
from django.core.validators import validate_email

ADDRESS_MAX_OCTETS = 254
LOCAL_MAX_OCTETS = 64
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_DOT_ATOM = re.compile(rf"{_ATOM}(?:\.{_ATOM})*")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


@cache
def _smtp() -> EmailBackend:
    return EmailBackend(alias="addresses", host="localhost")


def is_plain(text: str) -> bool:
    if not text.isascii() or len(text) > ADDRESS_MAX_OCTETS or text.count("@") != 1:
        return False
    local, domain = text.split("@")
    labels = domain.split(".")
    if not (
        len(local) <= LOCAL_MAX_OCTETS
        and _DOT_ATOM.fullmatch(local)
        and len(labels) >= 2
        and all(_LABEL.fullmatch(label) for label in labels)
        and not labels[-1].isdigit()
    ):
        return False
    try:
        validate_email(text)
        return bool(_smtp().prep_address(text) == text)
    except ValidationError, ValueError:
        return False


def plain_address(text: str) -> str:
    """The text unchanged if it is a plain address, otherwise the invalid-address error."""
    if not is_plain(text):
        raise ValidationError(validate_email.message, code=validate_email.code)
    return text
