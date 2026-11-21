import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import UserLanguagePreference
from pyracast.forecasts.models import (
    CollectionJob,
    DispatcherPhase,
    DispatcherStatus,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.plants.models import CalibrationRun, Plant, PlantConfiguration
from tests.support.api import verified_admin
from tests.support.delivery_health import NOW, recent_forecast
from tests.support.domain import System, make_system
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)
TURKISH = {
    "Calibration runs": "Kalibrasyon çal\u0131şmalar\u0131",
    "Failed calibrations": "Başar\u0131s\u0131z kalibrasyonlar",
    "Workers": "Arka plan işleri",
    "Queues": "Kuyruklar",
    "Dead letters": "Teslim edilemeyen mesajlar",
    "Provider requests per plant": "Santral baş\u0131na sağlay\u0131c\u0131 istekleri",
    "Message broker could not be read; queue counts are not shown.": (
        "Mesaj kuyruğu okunamad\u0131; kuyruk say\u0131lar\u0131 gösterilmiyor."
    ),
    "Message broker not answering": "Mesaj kuyru\u011fu yan\u0131t vermiyor",
    "Checked at": "Kontrol zaman\u0131",
    "Why listed": "Listelenme nedeni",
    "No forecast saved yet": "Henüz kaydedilmiş tahmin yok",
    "No plants currently match this check.": "Bu kontrole şu anda uyan santral yok.",
}


def console(language: str) -> Client:
    client, user = verified_admin()
    if language == "tr":
        UserLanguagePreference.objects.create(user=user, language="tr")
    return client


def text(language: str, english: str) -> str:
    return TURKISH[english] if language == "tr" else english


def dispatched(**fields: Any) -> None:
    for phase in DispatcherPhase.values:
        DispatcherStatus.objects.update_or_create(
            phase=phase,
            defaults={"attempted_at": NOW, "succeeded_at": NOW, "last_error": "", **fields},
        )


class Channel:
    def queue_declare(self, queue: str, passive: bool = False) -> SimpleNamespace:
        counts = {"default": (7, 1), "default.XQ": (3, 0)}.get(queue, (0, 0))
        return SimpleNamespace(
            method=SimpleNamespace(message_count=counts[0], consumer_count=counts[1])
        )


class Broker:
    channel = Channel()

    def close(self) -> None:
        pass


def broker(monkeypatch: pytest.MonkeyPatch, reachable: bool) -> None:
    def make() -> Broker:
        if not reachable:
            raise ImproperlyConfigured("no broker in this test")
        return Broker()

    monkeypatch.setattr("pyracast.background.broker.make_broker", make)


def failed_calibration(system: System, plant: Plant) -> CalibrationRun:
    return CalibrationRun.objects.create(
        plant=plant,
        configuration=PlantConfiguration.objects.get(plant=plant),
        binding=system.binding,
        requested_by=system.member,
        window_start=NOW.date() - timedelta(days=7),
        window_end=NOW.date() - timedelta(days=1),
        cutoff_at=NOW - timedelta(days=1),
        status="failed",
        attempts=3,
        last_error="attempt_limit",
        completed_at=NOW - timedelta(hours=1),
    )


@pytest.mark.parametrize("language", ["en", "tr"])
def test_operations_lists_calibrations_behind_their_health_check(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    system, plant = make_plant(monkeypatch)
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    broker(monkeypatch, reachable=False)
    recent_forecast(system)
    dispatched()
    run = failed_calibration(system, plant)
    client = console(language)

    overview = client.get("/admin/")
    checks = {check.key: check for check in overview.context["health_summary"].checks}
    assert set(checks) == {"failed_calibrations"}
    assert checks["failed_calibrations"].action_url == (
        "/admin/operations/?issue=failed_calibrations#calibrations-title"
    )
    assert text(language, "Failed calibrations") in overview.content.decode()

    page = client.get(checks["failed_calibrations"].action_url)
    assert page.context["health_filter"].key == "failed_calibrations"
    assert [row["id"] for row in page.context["calibrations"]] == [run.pk]
    assert page.context["show_collections"] is False
    content = page.content.decode()
    assert text(language, "Calibration runs") in content
    assert 'id="calibrations-title"' in content
    assert plant.name in content


@pytest.mark.parametrize("language", ["en", "tr"])
def test_the_workers_panel_shows_phase_timing_queues_and_provider_requests(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    broker(monkeypatch, reachable=True)
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched(duration_ms=1250, published=4)
    for _ in range(3):
        RequestAttempt.objects.create(
            provider_account=system.account,
            binding=system.binding,
            utc_day=NOW.date(),
            created_at=NOW,
            purpose=RequestPurpose.FORECAST,
        )
    client = console(language)

    page = client.get("/admin/operations/")

    content = page.content.decode()
    for english in ("Workers", "Queues", "Dead letters", "Provider requests per plant"):
        assert text(language, english) in content, english
    assert "1250 ms" in content
    assert page.context["broker"].reachable is True
    assert [
        (queue.queue, queue.messages, queue.consumers, queue.dead_letters)
        for queue in page.context["broker"].queues
    ] == [
        ("default", 7, 1, 3),
        ("calibration", 0, 0, 0),
        ("notifications", 0, 0, 0),
        ("epias", 0, 0, 0),
    ]
    assert [
        (row["plant_name"], row["today"], row["busiest_day"])
        for row in page.context["provider_requests"]
    ] == [(system.region.name, 3, 3)]
    assert page.context["health_summary"].checks == ()

    broker(monkeypatch, reachable=False)
    page = client.get("/admin/operations/")
    content = page.content.decode()
    assert (
        text(language, "Message broker could not be read; queue counts are not shown.") in content
    )
    assert [check.key for check in page.context["health_summary"].checks] == ["broker"]
    assert text(language, "Message broker not answering") in content


@pytest.mark.parametrize("language", ["en", "tr"])
def test_the_plant_drill_down_says_why_each_plant_is_listed(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    CollectionJob.objects.filter(binding=system.binding).update(status="failed")
    dispatched()
    blocked_until = NOW + timedelta(hours=2)
    system.account.request_blocked_until = blocked_until
    system.account.save(update_fields=("request_blocked_until",))
    client = console(language)

    stale = client.get("/admin/plants/?issue=regions_without_recent_forecasts")
    assert [plant.id for plant in stale.context["plants"]] == [system.region.pk]
    content = stale.content.decode()
    assert text(language, "Why listed") in content
    assert text(language, "No forecast saved yet") in content

    delayed = client.get("/admin/plants/?issue=provider_cooldowns")
    assert [plant.id for plant in delayed.context["plants"]] == [system.region.pk]
    assert [plant.issue_time for plant in delayed.context["plants"]] == [blocked_until]
    assert f'datetime="{blocked_until.isoformat()}"' in delayed.content.decode()


@pytest.mark.parametrize("language", ["en", "tr"])
def test_a_check_without_matching_plants_shows_an_empty_filtered_list(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched()
    client = console(language)

    page = client.get("/admin/plants/?issue=provider_cooldowns")

    assert page.context["health_filter"].key == "provider_cooldowns"
    assert page.context["plants"] == []
    assert text(language, "No plants currently match this check.") in page.content.decode()


def test_each_console_page_accepts_only_its_own_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    broker(monkeypatch, reachable=False)
    system = make_system(approved=True)
    CollectionJob.objects.filter(binding=system.binding).update(status="failed", scheduled_for=NOW)
    system.account.request_blocked_until = NOW + timedelta(hours=1)
    system.account.save(update_fields=("request_blocked_until",))
    dispatched()
    client = console("en")

    operations = client.get("/admin/operations/?issue=provider_cooldowns")
    assert operations.context["health_filter"] is None
    assert operations.context["show_collections"] is True
    assert operations.context["show_dispatch"] is True
    plants = client.get("/admin/plants/?issue=failed_collections")
    assert plants.context["health_filter"] is None
    assert [plant.id for plant in plants.context["plants"]] == [system.region.pk]
    assert client.get("/admin/plants/?issue=not_a_check").context["health_filter"] is None


def test_a_filtered_operations_section_says_how_many_rows_it_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    broker(monkeypatch, reachable=False)
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched()
    for index in range(25):
        CollectionJob.objects.create(
            binding=system.binding,
            target_date=NOW.date(),
            scheduled_for=NOW - timedelta(minutes=index),
            available_at=NOW,
            status="failed",
            lease_token=None,
        )
    client = console("en")

    page = client.get("/admin/operations/?issue=failed_collections")

    assert len(page.context["collections"]) == 20
    assert "Showing 20 of 25" in page.content.decode()


@pytest.mark.parametrize("language", ["en", "tr"])
def test_a_healthy_panel_says_when_it_was_checked(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched()
    client = console(language)

    page = client.get("/admin/")

    assert page.context["health_summary"].checks == ()
    content = page.content.decode()
    assert text(language, "Checked at") in content
    assert f'datetime="{NOW.isoformat()}"' in content


def test_running_calibrations_are_not_a_health_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    system, plant = make_plant(monkeypatch)
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    recent_forecast(system)
    dispatched()
    CalibrationRun.objects.create(
        plant=plant,
        configuration=PlantConfiguration.objects.get(plant=plant),
        binding=system.binding,
        requested_by=User.objects.get(username="member"),
        window_start=NOW.date() - timedelta(days=7),
        window_end=NOW.date() - timedelta(days=1),
        cutoff_at=NOW - timedelta(days=1),
        status="running",
        attempts=1,
        lease_token=uuid.uuid4(),
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    client = console("en")

    assert client.get("/admin/").context["health_summary"].checks == ()
