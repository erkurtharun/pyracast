import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from zoneinfo import available_timezones

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone as django_timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from pyracast.accounts.authorization import authorize_workspace_admin, require_platform_admin
from pyracast.accounts.models import Tenant
from pyracast.connections.models import (
    SITE_ID_PATTERN,
    PowerBasis,
    Provider,
    ProviderAccount,
    SiteBinding,
)
from pyracast.errors import ConflictError, DomainError, ErrorCode
from pyracast.regions.audit import record_audit
from pyracast.regions.models import (
    NAME_MAX_LENGTH,
    AuditAction,
    AuditEvent,
    LocationChangeStatus,
    Region,
    RegionLocationChangeRequest,
    RegionStatus,
    active_region,
    location_coordinate,
)
from pyracast.regions.review import SETUP_CHANGED, require_seen_status, set_review_status

SITE_ID_REFUSED = _(
    "The resource ID contains characters the provider does not use. "
    "Use only letters, digits, hyphens and underscores."
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
    occupant = (
        regions.filter(
            location_latitude=Decimal(str(canonical_coordinate(latitude))),
            location_longitude=Decimal(str(canonical_coordinate(longitude))),
        )
        .values_list("pk", flat=True)
        .first()
    )
    if occupant is not None:
        raise _location_taken(occupant)


def _location_taken(region_id: int) -> DomainError:
    """Names the plant of the same workspace that holds the location, so it can be opened."""
    return DomainError(ErrorCode.LOCATION_ALREADY_EXISTS, details={"region_id": region_id})


@cache
def available_zones() -> frozenset[str]:
    """ZoneInfo also opens keys outside this set, such as `right/UTC` with leap seconds or a
    Windows path spelling, which other hosts cannot open."""
    return frozenset(available_timezones())


def require_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > NAME_MAX_LENGTH:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            _("Enter a plant name of at most 200 characters."),
            field="name",
        )
    if "\x00" in name:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            _("The plant name contains an invisible character. Type the name again."),
            field="name",
        )
    return name


def validate_location(latitude: float, longitude: float, timezone: str) -> None:
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            _("Enter a latitude between -90 and 90 degrees."),
            field="latitude",
        )
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            _("Enter a longitude between -180 and 180 degrees."),
            field="longitude",
        )
    if timezone not in available_zones():
        raise DomainError(ErrorCode.INVALID_REQUEST, _("Choose a time zone."), field="timezone")


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
    name = require_name(name)
    with transaction.atomic():
        authorize_workspace_admin(actor, tenant_id, platform=platform)
        require_available_location(tenant_id, latitude, longitude)
        region = Region.objects.create(
            tenant_id=tenant_id,
            name=name,
            latitude=canonical_coordinate(latitude),
            longitude=canonical_coordinate(longitude),
            timezone=timezone,
        )
        record_audit(
            actor=actor, region=region, action=AuditAction.REGION_CREATED, revision=region.revision
        )
        return region


def _require_free_target(tenant_id: int, latitude: float, longitude: float, region: Region) -> None:
    require_available_location(tenant_id, latitude, longitude, exclude_region_id=region.pk)
    requester = (
        RegionLocationChangeRequest.objects.filter(
            active_region("region__"),
            region__tenant_id=tenant_id,
            status=LocationChangeStatus.PENDING,
        )
        .exclude(region=region)
        .annotate(
            target_latitude=location_coordinate("latitude"),
            target_longitude=location_coordinate("longitude"),
        )
        .filter(
            target_latitude=Decimal(str(latitude)),
            target_longitude=Decimal(str(longitude)),
        )
        .values_list("region_id", flat=True)
        .first()
    )
    if requester is not None:
        raise _location_taken(requester)


def _awaits_first_setup(region: Region) -> bool:
    """A plant that was never approved and has no connection for its current location loses
    nothing when it moves, so its administrator moves it without an operator review."""
    return (
        region.status == RegionStatus.PENDING
        and not SiteBinding.objects.filter(region=region, region_revision=region.revision).exists()
        and not AuditEvent.objects.filter(
            region=region, action=AuditAction.REGION_APPROVED
        ).exists()
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
        raise ValidationError(gettext("Enter a reason of at most 1000 characters."))
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
        _require_free_target(tenant_id, latitude, longitude, region)
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
        if _awaits_first_setup(region):
            _move(actor, change, region, "")
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
        return approve_location_change(actor, change.pk, expected_revision=expected_revision).change


@dataclass(frozen=True)
class LocationDecision:
    change: RegionLocationChangeRequest
    changed: bool


def _decision_note(reason: str) -> str:
    if len(reason.strip()) > 500:
        raise DomainError(ErrorCode.INVALID_REQUEST, field="decision_reason")
    return reason.strip()


def _locked_request(request_id: int) -> tuple[RegionLocationChangeRequest, Region]:
    tenant_id, region_id = RegionLocationChangeRequest.objects.values_list(
        "region__tenant_id", "region_id"
    ).get(pk=request_id)
    Tenant.objects.select_for_update().get(pk=tenant_id)
    region = Region.objects.active().select_for_update().get(pk=region_id)
    return RegionLocationChangeRequest.objects.select_for_update().get(pk=request_id), region


def _settled(change: RegionLocationChangeRequest, status: str) -> bool:
    if change.status == status:
        return True
    if change.status != LocationChangeStatus.PENDING:
        raise ConflictError(ErrorCode.LOCATION_CHANGE_ALREADY_REVIEWED)
    return False


def _require_current(
    change: RegionLocationChangeRequest, region: Region, expected: int, status: str | None
) -> None:
    require_seen_status(region, status)
    if expected != region.revision or change.base_revision != region.revision:
        raise ConflictError(ErrorCode.REGION_REVISION_CHANGED, SETUP_CHANGED)


def _close(
    actor: User | None,
    change: RegionLocationChangeRequest,
    status: LocationChangeStatus,
    reason: str,
) -> None:
    change.status = status
    change.reviewed_by = actor
    change.reviewed_at = django_timezone.now()
    change.decision_reason = reason
    change.save(update_fields=("status", "reviewed_by", "reviewed_at", "decision_reason"))


def _move(actor: User, change: RegionLocationChangeRequest, region: Region, reason: str) -> None:
    region.latitude = change.latitude
    region.longitude = change.longitude
    region.timezone = change.timezone
    region.revision += 1
    _save_reopened(region, "latitude", "longitude", "timezone", "revision")
    _close(actor, change, LocationChangeStatus.APPROVED, reason)
    record_audit(
        actor=actor,
        region=region,
        action=AuditAction.LOCATION_CHANGE_APPROVED,
        revision=region.revision,
        details={"request_id": change.pk},
    )


def approve_location_change(
    actor: User,
    request_id: int,
    *,
    expected_revision: int,
    expected_status: str | None = None,
    reason: str = "",
) -> LocationDecision:
    require_platform_admin(actor)
    reason = _decision_note(reason)
    with transaction.atomic():
        change, region = _locked_request(request_id)
        if _settled(change, LocationChangeStatus.APPROVED):
            return LocationDecision(change, changed=False)
        _require_current(change, region, expected_revision, expected_status)
        require_available_location(
            region.tenant_id, change.latitude, change.longitude, exclude_region_id=region.pk
        )
        _move(actor, change, region, reason)
        return LocationDecision(change, changed=True)


def reject_location_change(
    actor: User,
    request_id: int,
    *,
    expected_revision: int,
    expected_status: str | None = None,
    reason: str = "",
) -> LocationDecision:
    require_platform_admin(actor)
    reason = _decision_note(reason)
    with transaction.atomic():
        change, region = _locked_request(request_id)
        if _settled(change, LocationChangeStatus.REJECTED):
            return LocationDecision(change, changed=False)
        _require_current(change, region, expected_revision, expected_status)
        _close(actor, change, LocationChangeStatus.REJECTED, reason)
        record_audit(
            actor=actor,
            region=region,
            action=AuditAction.LOCATION_CHANGE_REJECTED,
            revision=region.revision,
            details={"request_id": change.pk},
        )
        return LocationDecision(change, changed=True)


def withdraw_location_change(
    actor: User, tenant_id: int, region_id: int, request_id: int
) -> RegionLocationChangeRequest:
    with transaction.atomic():
        authorize_workspace_admin(actor, tenant_id)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        change = RegionLocationChangeRequest.objects.select_for_update().get(
            pk=request_id, region=region
        )
        if _settled(change, LocationChangeStatus.WITHDRAWN):
            return change
        _close(None, change, LocationChangeStatus.WITHDRAWN, "")
        record_audit(
            actor=actor,
            region=region,
            action=AuditAction.LOCATION_CHANGE_WITHDRAWN,
            revision=region.revision,
            details={"request_id": change.pk},
        )
        return change


def _save_reopened(region: Region, *fields: str) -> None:
    # One UPDATE: a second UPDATE of the row in the same transaction re-checks its tenant key
    # (FOR KEY SHARE) and deadlocks with a writer that locked the tenant before the region.
    if region.status == RegionStatus.PENDING:
        region.approved_binding = None
        region.save(update_fields=("approved_binding", *fields))
    else:
        set_review_status(region, RegionStatus.PENDING, fields=fields)


def _start_revision(actor: User, region: Region) -> None:
    region.revision += 1
    _save_reopened(region, "revision")
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
        raise ValidationError(gettext("A provider site identity is required."))
    if not re.fullmatch(SITE_ID_PATTERN, site_id.strip()):
        raise ValidationError(SITE_ID_REFUSED)
    if not math.isfinite(capacity_kw) or capacity_kw <= 0:
        raise ValidationError(gettext("Positive finite capacity is required."))
    if dc_capacity_kw is not None and (
        isinstance(dc_capacity_kw, bool) or not math.isfinite(dc_capacity_kw) or dc_capacity_kw <= 0
    ):
        raise ValidationError(gettext("DC capacity must be finite and positive when specified."))
    if power_basis not in PowerBasis.values:
        raise ValidationError(gettext("An AC or DC power basis is required."))
    if not capacity_semantics_verified:
        raise ValidationError(gettext("A verified horizontal reference configuration is required."))
    if not math.isfinite(azimuth_degrees) or not 0 <= azimuth_degrees < 360:
        raise ValidationError(gettext("Azimuth must be between 0 and 360 degrees."))
    if not math.isfinite(losses_percent) or not 0 <= losses_percent < 100:
        raise ValidationError(gettext("Losses must be between 0 and 100 percent."))
    with transaction.atomic():
        region = Region.objects.active().select_for_update().get(pk=region_id)
        account = ProviderAccount.objects.get(pk=provider_account_id, enabled=True)
        reference = settings_verification_reference.strip()
        if len(reference) > 500 or (
            account.provider == Provider.SOLCAST and (not reference or installation_date is None)
        ):
            raise ValidationError(
                gettext("Confirmed site settings and installation date are required.")
            )
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
