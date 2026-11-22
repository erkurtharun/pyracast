from datetime import timedelta

from django.apps import apps
from django.db.models import Q

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import RequestAttempt
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import CalibrationRun, Plant, PlantConfiguration, ProductionObservation
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.models import Region, RegionStatus
from tests.support.domain import NOW, System
from tests.support.production_changes import edit, write

# Deleted by name in `pyracast.trash`.
PURGED = frozenset(
    {
        "change_requests.ChangeRequest",
        "change_requests.IncidentChange",
        "change_requests.ProductionChangeItem",
        "connections.SiteBinding",
        "forecasts.CollectionJob",
        "forecasts.CollectionSchedule",
        "forecasts.ForecastResult",
        "forecasts.ForecastSnapshot",
        "forecasts.HistoricalReferenceImport",
        "forecasts.ReferenceCapacityNormalization",
        "incidents.IncidentRevision",
        "incidents.PlantIncident",
        "metering.EpiasCollectionJob",
        "metering.EpiasConnection",
        "metering.EpiasConnectionRevision",
        "metering.EpiasSnapshot",
        "notifications.DeliveryRecipient",
        "notifications.NotificationDelivery",
        "notifications.ReminderMemberRole",
        "notifications.ReminderRecipient",
        "notifications.ReminderRecipientTie",
        "notifications.ReminderSchedule",
        "plants.CalibrationActivation",
        "plants.CalibrationRun",
        "plants.ForecastIssuance",
        "plants.Plant",
        "plants.PlantConfiguration",
        "plants.PlantEvent",
        "plants.PlantSubmission",
        "plants.ProductionObservation",
        "regions.AuditEvent",
        "regions.Region",
        "regions.RegionLocationChangeRequest",
    }
)
# Kept for quota accounting; the purge clears their references to purged rows.
DETACHED = frozenset({"forecasts.RequestAttempt"})


def sibling(system: System) -> tuple[Region, SiteBinding]:
    region = Region.objects.create(
        tenant=system.tenant,
        name="Other region",
        latitude=41 + Region.objects.filter(tenant=system.tenant).count(),
        longitude=29,
        timezone=system.region.timezone,
    )
    binding = SiteBinding.objects.get(pk=system.binding.pk)
    binding.pk = None
    binding.region = region
    binding.latitude = region.latitude
    binding.longitude = region.longitude
    binding.timezone = region.timezone
    binding.save()
    region.status = RegionStatus.APPROVED
    region.approved_binding = binding
    region.save()
    return region, binding


def sibling_plant(system: System) -> Plant:
    region, _ = sibling(system)
    plant = create_plant(
        system.member,
        system.tenant.pk,
        region.pk,
        PlantParameters(name="Second plant", dc_capacity_mw=2),
    )
    assert plant.current_configuration_id is not None
    PlantConfiguration.objects.filter(pk=plant.current_configuration_id).update(
        created_at=NOW - timedelta(days=100)
    )
    return Plant.objects.get(pk=plant.pk)


def revised_observation(system: System, plant: Plant, revisions: int) -> list[int]:
    """One observed interval and its revisions, oldest first; each supersedes the one before."""
    start = local_day_bounds(NOW.date() - timedelta(days=2), system.binding.timezone)[0]
    write(system, plant, [edit(start, 1)])
    chain = [ProductionObservation.objects.get(plant=plant, starts_at=start).pk]
    for energy in range(2, revisions + 2):
        write(system, plant, [edit(start, energy, revision_id=chain[-1])])
        chain.append(ProductionObservation.objects.get(supersedes_id=chain[-1]).pk)
    return chain


def remaining_purge_work() -> int:
    """Purged rows plus the references the purge clears; every purge statement lowers it."""
    rows: int = sum(apps.get_model(label)._default_manager.count() for label in PURGED)
    links = (
        RequestAttempt.objects.filter(Q(binding__isnull=False) | Q(job__isnull=False)).count()
        + Plant.objects.filter(current_configuration__isnull=False).count()
        + Region.objects.filter(approved_binding__isnull=False).count()
        + CalibrationRun.objects.filter(comparison_activation__isnull=False).count()
    )
    return rows + links
