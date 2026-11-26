from html import unescape

import pytest
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings

from pyracast.connections.models import ProviderAccount
from pyracast.notifications import jobs
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from pyracast.plants.models import Plant
from tests.support.domain import NOW, make_system
from tests.support.notification_transmission import replying
from tests.support.notifications import ABSENT_HEADERS, SLOT, saved

pytestmark = pytest.mark.django_db(transaction=True)

KEPT_HEADERS = ("Auto-Submitted", "X-Auto-Response-Suppress", "Content-Language")
ORIGIN = "https://pyracast.example"
RECIPIENT = "reader@example.com"
FOOTERS = {
    "en": (
        "Forecast email of Kayseri solar park in Footer workspace. The workspace's "
        "administrators manage who receives it."
    ),
    "tr": (
        "Footer workspace çal\u0131şma alan\u0131ndaki Kayseri solar park santralinin tahmin "
        "e-postas\u0131. Al\u0131c\u0131lar\u0131 çal\u0131şma alan\u0131 yöneticileri belirler."
    ),
}


def delivery(language: str, times: tuple[str, ...] = ("10:00",)) -> NotificationDelivery:
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
        times=list(times),
        now=NOW,
    )
    saved(system)
    (delivery_id,) = enqueue_due(SLOT)
    return NotificationDelivery.objects.get(pk=delivery_id)


def sent(delivery: NotificationDelivery) -> EmailMultiAlternatives:
    with replying(lambda message: None) as messages:
        assert jobs.process_notification(delivery.pk, SLOT)
    (message,) = messages
    assert isinstance(message, EmailMultiAlternatives)
    return message


def unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> EmailMultiAlternatives:
        raise RuntimeError("The renderer cannot be used.")

    monkeypatch.setattr("pyracast.notifications.jobs.build_message", broken)


@override_settings(PYRACAST_PUBLIC_ORIGIN=ORIGIN)
@pytest.mark.parametrize("language", ["en", "tr"])
def test_the_failure_notice_says_no_forecast_was_prepared_and_links_to_the_plant(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    unavailable(monkeypatch)
    reminder = delivery(language)

    message = sent(reminder)

    region = reminder.schedule.region
    subject, sentence, action = {
        "en": (
            "Kayseri solar park: no forecast for Wed 11 Nov",
            "The forecast for Wednesday 11 November 2026 could not be prepared. This email "
            "contains no figures.",
            "Open plant in Pyracast:",
        ),
        "tr": (
            "Kayseri solar park: 11 Kas\u0131m Çarşamba tahmini haz\u0131rlanamad\u0131",
            "11 Kas\u0131m 2026 Çarşamba için üretim tahmini haz\u0131rlanamad\u0131. Bu "
            "e-postada rakam yoktur.",
            "Santrali Pyracast'te aç:",
        ),
    }[language]
    assert message.subject == subject
    assert message.body.startswith(f"{sentence}\n\n{action}\n")
    assert f"\n{ORIGIN}/app/w/{region.tenant_id}/plants/{region.pk}/forecast/\n" in message.body
    assert "Europe/Istanbul" not in message.body
    assert not message.alternatives
    assert not message.attachments


@pytest.mark.parametrize(
    ("kind", "language", "subject"),
    [
        ("forecast", "en", "Kayseri solar park: no forecast for Wed 11 Nov (10:00 edition)"),
        (
            "unavailable",
            "tr",
            "Kayseri solar park: 11 Kas\u0131m Çarşamba tahmini haz\u0131rlanamad\u0131 "
            "(10:00 gönderimi)",
        ),
    ],
)
def test_a_mail_without_figures_names_its_clock_when_the_day_has_two(
    monkeypatch: pytest.MonkeyPatch, kind: str, language: str, subject: str
) -> None:
    if kind == "unavailable":
        unavailable(monkeypatch)
    reminder = delivery(language, ("10:00", "12:00"))

    message = sent(reminder)

    assert message.subject == subject
    assert not message.attachments
    assert "10:00" not in message.body


@pytest.mark.parametrize("times", [("10:00",), ("10:00", "12:00")])
def test_only_clocks_the_provider_quota_allows_make_a_second_edition(
    times: tuple[str, ...],
) -> None:
    reminder = delivery("en", times)
    ProviderAccount.objects.filter(pk=reminder.binding.provider_account_id).update(daily_quota=1)

    message = sent(reminder)

    assert message.subject == "Kayseri solar park: no forecast for Wed 11 Nov"


@pytest.mark.parametrize("kind", ["forecast", "unavailable"])
@pytest.mark.parametrize("language", ["en", "tr"])
def test_every_reminder_mail_names_its_plant_workspace_and_who_manages_its_recipients(
    monkeypatch: pytest.MonkeyPatch, kind: str, language: str
) -> None:
    if kind == "unavailable":
        unavailable(monkeypatch)
    reminder = delivery(language)

    message = sent(reminder)

    assert str(message.body).endswith(f"\n\n--\n{FOOTERS[language]}")
    if kind == "forecast":
        assert FOOTERS[language] in " ".join(unescape(str(message.alternatives[0][0])).split())
    assert {name: message.extra_headers[name] for name in KEPT_HEADERS} == {
        "Auto-Submitted": "auto-generated",
        "X-Auto-Response-Suppress": "OOF, AutoReply",
        "Content-Language": language,
    }
    wire = message.message()
    assert [name for name in ABSENT_HEADERS if wire[name] is not None] == []
    assert RECIPIENT not in str(message.body)
