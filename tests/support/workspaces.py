import re

from django.core import mail


def invitation_token() -> str:
    match = re.search(r"token=([A-Za-z0-9_-]+)", str(mail.outbox[-1].body))
    assert match is not None
    return match.group(1)
