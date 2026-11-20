import pytest
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings

from pyracast.notifications import jobs
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from pyracast.plants.models import Plant
from tests.support.domain import NOW, make_system
from tests.support.notifications import SLOT, saved

pytestmark = pytest.mark.django_db(transaction=True)

ORIGIN = "https://pyracast.example"
RECIPIENT = "reader@example.com"
FOOTERS = {
    "en": (
        "You receive this email because an administrator of Footer workspace added your "
        "address to the reminders of Kayseri solar park. To stop them, ask that administrator "
        "to remove your address."
    ),
    "tr": (
        "Bu e-postay\u0131, Footer workspace yöneticilerinden biri adresinizi "
        "Kayseri solar park hat\u0131rlatmalar\u0131na eklediği için al\u0131yorsunuz. "
        "Durdurmak için o yöneticiden adresinizi ç\u0131karmas\u0131n\u0131 isteyin."
    ),
}


def delivery(language: str) -> NotificationDelivery:
    system = make_system(approved=True)
    system.tenant.name = "Footer workspace"
    system.tenant.save(update_fields=["name"])
    Plant.objects.create(region=system.region, name="Kayseri solar park")
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language=language,
        enabled=True,
        recipients=[RECIPIENT],
        times=["10:00"],
        now=NOW,
    )
    saved(system)
    (delivery_id,) = enqueue_due(SLOT)
    return NotificationDelivery.objects.get(pk=delivery_id)


def sent(delivery: NotificationDelivery) -> EmailMultiAlternatives:
    messages: list[EmailMultiAlternatives] = []
    assert jobs.process_notification(delivery.pk, SLOT, sender=messages.append)
    (message,) = messages
    return message


def unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> EmailMultiAlternatives:
        raise RuntimeError("The renderer cannot be used.")

    monkeypatch.setattr("pyracast.notifications.jobs.build_message", broken)


@override_settings(PYRACAST_PUBLIC_ORIGIN=ORIGIN)
@pytest.mark.parametrize("language", ["en", "tr"])
def test_the_unavailable_notice_names_the_plant_links_to_it_and_uses_plant_time(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    unavailable(monkeypatch)
    reminder = delivery(language)

    message = sent(reminder)

    region = reminder.schedule.region
    subjects = {
        "en": "Kayseri solar park: forecast unavailable for 11 Nov 2026",
        "tr": "Kayseri solar park: 11 Kas 2026 tahmini kullan\u0131lam\u0131yor",
    }
    assert message.subject == subjects[language]
    assert "Kayseri solar park" in message.body
    assert "10:00 (Europe/Istanbul)" in message.body
    assert SLOT.isoformat() not in message.body
    assert f"{ORIGIN}/app/w/{region.tenant_id}/plants/{region.pk}/forecast/" in message.body


@pytest.mark.parametrize("kind", ["forecast", "unavailable"])
@pytest.mark.parametrize("language", ["en", "tr"])
def test_every_reminder_mail_says_why_it_arrives_and_how_to_stop_it(
    monkeypatch: pytest.MonkeyPatch, kind: str, language: str
) -> None:
    if kind == "unavailable":
        unavailable(monkeypatch)
    reminder = delivery(language)

    message = sent(reminder)

    assert FOOTERS[language] in " ".join(str(message.body).split())
    if kind == "forecast":
        assert FOOTERS[language] in " ".join(str(message.alternatives[0][0]).split())
    assert (
        message.extra_headers["List-Unsubscribe"]
        == "<mailto:noreply@localhost?subject=unsubscribe>"
    )
    assert RECIPIENT not in str(message.body) + message.extra_headers["List-Unsubscribe"]
