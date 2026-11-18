from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from pyracast.accounts.authorization import is_platform_admin
from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.calculation import calculate
from pyracast.connections.models import SiteBinding
from pyracast.connections.operations import QuotaExceededError, create_account, validate_binding
from pyracast.forecasts.jobs import collect
from pyracast.plants.models import Plant
from pyracast.plants.operations import create_plant
from pyracast.providers import FixtureProvider
from pyracast.regions.models import Region
from pyracast.regions.operations import approve_region, configure_binding, submit_region

REGION = "Synthetic Ankara"


def _region(actor: User, tenant: Tenant) -> Region:
    existing = Region.objects.filter(tenant=tenant, name=REGION, deleted_at__isnull=True).first()
    return existing or submit_region(
        actor,
        tenant.pk,
        name=REGION,
        latitude=39.9,
        longitude=32.8,
        timezone="Europe/Istanbul",
    )


def _plant(actor: User, region: Region) -> Plant:
    existing = Plant.objects.filter(region=region).first()
    return existing or create_plant(
        actor, region.tenant_id, region.pk, name="Synthetic plant", dc_capacity_mw=1
    )


def _binding(actor: User, region: Region) -> SiteBinding:
    binding = (
        SiteBinding.objects.filter(region=region, region_revision=region.revision)
        .order_by("-pk")
        .first()
    )
    if binding is None:
        account = create_account(actor, provider="fixture", external_account_id="fixture-demo")
        binding = configure_binding(
            actor,
            region.pk,
            provider_account_id=account.pk,
            site_id="synthetic",
            capacity_kw=1000,
            dc_capacity_kw=1000,
            capacity_semantics_verified=True,
        )
    if binding.validated_at is None:
        binding = validate_binding(actor, binding.pk, FixtureProvider(), timezone.now())
    return binding


@transaction.atomic
def _workspace(actor: User) -> Tenant:
    name = f"Fixture demonstration (user {actor.pk})"
    tenant = Tenant.objects.filter(
        name=name, membership__user=actor, membership__role=WorkspaceRole.ADMIN
    ).first()
    if tenant is None:
        tenant = Tenant.objects.create(name=name)
        Membership.objects.create(user=actor, tenant=tenant, role=WorkspaceRole.ADMIN)
    return tenant


def run_demo(actor: User) -> str:
    tenant = _workspace(actor)
    region = _region(actor, tenant)
    plant = _plant(actor, region)
    binding = _binding(actor, region)
    # A plant issuance needs a configuration older than the snapshot, so each step reads the clock.
    job = approve_region(actor, region.pk, binding.pk, timezone.now())
    if job.status != "succeeded" and not collect(
        job.pk, FixtureProvider(), calculate, timezone.now()
    ):
        job.refresh_from_db()
        if job.status != "succeeded":
            raise CommandError(f"The demo collection ended as {job.status}; run the command again.")
    return (
        f"/api/tenants/{tenant.pk}/regions/{region.pk}/plants/{plant.pk}/forecast/"
        f"?date={job.target_date}"
    )


class Command(BaseCommand):
    help = "Create or resume a synthetic plant forecast in a local development database."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)

    def handle(self, *args: Any, **options: Any) -> str:
        if not settings.DEBUG:
            raise CommandError("Fixture demonstration requires PYRACAST_DEBUG=1.")
        actor = User.objects.filter(username=options["username"]).first()
        if actor is None or not is_platform_admin(actor):
            raise CommandError("Create an active staff superuser first.")
        try:
            return run_demo(actor)
        except (ValidationError, PermissionDenied, QuotaExceededError) as error:
            raise CommandError(f"Fixture demonstration stopped: {error}") from error
