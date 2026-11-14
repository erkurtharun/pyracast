from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant
from pyracast.calculation import calculate
from pyracast.connections.operations import create_account, validate_binding
from pyracast.forecasts.jobs import collect
from pyracast.providers import FixtureProvider
from pyracast.regions.operations import approve_region, configure_binding, submit_region


class Command(BaseCommand):
    help = "Create and calculate a synthetic region in a local development database."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--username", required=True)

    def handle(self, *args: Any, **options: Any) -> str:
        if not settings.DEBUG:
            raise CommandError("Fixture demonstration requires PYRACAST_DEBUG=1.")
        actor = User.objects.filter(
            username=options["username"], is_superuser=True, is_active=True
        ).first()
        if actor is None:
            raise CommandError("Create an active superuser first.")
        tenant, _ = Tenant.objects.get_or_create(name="Fixture demonstration")
        Membership.objects.get_or_create(
            user=actor, tenant=tenant, defaults={"role": Membership.Role.ADMIN}
        )
        region = submit_region(
            actor,
            tenant.pk,
            name="Synthetic Ankara",
            latitude=39.9,
            longitude=32.8,
            timezone="Europe/Istanbul",
        )
        account = create_account(
            actor,
            provider="fixture",
            external_account_id="fixture-demo",
        )
        binding = configure_binding(
            actor,
            region.pk,
            provider_account_id=account.pk,
            site_id="synthetic",
            capacity_kw=1000,
            dc_capacity_kw=1000,
            capacity_semantics_verified=True,
        )
        adapter = FixtureProvider()
        validate_binding(actor, binding.pk, adapter, timezone.now())
        job = approve_region(actor, region.pk, binding.pk, timezone.now())
        collect(job.pk, adapter, calculate, timezone.now())
        return f"/api/tenants/{tenant.pk}/regions/{region.pk}/forecast/?date={job.target_date}"
