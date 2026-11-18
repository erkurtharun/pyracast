from datetime import datetime, timedelta

from django.utils import timezone


def deliver_mail(now: datetime | None = None) -> int:
    from pyracast.notifications.outbox import pending_mail_ids, send_outbound_mail

    moment = now or timezone.now()
    sent = 0
    for _ in range(10):
        ids = pending_mail_ids(moment)
        if not ids:
            return sent
        sent += sum(send_outbound_mail(mail_id, moment) for mail_id in ids)
    raise AssertionError("The outbox did not drain.")


def after_the_lease(now: datetime | None = None) -> datetime:
    from pyracast.notifications.outbox import LEASE_DURATION

    return (now or timezone.now()) + LEASE_DURATION + timedelta(seconds=1)
