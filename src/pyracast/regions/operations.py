import math
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone as django_timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from pyracast.accounts.authorization import authorize_workspace_admin, require_platform_admin
from pyracast.accounts.models import Tenant
from pyracast.connections.models import PowerBasis, Provider, ProviderAccount, SiteBinding
from pyracast.errors import ConflictError, DomainError, ErrorCode
from pyracast.forecasts.models import CollectionJob
from pyracast.regions.audit import record_audit
from pyracast.regions.models import (
    REGION_TRANSITIONS,
    AuditAction,
    LocationChangeStatus,
    Region,
    RegionLocationChangeRequest,
    RegionStatus,
    location_coordinate,
)


def canonical_coordinate(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def require_available_location(
    tenant_id: int, latitude: float, longitude: float, *, exclude_region_id: int | None = None
) -> None:
    Tenant.objects.select_for_update().get(pk=tenant_id)
    regions = (
        Region.objects.active()
        .filter(tenant_id=tenant_id)
        .annotate(
            location_latitude=location_coordinate("latitude"),
            location_longitude=location_coordinate("longitude"),
        )
    )
    if exclude_region_id is not None:
        regions = regions.exclude(pk=exclude_region_id)
    if regions.filter(
        location_latitude=Decimal(str(canonical_coordinate(latitude))),
        location_longitude=Decimal(str(canonical_coordinate(longitude))),
    ).exists():
        raise DomainError(ErrorCode.LOCATION_ALREADY_EXISTS)


def validate_location(latitude: float, longitude: float, timezone: str) -> None:
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            _("Latitude must be between -90 and 90 degrees."),
            field="latitude",
        )
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            _("Longitude must be between -180 and 180 degrees."),
            field="longitude",
        )
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DomainError(
            ErrorCode.INVALID_REQUEST, _("An IANA timezone is required."), field="timezone"
        ) from exc


def submit_region(
    actor: User,
    tenant_id: int,
    *,
    name: str,
    latitude: float,
    longitude: float,
    timezone: str,
    platform: bool = False,
) -> Region:
    validate_location(latitude, longitude, timezone)
    if not name.strip() or len(name) > 200:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            _("A region name of at most 200 characters is required."),
            field="name",
        )
    with transaction.atomic():
        authorize_workspace_admin(actor, tenant_id, platform=platform)
        require_available_location(tenant_id, latitude, longitude)
        return Region.objects.create(
            tenant_id=tenant_id,
            name=name.strip(),
            latitude=canonical_coordinate(latitude),
            longitude=canonical_coordinate(longitude),
            timezone=timezone,
        )


def request_location_change(
    actor: User,
    region_id: int,
    tenant_id: int,
    *,
    expected_revision: int,
    latitude: float,
    longitude: float,
    timezone: str,
    reason: str = "",
    platform: bool = False,
) -> RegionLocationChangeRequest:
    validate_location(latitude, longitude, timezone)
    latitude, longitude = canonical_coordinate(latitude), canonical_coordinate(longitude)
    if len(reason) > 1000:
        raise ValidationError(gettext("A reason of at most 1000 characters is required."))
    with transaction.atomic():
        authorize_workspace_admin(actor, tenant_id, platform=platform)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        if region.revision != expected_revision:
            raise ConflictError(ErrorCode.REGION_REVISION_CHANGED)
        desired = (latitude, longitude, timezone, reason.strip())
        pending = RegionLocationChangeRequest.objects.filter(
            region=region, status=LocationChangeStatus.PENDING
        ).first()
        if pending is not None:
            if (
                pending.base_revision == expected_revision
                and (pending.latitude, pending.longitude, pending.timezone, pending.reason)
                == desired
            ):
                return pending
            raise ConflictError(ErrorCode.LOCATION_CHANGE_ALREADY_PENDING)
        if (region.latitude, region.longitude, region.timezone) == desired[:3]:
            raise DomainError(ErrorCode.LOCATION_UNCHANGED)
        change = RegionLocationChangeRequest.objects.create(
            region=region,
            requested_by=actor,
            base_revision=region.revision,
            latitude=latitude,
            longitude=longitude,
            timezone=timezone,
            reason=reason.strip(),
        )
        record_audit(
            actor=actor,
            region=region,
            action=AuditAction.LOCATION_CHANGE_REQUESTED,
            revision=region.revision,
            details={"request_id": change.pk},
        )
        return change


def correct_location(
    actor: User,
    region_id: int,
    *,
    expected_revision: int,
    latitude: float,
    longitude: float,
    timezone: str,
    reason: str = "",
) -> RegionLocationChangeRequest:
    require_platform_admin(actor)
    with transaction.atomic():
        tenant_id = Region.objects.active().values_list("tenant_id", flat=True).get(pk=region_id)
        Tenant.objects.select_for_update().get(pk=tenant_id)
        if RegionLocationChangeRequest.objects.filter(
            region_id=region_id, status=LocationChangeStatus.PENDING
        ).exists():
            raise ConflictError(ErrorCode.LOCATION_CHANGE_ALREADY_PENDING)
        change = request_location_change(
            actor,
            region_id,
            tenant_id,
            expected_revision=expected_revision,
            latitude=latitude,
            longitude=longitude,
            timezone=timezone,
            reason=reason,
            platform=True,
        )
        return review_location_change(actor, change.pk, approve=True)


def review_location_change(
    actor: User, request_id: int, *, approve: bool
) -> RegionLocationChangeRequest:
    require_platform_admin(actor)
    with transaction.atomic():
        change = RegionLocationChangeRequest.objects.get(pk=request_id)
        if approve:
            tenant_id = Region.objects.values_list("tenant_id", flat=True).get(pk=change.region_id)
            require_available_location(
                tenant_id, change.latitude, change.longitude, exclude_region_id=change.region_id
            )
        region = Region.objects.active().select_for_update().get(pk=change.region_id)
        change.refresh_from_db()
        status = LocationChangeStatus.APPROVED if approve else LocationChangeStatus.REJECTED
        if change.status == status:
            return change
        if change.status != LocationChangeStatus.PENDING:
            raise ConflictError(ErrorCode.LOCATION_CHANGE_ALREADY_REVIEWED)
        if approve:
            if change.base_revision != region.revision:
                raise ConflictError(ErrorCode.REGION_REVISION_CHANGED)
            region.latitude = change.latitude
            region.longitude = change.longitude
            region.timezone = change.timezone
            region.revision += 1
            region.status = RegionStatus.PENDING
            region.approved_binding = None
            region.save(
                update_fields=(
                    "latitude",
                    "longitude",
                    "timezone",
                    "revision",
                    "status",
                    "approved_binding",
                )
            )
        change.status = status
        change.reviewed_by = actor
        change.reviewed_at = django_timezone.now()
        change.save(update_fields=("status", "reviewed_by", "reviewed_at"))
        record_audit(
            actor=actor,
            region=region,
            action=(
                AuditAction.LOCATION_CHANGE_APPROVED
                if approve
                else AuditAction.LOCATION_CHANGE_REJECTED
            ),
            revision=region.revision,
            details={"request_id": change.pk},
        )
        return change


def _start_revision(actor: User, region: Region) -> None:
    region.revision += 1
    region.status = RegionStatus.PENDING
    region.approved_binding = None
    region.save(update_fields=("revision", "status", "approved_binding"))
    if RegionLocationChangeRequest.objects.filter(
        region=region, status=LocationChangeStatus.PENDING
    ).update(base_revision=region.revision):
        record_audit(
            actor=actor,
            region=region,
            action=AuditAction.LOCATION_CHANGE_REBASED,
            revision=region.revision,
        )


def configure_binding(
    actor: User,
    region_id: int,
    *,
    provider_account_id: int,
    site_id: str,
    capacity_kw: float,
    dc_capacity_kw: float | None = None,
    power_basis: str = PowerBasis.AC,
    capacity_semantics_verified: bool = False,
    azimuth_degrees: float = 0,
    losses_percent: float = 0,
    installation_date: date | None = None,
    settings_verification_reference: str = "",
) -> SiteBinding:
    require_platform_admin(actor)
    if not site_id.strip() or len(site_id) > 200:
        raise ValidationError("A provider site identity is required.")
    if not math.isfinite(capacity_kw) or capacity_kw <= 0:
        raise ValidationError("Positive finite capacity is required.")
    if dc_capacity_kw is not None and (
        isinstance(dc_capacity_kw, bool) or not math.isfinite(dc_capacity_kw) or dc_capacity_kw <= 0
    ):
        raise ValidationError("DC capacity must be finite and positive when specified.")
    if power_basis not in PowerBasis.values:
        raise ValidationError("An AC or DC power basis is required.")
    if not capacity_semantics_verified:
        raise ValidationError("A verified horizontal reference configuration is required.")
    if not math.isfinite(azimuth_degrees) or not 0 <= azimuth_degrees < 360:
        raise ValidationError("Azimuth must be between 0 and 360 degrees.")
    if not math.isfinite(losses_percent) or not 0 <= losses_percent < 100:
        raise ValidationError("Losses must be between 0 and 100 percent.")
    with transaction.atomic():
        region = Region.objects.active().select_for_update().get(pk=region_id)
        account = ProviderAccount.objects.get(pk=provider_account_id, enabled=True)
        reference = settings_verification_reference.strip()
        if len(reference) > 500 or (
            account.provider == Provider.SOLCAST and (not reference or installation_date is None)
        ):
            raise ValidationError("Confirmed site settings and installation date are required.")
        values = {
            "provider_account": account,
            "site_id": site_id.strip(),
            "capacity_kw": capacity_kw,
            "dc_capacity_kw": dc_capacity_kw,
            "power_basis": power_basis,
            "capacity_semantics_verified": capacity_semantics_verified,
            "azimuth_degrees": azimuth_degrees,
            "losses_percent": losses_percent,
            "installation_date": installation_date,
            "settings_verification_reference": reference or "fixture-development-only",
            "latitude": region.latitude,
            "longitude": region.longitude,
            "timezone": region.timezone,
        }
        current = SiteBinding.objects.filter(region=region, region_revision=region.revision).first()
        if current is not None:
            if SiteBinding.objects.filter(pk=current.pk, **values).exists():
                return current
            _start_revision(actor, region)
        binding = SiteBinding.objects.create(
            region=region, region_revision=region.revision, **values
        )
        record_audit(
            actor=actor,
            region=region,
            action=AuditAction.BINDING_CONFIGURED,
            revision=region.revision,
            details={"binding_id": binding.pk},
        )
    return binding


def approve_region(actor: User, region_id: int, binding_id: int, now: datetime) -> CollectionJob:
    require_platform_admin(actor)
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    with transaction.atomic():
        region = Region.objects.active().select_for_update().get(pk=region_id)
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
                account.provider == Provider.SOLCAST
                and (
                    not binding.settings_verification_reference or binding.installation_date is None
                )
            )
        ):
            raise ValidationError("The current configuration must be validated first.")
        if region.status == RegionStatus.APPROVED and region.approved_binding_id == binding.pk:
            return CollectionJob.objects.filter(binding=binding).earliest("created_at")
        region.status = RegionStatus.APPROVED
        region.approved_binding = binding
        region.save(update_fields=("status", "approved_binding"))
        from pyracast.plants.operations import activate_submitted_plants

        activate_submitted_plants(actor, region, binding)
        record_audit(
            actor=actor,
            region=region,
            action=AuditAction.REGION_APPROVED,
            revision=region.revision,
            details={"binding_id": binding.pk},
        )
        job, _ = CollectionJob.objects.get_or_create(
            binding=binding,
            endpoint="forecasts",
            scheduled_for=now,
            defaults={
                "target_date": now.astimezone(ZoneInfo(region.timezone)).date() + timedelta(days=1),
                "available_at": now,
            },
        )
    return job


def transition_region(actor: User, region_id: int, status: str) -> Region:
    require_platform_admin(actor)
    action = REGION_TRANSITIONS.get(status)
    if action is None:
        raise ValidationError("Use approval for approval transitions.")
    with transaction.atomic():
        region = Region.objects.active().select_for_update().get(pk=region_id)
        region.status = status
        region.save(update_fields=("status",))
        record_audit(actor=actor, region=region, action=action, revision=region.revision)
    return region
