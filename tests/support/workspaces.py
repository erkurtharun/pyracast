import re

from django.core import mail

from tests.support.outbox import deliver_mail


def invitation_token() -> str:
    deliver_mail()
    match = re.search(r"token=([A-Za-z0-9_-]+)", str(mail.outbox[-1].body))
    assert match is not None
    return match.group(1)
