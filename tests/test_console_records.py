from datetime import timedelta

import pytest
from django.db.models import Model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import CollectionJob, ForecastResult, RequestAttempt
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from pyracast.plants.models import (
    CalibrationActivation,
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    ProductionObservation,
)
from pyracast.platform_admin.records import LIVE_ROWS
from pyracast.regions.models import AuditEvent, Region, RegionLocationChangeRequest
from tests.support.api import verified_admin
from tests.support.domain import NOW
from tests.support.notifications import SLOT, saved
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)


def records_of_one_plant(monkeypatch: pytest.MonkeyPatch) -> dict[type[Model], Model]:
    system, plant = make_plant(monkeypatch)
    configuration = PlantConfiguration.objects.get(plant=plant)
    run = CalibrationRun.objects.create(
        plant=plant,
        configuration=configuration,
        binding=system.binding,
        requested_by=system.member,
        window_start=NOW.date() - timedelta(days=7),
        window_end=NOW.date() - timedelta(days=1),
        cutoff_at=NOW,
    )
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=["default@example.com"],
        times=["10:00"],
        now=NOW,
    )
    snapshot = saved(system)
    (delivery,) = enqueue_due(SLOT)
    return {
        Region: system.region,
        RegionLocationChangeRequest: RegionLocationChangeRequest.objects.create(
            region=system.region,
            requested_by=system.member,
            base_revision=system.region.revision,
            latitude=41,
            longitude=29,
            timezone="Europe/Istanbul",
        ),
        AuditEvent: AuditEvent.objects.filter(region=system.region).earliest("pk"),
        SiteBinding: system.binding,
        CollectionJob: snapshot.job,
        RequestAttempt: RequestAttempt.objects.filter(binding=system.binding).earliest("pk"),
        ForecastResult: ForecastResult.objects.get(snapshot=snapshot),
        NotificationDelivery: NotificationDelivery.objects.get(pk=delivery),
        Plant: plant,
        PlantConfiguration: configuration,
        ProductionObservation: ProductionObservation.objects.create(
            plant=plant,
            configuration=configuration,
            starts_at=NOW - timedelta(hours=1),
            ends_at=NOW,
            energy_mwh=1,
            source="meter",
            recorded_by=system.member,
        ),
        CalibrationRun: run,
        CalibrationActivation: CalibrationActivation.objects.create(
            plant=plant, configuration=configuration, calibration=run
        ),
        PlantEvent: PlantEvent.objects.filter(plant=plant).earliest("pk"),
    }


def listed(client: Client, record: Model) -> bool:
    options = record._meta
    page = client.get(reverse(f"admin:{options.app_label}_{options.model_name}_changelist"))
    return bool(page.context["cl"].queryset.filter(pk=record.pk).exists())


def test_the_record_browser_leaves_out_every_record_of_a_trashed_plant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = records_of_one_plant(monkeypatch)
    region = records[Region]
    account_attempt = RequestAttempt.objects.create(
        provider_account=ProviderAccount.objects.get(),
        utc_day=NOW.date(),
        created_at=NOW,
        purpose="validation",
    )
    client, _ = verified_admin()
    assert records.keys() == LIVE_ROWS.keys()
    assert [
        model._meta.label for model, record in records.items() if not listed(client, record)
    ] == []

    Region.objects.filter(pk=region.pk).update(deleted_at=timezone.now())

    assert [model._meta.label for model, record in records.items() if listed(client, record)] == []
    assert listed(client, account_attempt)
    for view in ("change", "history"):
        page = client.get(reverse(f"admin:regions_region_{view}", args=(region.pk,)))
        assert (page.status_code, page["Location"]) == (302, "/admin/")


def test_the_record_browser_keeps_provider_account_events(monkeypatch: pytest.MonkeyPatch) -> None:
    system, _ = make_plant(monkeypatch)
    client, _ = verified_admin()
    Region.objects.filter(pk=system.region.pk).update(deleted_at=timezone.now())

    events = client.get("/admin/regions/auditevent/").context["cl"].result_list

    assert [event.provider_account_id for event in events] != []
