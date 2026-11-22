import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.paginator import Paginator
from django.db.models import Model, QuerySet
from django.test import Client
from django.utils import timezone
from dramatiq.brokers.rabbitmq import RabbitmqBroker
from dramatiq.common import xq_name

from pyracast.accounts.models import UserLanguagePreference
from pyracast.forecasts.models import (
    CollectionJob,
    DispatcherPhase,
    DispatcherStatus,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.metering.models import EpiasCollectionJob
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.scheduler import PREPARATION_GRACE
from pyracast.orchestration.dispatcher import record_dispatch_phase
from pyracast.orchestration.kinds import QUEUES
from pyracast.plants.models import CalibrationRun, Plant, PlantConfiguration
from pyracast.platform_admin import read_models
from tests.support.api import verified_admin
from tests.support.budgets import assert_scale_invariant
from tests.support.delivery_health import NOW, recent_forecast
from tests.support.domain import System, make_system
from tests.support.metering import connected
from tests.support.plant_operations import make_plant
from tests.support.rabbitmq import install_fake_rabbitmq

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
    "Missing message queues": "Eksik mesaj kuyruklar\u0131",
    "Not declared": "Tan\u0131mlanmam\u0131\u015f",
    "Checked at": "Kontrol zaman\u0131",
    "Why listed": "Listelenme nedeni",
    "No forecast saved yet": "Henüz kaydedilmiş tahmin yok",
    "Last forecast": "Son tahmin",
    "No plants currently match this check.": "Bu kontrole şu anda uyan santral yok.",
    "Showing 1\u20132 of 2": "2 kay\u0131ttan 1\u20132 gösteriliyor",
    "Showing 1\u20135 of 5": "5 kay\u0131ttan 1\u20135 gösteriliyor",
    "Work of the last 24 hours and all open work.": "Son 24 saatin işleri ve tüm aç\u0131k işler.",
    "All work.": "Tüm işler.",
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
def test_a_queue_the_broker_has_not_declared_is_named_while_the_other_queues_show_their_counts(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    rabbitmq = install_fake_rabbitmq(monkeypatch)
    declared = [queue for queue in QUEUES if queue != "epias"]
    rabbitmq.declare(*declared, *(xq_name(queue) for queue in declared))
    rabbitmq.queues["default"].append(b"waiting")
    monkeypatch.setattr(
        "pyracast.background.broker.make_broker",
        lambda: RabbitmqBroker(url="amqp://broker.invalid/", confirm_delivery=True),
    )
    system = make_system(approved=True)
    recent_forecast(system)
    dispatched()

    page = console(language).get("/admin/operations/")

    content = page.content.decode()
    checks = page.context["health_summary"].checks
    assert [(check.key, check.count) for check in checks] == [("missing_queues", 1)]
    assert "epias" in checks[0].help
    assert text(language, "Missing message queues") in content
    assert text(language, "Not declared") in content
    assert [
        (queue.queue, queue.messages, queue.dead_letters, queue.missing)
        for queue in page.context["broker"].queues
    ] == [
        ("default", 1, 0, False),
        ("calibration", 0, 0, False),
        ("notifications", 0, 0, False),
        ("epias", None, None, True),
    ]
    assert rabbitmq.attempts == 1


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

    last_forecast = NOW - timedelta(hours=27)
    job = CollectionJob.objects.get(binding=system.binding)
    CollectionJob.objects.filter(pk=job.pk).update(status="succeeded")
    snapshot = ForecastSnapshot.objects.create(
        job=job,
        binding=system.binding,
        raw_payload={},
        normalized_intervals=[],
        source_kind="forecast",
        collected_at=last_forecast,
        available_at=last_forecast,
    )
    ForecastResult.objects.create(snapshot=snapshot, payload={}, published_at=last_forecast)
    old = client.get("/admin/plants/?issue=regions_without_recent_forecasts")
    assert [plant.issue_time for plant in old.context["plants"]] == [last_forecast]
    assert text(language, "Last forecast") in old.content.decode()

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
    assert "Showing 1\u201320 of 25" in page.content.decode()

    second = client.get("/admin/operations/?issue=failed_collections&collections_page=2")

    assert len(second.context["collections"]) == 5
    content = second.content.decode()
    assert "Showing 21\u201325 of 25" in content
    assert 'href="?issue=failed_collections&amp;collections_page=1"' in content
    assert "collections_page=3" not in content


def failed_jobs(system: System, count: int, created_at: datetime) -> None:
    first = CollectionJob.objects.count()
    jobs = [
        CollectionJob.objects.create(
            binding=system.binding,
            target_date=NOW.date(),
            scheduled_for=NOW - timedelta(minutes=10, seconds=first + index),
            available_at=NOW,
            status="failed",
            lease_token=None,
        )
        for index in range(count)
    ]
    CollectionJob.objects.filter(pk__in=[job.pk for job in jobs]).update(created_at=created_at)


@pytest.mark.parametrize("language", ["en", "tr"])
def test_operations_lists_show_the_last_24_hours_until_all_work_is_asked_for(
    monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    broker(monkeypatch, reachable=False)
    system = make_system(approved=True)
    dispatched()
    CollectionJob.objects.filter(binding=system.binding).delete()
    failed_jobs(system, 2, NOW - timedelta(hours=24))
    failed_jobs(system, 3, NOW - timedelta(hours=24, microseconds=1))
    client = console(language)

    recent = client.get("/admin/operations/")

    assert recent.context["collections_page"].paginator.count == 2
    content = recent.content.decode()
    assert text(language, "Showing 1\u20132 of 2") in content
    assert text(language, "Work of the last 24 hours and all open work.") in content
    assert 'href="/admin/operations/?window=all"' in content

    everything = client.get("/admin/operations/?window=all&collections_page=1")

    assert everything.context["collections_page"].paginator.count == 5
    content = everything.content.decode()
    assert text(language, "Showing 1\u20135 of 5") in content
    assert text(language, "All work.") in content
    assert 'href="/admin/operations/"' in content


def test_a_selected_check_lists_its_rows_whatever_their_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    broker(monkeypatch, reachable=False)
    system = make_system(approved=True)
    dispatched()
    CollectionJob.objects.filter(binding=system.binding).delete()
    job = CollectionJob.objects.create(
        binding=system.binding,
        target_date=NOW.date(),
        scheduled_for=NOW - timedelta(hours=30),
        available_at=NOW - timedelta(hours=30),
    )
    CollectionJob.objects.filter(pk=job.pk).update(created_at=NOW - timedelta(hours=30))
    client = console("en")

    page = client.get("/admin/operations/?issue=overdue_collections")

    assert [row["id"] for row in page.context["collections"]] == [job.pk]
    assert "Work of the last 24 hours and all open work." not in page.content.decode()


def old_collections(system: System, _plant: Plant, old: datetime) -> list[int]:
    jobs = [
        CollectionJob.objects.create(
            binding=system.binding,
            target_date=NOW.date(),
            scheduled_for=old + timedelta(minutes=index),
            available_at=old,
            status=status,
            lease_token=None,
        ).pk
        for index, status in enumerate(("retry", "failed"))
    ]
    CollectionJob.objects.filter(pk__in=jobs).update(created_at=old)
    return jobs


def old_deliveries(system: System, _plant: Plant, old: datetime) -> list[int]:
    schedule = ReminderSchedule.objects.create(
        region=system.region, effective_from=old - timedelta(days=1)
    )
    return [
        NotificationDelivery.objects.create(
            schedule=schedule,
            binding=system.binding,
            scheduled_for=old + timedelta(minutes=index),
            preparation_deadline=old + PREPARATION_GRACE,
            available_at=old,
            status=status,
            last_error=error,
        ).pk
        for index, (status, error) in enumerate(
            (("uncertain", ""), ("cancelled", "cancelled_by_settings_change"))
        )
    ]


def old_metering(_system: System, _plant: Plant, old: datetime) -> list[int]:
    # A job awaiting publication is never claimed again; a later night's job asks EPİAŞ anew.
    jobs = list(EpiasCollectionJob.objects.order_by("pk").values_list("pk", flat=True)[:2])
    for index, (status, error) in enumerate(
        (("pending", ""), ("awaiting_publication", "unpublished_data"))
    ):
        EpiasCollectionJob.objects.filter(pk=jobs[index]).update(
            status=status, last_error=error, scheduled_for=old + timedelta(minutes=index)
        )
    return jobs


def old_calibrations(system: System, plant: Plant, old: datetime) -> list[int]:
    waiting = CalibrationRun.objects.create(
        plant=plant,
        configuration=PlantConfiguration.objects.get(plant=plant),
        binding=system.binding,
        requested_by=system.member,
        window_start=NOW.date() - timedelta(days=7),
        window_end=NOW.date() - timedelta(days=1),
        cutoff_at=old,
    )
    runs = [waiting.pk, failed_calibration(system, plant).pk]
    CalibrationRun.objects.filter(pk__in=runs).update(created_at=old)
    return runs


@pytest.mark.parametrize(
    ("scope", "rows"),
    [
        ("collections", old_collections),
        ("deliveries", old_deliveries),
        ("metering", old_metering),
        ("calibrations", old_calibrations),
    ],
)
def test_open_work_older_than_a_day_stays_listed_until_it_ends(
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    rows: Callable[[System, Plant, datetime], list[int]],
) -> None:
    system, plant, _ = connected(monkeypatch)
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    broker(monkeypatch, reachable=False)
    dispatched()
    open_row, finished = rows(system, plant, NOW - timedelta(hours=48))

    page = console("en").get("/admin/operations/")

    listed = set(page.context[f"{scope}_page"].paginator.object_list.values_list("pk", flat=True))
    assert open_row in listed
    assert finished not in listed
    assert page.context["work_window"] == "recent"


def test_a_filtered_operations_page_reads_only_the_list_it_shows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paged: list[type[Model]] = []

    def recorded(object_list: QuerySet[Any], *args: Any) -> Any:
        paged.append(object_list.model)
        return Paginator(object_list, *args)

    monkeypatch.setattr(read_models, "Paginator", recorded)
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    broker(monkeypatch, reachable=False)
    make_system(approved=True)
    dispatched()
    client = console("en")

    client.get("/admin/operations/?issue=failed_collections")
    assert paged == [CollectionJob]

    paged.clear()
    client.get("/admin/operations/")
    assert set(paged) == {CollectionJob, NotificationDelivery, EpiasCollectionJob, CalibrationRun}


def test_the_dispatch_check_lists_no_work_and_so_offers_no_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    broker(monkeypatch, reachable=False)
    make_system(approved=True)
    dispatched()
    record_dispatch_phase("notifications", NOW, "OperationalError")

    page = console("en").get("/admin/operations/?issue=dispatch")

    content = page.content.decode()
    assert page.context["show_dispatch"] is True
    assert page.context["work_window"] == ""
    assert "Work of the last 24 hours and all open work." not in content
    assert "Show all work" not in content


def test_the_window_links_keep_the_rest_of_the_query_and_start_each_list_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    broker(monkeypatch, reachable=False)
    make_system(approved=True)
    dispatched()
    client = console("en")

    recent = client.get("/admin/operations/?issue=provider_cooldowns&collections_page=2")
    assert 'href="/admin/operations/?issue=provider_cooldowns&amp;window=all"' in (
        recent.content.decode()
    )

    everything = client.get(
        "/admin/operations/?issue=provider_cooldowns&window=all&calibrations_page=2"
    )
    assert 'href="/admin/operations/?issue=provider_cooldowns"' in everything.content.decode()


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@pytest.mark.parametrize("query", ["", "?window=all&collections_page=2&calibrations_page=x"])
def test_the_operations_page_reads_a_fixed_number_of_statements_whatever_it_lists(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    system, plant = make_plant(monkeypatch)
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    broker(monkeypatch, reachable=False)
    dispatched()
    client = console("en")

    def seed(count: int) -> None:
        failed_jobs(system, count, NOW - timedelta(minutes=1))
        for _ in range(count):
            failed_calibration(system, plant)

    assert_scale_invariant(
        seed, lambda: client.get(f"/admin/operations/{query}"), small=1, large=30
    )


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
