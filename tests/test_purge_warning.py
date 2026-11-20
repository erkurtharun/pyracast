from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.test import override_settings
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant, UserLanguagePreference, WorkspaceRole
from pyracast.background.failures import ItemsFailedError
from pyracast.mail.errors import NotSentError
from pyracast.notifications.models import OutboundMail
from pyracast.plants.models import Plant
from pyracast.regions.models import Region
from pyracast.regions.operations import submit_region
from pyracast.trash import RETENTION, purge_expired, restore_region, trash_region
from tests.support.outbox import deliver_mail

pytestmark = pytest.mark.django_db(transaction=True)

ORIGIN = "https://pyracast.example"
NOW = datetime(2026, 11, 20, 9, tzinfo=UTC)
LEAD = timedelta(days=7)


class Recorder(BaseEmailBackend):
    sent: ClassVar[list[EmailMessage]] = []
    failure: ClassVar[BaseException | None] = None

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        assert email_messages is not None
        if self.failure is not None:
            raise self.failure
        self.sent.extend(email_messages)
        return len(email_messages)


@pytest.fixture(autouse=True)
def recorder() -> Iterator[type[Recorder]]:
    Recorder.sent = []
    Recorder.failure = None
    with override_settings(
        MAILERS={"default": {"BACKEND": "tests.test_purge_warning.Recorder"}},
        PYRACAST_PUBLIC_ORIGIN=ORIGIN,
    ):
        yield Recorder


def administrator(tenant: Tenant, address: str, language: str | None = None) -> User:
    user = User.objects.create_user(username=address, email=address)
    EmailAddress.objects.create(user=user, email=address, verified=True, primary=True)
    if language is not None:
        UserLanguagePreference.objects.create(user=user, language=language)
    Membership.objects.create(tenant=tenant, user=user, role=WorkspaceRole.ADMIN)
    return user


def trashed(name: str, deleted_at: datetime, *, language: str | None = None) -> tuple[Region, User]:
    tenant = Tenant.objects.create(name=f"{name} workspace")
    admin = administrator(tenant, f"{name.lower()}@example.com", language)
    region = submit_region(
        admin, tenant.pk, name=name, latitude=40, longitude=29, timezone="Europe/Istanbul"
    )
    Plant.objects.create(region=region, name=f"{name} solar park")
    trash_region(admin, tenant.pk, region.pk)
    Region.objects.filter(pk=region.pk).update(deleted_at=deleted_at)
    return Region.objects.get(pk=region.pk), admin


def warn(now: datetime = NOW) -> int:
    from pyracast.purge_warning import warn_before_purge

    return warn_before_purge(now)


def warnings() -> list[tuple[str, str]]:
    return list(OutboundMail.objects.order_by("pk").values_list("kind", "recipient"))


def test_a_purge_run_deletes_an_expired_trashed_plant_and_keeps_a_younger_one() -> None:
    expired, _ = trashed("Expired", NOW - RETENTION)
    younger, _ = trashed("Younger", NOW - RETENTION + timedelta(seconds=1))

    purge_expired(NOW)

    assert not Region.objects.filter(pk=expired.pk).exists()
    assert not Plant.objects.filter(region_id=expired.pk).exists()
    assert Region.objects.get(pk=younger.pk).deleted_at == NOW - RETENTION + timedelta(seconds=1)
    assert Plant.objects.filter(region=younger).exists()


def test_the_warning_is_written_once_a_week_before_the_purge() -> None:
    due, _ = trashed("Due", NOW - RETENTION + LEAD)
    early, _ = trashed("Early", NOW - RETENTION + LEAD + timedelta(seconds=1))

    assert warn() == 1
    assert warn(NOW + timedelta(hours=1)) == 1
    assert warn(NOW + timedelta(hours=2)) == 0

    assert warnings() == [
        ("plant_purge_warning", "due@example.com"),
        ("plant_purge_warning", "early@example.com"),
    ]
    assert Region.objects.get(pk=due.pk).purge_warning_sent_at == NOW
    assert Region.objects.get(pk=early.pk).purge_warning_sent_at == NOW + timedelta(hours=1)


def test_no_warning_is_written_once_the_purge_is_due() -> None:
    trashed("Overdue", NOW - RETENTION)

    assert warn() == 0
    assert warnings() == []


def test_a_restore_clears_the_warning_and_a_new_trashing_is_warned_again(
    recorder: type[Recorder],
) -> None:
    now = timezone.now()
    region, admin = trashed("Again", now - RETENTION + LEAD)
    warn(now)

    restore_region(admin, region.tenant_id, region.pk)
    assert Region.objects.get(pk=region.pk).purge_warning_sent_at is None
    assert deliver_mail() == 0
    assert OutboundMail.objects.values_list("status", "last_error").get() == (
        "skipped",
        "subject_unavailable",
    )
    trash_region(admin, region.tenant_id, region.pk)
    Region.objects.filter(pk=region.pk).update(deleted_at=now - RETENTION + LEAD)

    assert warn(now) == 1
    assert len(warnings()) == 2
    assert recorder.sent == []


def test_only_the_administrators_of_the_plant_workspace_are_warned() -> None:
    region, _ = trashed("Scoped", NOW - RETENTION + LEAD)
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    EmailAddress.objects.create(user=viewer, email="viewer@example.com", verified=True)
    Membership.objects.create(tenant_id=region.tenant_id, user=viewer, role=WorkspaceRole.VIEWER)
    other = Tenant.objects.create(name="Other")
    administrator(other, "other@example.com")

    warn()

    assert warnings() == [("plant_purge_warning", "scoped@example.com")]


@pytest.mark.parametrize(
    ("language", "subject", "phrase"),
    [
        ("en", "Lake solar park will be deleted permanently", "will be deleted permanently on"),
        (
            "tr",
            "Lake solar park kal\u0131c\u0131 olarak silinecek",
            "kal\u0131c\u0131 olarak silinecek",
        ),
    ],
)
def test_the_warning_mail_names_the_plant_its_deletion_time_and_the_recycle_bin(
    recorder: type[Recorder], language: str, subject: str, phrase: str
) -> None:
    region, _ = trashed("Lake", NOW - RETENTION + LEAD, language=language)
    warn()

    assert deliver_mail(NOW) == 1

    (message,) = recorder.sent
    assert message.to == ["lake@example.com"]
    assert message.subject == subject
    assert phrase in message.body
    assert "27" in message.body
    assert "12:00" in message.body
    assert f"{ORIGIN}/app/w/{region.tenant_id}/trash/" in message.body
    assert f"{ORIGIN}/app/w/{region.tenant_id}/trash/" in message.alternatives[0][0]


def test_the_purge_happens_on_time_when_the_warning_mail_cannot_be_sent(
    recorder: type[Recorder],
) -> None:
    region, _ = trashed("Unsent", NOW - RETENTION + LEAD)
    warn()
    recorder.failure = NotSentError("connection refused")

    deliver_mail(NOW)
    purge_expired(NOW + LEAD)

    assert not Region.objects.filter(pk=region.pk).exists()
    assert OutboundMail.objects.get().status == "retry"


def test_a_warning_that_cannot_be_written_never_holds_back_the_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyracast.orchestration import dispatcher

    now = timezone.now()
    expired, _ = trashed("Gone", now - RETENTION - timedelta(minutes=1))
    waiting, _ = trashed("Waiting", now - RETENTION + LEAD - timedelta(minutes=1))

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("The outbox is unavailable.")

    monkeypatch.setattr("pyracast.purge_warning.enqueue_mail", refuse)

    with pytest.raises(ItemsFailedError):
        dispatcher._retention()

    assert not Region.objects.filter(pk=expired.pk).exists()
    assert Region.objects.get(pk=waiting.pk).purge_warning_sent_at is None
    assert warnings() == []
