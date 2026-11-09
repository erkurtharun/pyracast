import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction

from pyracast.accounts.operations import require_admin, require_member
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import CollectionJob
from pyracast.regions.models import AuditEvent, Region


def validate_location(latitude: float, longitude: float, timezone: str) -> None:
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValidationError("Latitude must be between -90 and 90 degrees.")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValidationError("Longitude must be between -180 and 180 degrees.")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError("An IANA timezone is required.") from exc


def submit_region(
    actor: User,
    tenant_id: int,
    *,
    name: str,
    latitude: float,
    longitude: float,
    timezone: str,
) -> Region:
    require_member(actor, tenant_id)
    validate_location(latitude, longitude, timezone)
    if not name.strip() or len(name) > 200:
        raise ValidationError("A region name of at most 200 characters is required.")
    return Region.objects.create(
        tenant_id=tenant_id,
        name=name.strip(),
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
    )


def update_region(
    actor: User,
    region_id: int,
    tenant_id: int,
    *,
    name: str,
    latitude: float,
    longitude: float,
    timezone: str,
) -> Region:
    require_member(actor, tenant_id)
    validate_location(latitude, longitude, timezone)
    if not name.strip() or len(name) > 200:
        raise ValidationError("A region name of at most 200 characters is required.")
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id, tenant_id=tenant_id)
        region.name = name.strip()
        region.latitude = latitude
        region.longitude = longitude
        region.timezone = timezone
        region.revision += 1
        region.status = "pending"
        region.approved_binding = None
        region.save()
        AuditEvent.objects.create(
            actor=actor, region=region, action="region_revised", revision=region.revision
        )
    return region


def configure_binding(
    actor: User,
    region_id: int,
    *,
    provider_account_id: int,
    site_id: str,
    capacity_kw: float,
    power_basis: str = "ac",
    capacity_basis: str = "ac",
    capacity_semantics_verified: bool = False,
    tilt_degrees: float = 0,
    azimuth_degrees: float = 180,
    losses_percent: float = 0,
    installation_date: date | None = None,
    settings_verification_reference: str = "",
) -> SiteBinding:
    require_admin(actor)
    if not site_id.strip() or len(site_id) > 200:
        raise ValidationError("A provider site identity is required.")
    if not math.isfinite(capacity_kw) or capacity_kw <= 0:
        raise ValidationError("Positive finite capacity is required.")
    if power_basis not in {"ac", "dc"} or capacity_basis != power_basis:
        raise ValidationError("Matching AC/DC power and capacity bases are required.")
    if tilt_degrees != 0 or not capacity_semantics_verified:
        raise ValidationError("A verified horizontal reference configuration is required.")
    if not math.isfinite(azimuth_degrees) or not 0 <= azimuth_degrees < 360:
        raise ValidationError("Azimuth must be between 0 and 360 degrees.")
    if not math.isfinite(losses_percent) or not 0 <= losses_percent < 100:
        raise ValidationError("Losses must be between 0 and 100 percent.")
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id)
        account = ProviderAccount.objects.get(pk=provider_account_id, enabled=True)
        reference = settings_verification_reference.strip()
        if len(reference) > 500 or (
            account.provider == "solcast" and (not reference or installation_date is None)
        ):
            raise ValidationError("Confirmed site settings and installation date are required.")
        if SiteBinding.objects.filter(region=region, region_revision=region.revision).exists():
            region.revision += 1
            region.status = "pending"
            region.approved_binding = None
            region.save(update_fields=("revision", "status", "approved_binding"))
        binding = SiteBinding.objects.create(
            region=region,
            region_revision=region.revision,
            provider_account=account,
            site_id=site_id.strip(),
            capacity_kw=capacity_kw,
            power_basis=power_basis,
            capacity_basis=capacity_basis,
            capacity_semantics_verified=capacity_semantics_verified,
            tilt_degrees=tilt_degrees,
            azimuth_degrees=azimuth_degrees,
            losses_percent=losses_percent,
            installation_date=installation_date,
            settings_verification_reference=reference or "fixture-development-only",
            latitude=region.latitude,
            longitude=region.longitude,
            timezone=region.timezone,
        )
        AuditEvent.objects.create(
            actor=actor, region=region, action="binding_configured", revision=region.revision
        )
    return binding


def approve_region(actor: User, region_id: int, binding_id: int, now: datetime) -> CollectionJob:
    require_admin(actor)
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id)
        binding = SiteBinding.objects.select_related("provider_account").get(
            pk=binding_id, region=region, region_revision=region.revision
        )
        account = ProviderAccount.objects.select_for_update().get(pk=binding.provider_account_id)
        if (
            binding.validated_at is None
            or binding.validated_credential_revision != account.credential_revision
            or not account.enabled
            or account.authorized_at is None
            or (
                account.provider == "solcast"
                and (
                    not binding.settings_verification_reference or binding.installation_date is None
                )
            )
        ):
            raise ValidationError("The current configuration must be validated first.")
        if region.status == "approved" and region.approved_binding_id == binding.pk:
            return CollectionJob.objects.filter(binding=binding).earliest("created_at")
        region.status = "approved"
        region.approved_binding = binding
        region.save(update_fields=("status", "approved_binding"))
        AuditEvent.objects.create(
            actor=actor, region=region, action="approved", revision=region.revision
        )
        job, _ = CollectionJob.objects.get_or_create(
            binding=binding,
            endpoint="forecasts",
            scheduled_slot=now,
            defaults={
                "target_date": now.astimezone(ZoneInfo(region.timezone)).date() + timedelta(days=1),
                "available_at": now,
            },
        )
    return job


def transition_region(actor: User, region_id: int, status: str) -> Region:
    require_admin(actor)
    if status not in {"rejected", "suspended"}:
        raise ValidationError("Use approval for approval transitions.")
    with transaction.atomic():
        region = Region.objects.select_for_update().get(pk=region_id)
        region.status = status
        region.save(update_fields=("status",))
        AuditEvent.objects.create(
            actor=actor, region=region, action=status, revision=region.revision
        )
    return region
