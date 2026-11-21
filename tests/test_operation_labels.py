from datetime import timedelta
from typing import NoReturn

import pytest
from django.db.models import TextChoices
from django.utils import timezone, translation

from pyracast.accounts.models import UserLanguagePreference
from pyracast.forecasts.models import CollectionJob, CollectionJobError, CollectionJobStatus
from pyracast.metering.models import EpiasCollectionJob, EpiasJobError, EpiasJobStatus
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    ReminderSchedule,
)
from pyracast.notifications.scheduler import PREPARATION_GRACE
from pyracast.plants.models import (
    CalibrationError,
    CalibrationRun,
    CalibrationStatus,
    PlantConfiguration,
)
from tests.support.api import verified_admin
from tests.support.delivery_health import NOW
from tests.support.metering import connected
from tests.support.notifications import freeze

VOCABULARIES: tuple[type[TextChoices], ...] = (
    CollectionJobStatus,
    CollectionJobError,
    EpiasJobStatus,
    EpiasJobError,
    NotificationDeliveryStatus,
    NotificationDeliveryError,
    CalibrationStatus,
    CalibrationError,
)


def unreachable() -> NoReturn:
    raise ConnectionError("no broker in this test")


@pytest.mark.parametrize("vocabulary", VOCABULARIES, ids=lambda vocabulary: vocabulary.__name__)
def test_every_console_job_status_and_error_has_a_turkish_label(
    vocabulary: type[TextChoices],
) -> None:
    for choice in vocabulary:
        with translation.override("en"):
            english = str(choice.label)
        with translation.override("tr"):
            turkish = str(choice.label)
        assert turkish != english, choice.value


@pytest.mark.django_db(transaction=True)
def test_the_operations_tables_label_statuses_and_errors_in_turkish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, plant, _ = connected(monkeypatch)
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    for status, error in (("superseded", ""), ("failed", "attempt_limit")):
        CalibrationRun.objects.create(
            plant=plant,
            configuration=PlantConfiguration.objects.get(plant=plant),
            binding=system.binding,
            requested_by=system.member,
            window_start=NOW.date() - timedelta(days=7),
            window_end=NOW.date() - timedelta(days=1),
            cutoff_at=NOW - timedelta(days=1),
            status=status,
            attempts=3,
            last_error=error,
            completed_at=NOW,
        )
    monkeypatch.setattr("pyracast.background.broker.make_broker", unreachable)
    CollectionJob.objects.filter(binding=system.binding).update(
        status="retry", last_error="provider_unreachable", scheduled_for=NOW
    )
    EpiasCollectionJob.objects.filter(
        pk__in=EpiasCollectionJob.objects.order_by("pk").values("pk")[:1]
    ).update(status="failed", last_error="epias_credentials_rejected", scheduled_for=NOW)
    schedule = ReminderSchedule.objects.create(region=system.region, effective_from=NOW)
    delivery = NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=NOW + timedelta(minutes=1),
        preparation_deadline=NOW + PREPARATION_GRACE,
        available_at=NOW,
        status="cancelled",
        last_error="cancelled_by_settings_change",
    )
    freeze(delivery, ["recipient@example.com"])
    client, user = verified_admin()
    UserLanguagePreference.objects.create(user=user, language="tr")

    content = client.get("/admin/operations/").content.decode()

    for turkish in (
        "Yeniden denenecek",
        "Sağlay\u0131c\u0131ya ulaş\u0131lamad\u0131",
        "EPİAŞ kimlik bilgileri reddedildi",
        "İptal edildi",
        "Ayar değişikliğiyle iptal edildi",
        "Yerini yenisi ald\u0131",
        "Deneme s\u0131n\u0131r\u0131na ulaş\u0131ld\u0131",
    ):
        assert turkish in content, turkish
    for english in (
        "Provider unreachable",
        "Epias credentials rejected",
        "Cancelled by settings change",
        "Superseded",
        "Attempt limit reached",
    ):
        assert english not in content, english
