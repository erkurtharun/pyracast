from allauth.account.models import EmailAddress
from django.contrib.auth.models import User

from pyracast.accounts.models import Membership
from pyracast.notifications.models import ReminderSchedule
from pyracast.notifications.operations import configure_reminders
from tests.support.domain import NOW, System


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
