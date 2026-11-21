from datetime import timedelta

from allauth.account.models import EmailAddress
from django.contrib.auth.models import User

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.forecasts.validation import validate_binding
from pyracast.notifications.models import ReminderSchedule
from pyracast.notifications.operations import configure_reminders
from pyracast.regions.operations import approve_region, configure_binding, submit_region
from tests.support.domain import NOW, FakeAdapter, System


def member(system: System, email: str, *, verified: bool = True, active: bool = True) -> User:
    user = User.objects.create(username=email, email=email, is_active=active)
    Membership.objects.create(tenant=system.tenant, user=user)
    EmailAddress.objects.create(user=user, email=email, verified=verified, primary=True)
    return user


def group(system: System, addresses: list[str] | None = None) -> ReminderSchedule:
    return configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=addresses or [],
        include_workspace_members=True,
        times=["10:00"],
        language="en",
        now=NOW,
    )


def second_workspace(system: System, name: str = "Second") -> System:
    """An approved plant of another workspace on the same provider account."""
    administrator = User.objects.create(username=f"{name}-admin", email=f"{name}@example.com")
    tenant = Tenant.objects.create(name=name)
    Membership.objects.create(user=administrator, tenant=tenant, role=WorkspaceRole.ADMIN)
    region = submit_region(
        administrator, tenant.pk, name=name, latitude=41, longitude=30, timezone="Europe/Istanbul"
    )
    binding = configure_binding(
        system.admin,
        region.pk,
        provider_account_id=system.account.pk,
        site_id=name,
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    validate_binding(system.admin, binding.pk, FakeAdapter(), NOW - timedelta(hours=1))
    approve_region(system.admin, region.pk, binding.pk, NOW)
    region.refresh_from_db()
    binding.refresh_from_db()
    return System(administrator, system.admin, tenant, region, system.account, binding)
