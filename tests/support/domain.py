from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from django.contrib.auth.models import User
from django.db import connection

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import (
    create_account,
    validate_binding,
)
from pyracast.regions.models import Region
from pyracast.regions.operations import (
    approve_region,
    configure_binding,
    submit_region,
)

NOW = datetime(2026, 11, 9, 13, tzinfo=UTC)


def provider_key(name: str = "TEST_KEY") -> str:
    return f"test-only-{name}"


class FakeAdapter:
    def __init__(self, action: Callable[[], None] = lambda: None) -> None:
        self.action = action

    def validate(self, binding: SiteBinding) -> None:
        assert not connection.in_atomic_block
        self.action()

    def fetch(self, binding: SiteBinding, target_date: object) -> ProviderData:
        assert not connection.in_atomic_block
        self.action()
        return ProviderData({"fixture": True}, [])


@dataclass
class System:
    member: User
    admin: User
    tenant: Tenant
    region: Region
    account: ProviderAccount
    binding: SiteBinding


def make_system(*, approved: bool = False) -> System:
    member = User.objects.create(username="member")
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    tenant = Tenant.objects.create(name="Tenant")
    Membership.objects.create(user=member, tenant=tenant, role=WorkspaceRole.ADMIN)
    region = submit_region(
        member, tenant.pk, name="Site", latitude=40, longitude=29, timezone="Europe/Istanbul"
    )
    account = create_account(admin, provider="fixture", external_account_id="customer", api_key="")
    binding = configure_binding(
        admin,
        region.pk,
        provider_account_id=account.pk,
        site_id="site",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    if approved:
        validate_binding(admin, binding.pk, FakeAdapter(), NOW - timedelta(hours=1))
        approve_region(admin, region.pk, binding.pk, NOW)
    return System(member, admin, tenant, region, account, binding)
