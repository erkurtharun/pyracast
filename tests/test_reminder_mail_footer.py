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
from tests.support.notifications import SLOT, saved

pytestmark = pytest.mark.django_db(transaction=True)

ORIGIN = "https://pyracast.example"
RECIPIENT = "reader@example.com"
FOOTERS = {
    "en": (
        "You receive this email because an administrator of Footer workspace added your "
        "address to the forecast emails of Kayseri solar park. To stop receiving it, ask a "
        "workspace administrator to remove your address."
    ),
    "tr": (
        "Bu e-postay\u0131, Footer workspace çal\u0131şma alan\u0131n\u0131n bir yöneticisi "
        "adresinizi Kayseri solar park tahmin e-postalar\u0131na eklediği için "
        "al\u0131yorsunuz. Almak istemiyorsan\u0131z çal\u0131şma alan\u0131 yöneticisinden "
        "adresinizin ç\u0131kar\u0131lmas\u0131n\u0131 isteyin."
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
    assert message.extra_headers["Auto-Submitted"] == "auto-generated"
    assert message.extra_headers["X-Auto-Response-Suppress"] == "OOF, AutoReply"
    assert message.extra_headers["Content-Language"] == language
    assert RECIPIENT not in str(message.body) + message.extra_headers["List-Unsubscribe"]
