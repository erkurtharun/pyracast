import contextlib
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime, timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from allauth.usersessions.models import UserSession
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.db.models.deletion import Collector
from django.db.models.signals import pre_delete
from django.test import override_settings
from django.utils import timezone
from dramatiq.brokers.stub import StubBroker

from pyracast import trash
from pyracast.accounts import headless, retention
from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    Membership,
    Tenant,
    WorkspaceAdminChange,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.background import failures
from pyracast.forecasts.management.commands import dispatch
from pyracast.forecasts.models import DispatcherStatus
from pyracast.plants.models import Plant
from pyracast.regions.models import Region
from pyracast.trash import RETENTION, trash_region
from tests.support.account_sessions import alongside, browser, elsewhere, session_user
from tests.support.auth_lifecycle import submit
from tests.support.company_registration import mailed_code, signup
from tests.support.domain import NOW
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
LOCAL_MAIL = {"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}


def session(key: str, expires: datetime, user: User | None = None) -> None:
    Session.objects.create(session_key=key, session_data="", expire_date=expires)
    if user is not None:
        device(key, user)


def device(key: str, user: User) -> None:
    UserSession.objects.create(user=user, session_key=key, ip="127.0.0.1", user_agent="test")


def test_the_retention_phase_removes_expired_sessions_and_their_device_rows() -> None:
    now = timezone.now()
    user = User.objects.create_user(username="owner")
    session("live", now + timedelta(hours=1), user)
    session("expired-signed-in", now - timedelta(seconds=1), user)
    session("expired-anonymous", now - timedelta(seconds=1))
    device("orphan", user)

    dispatch._retention()

    assert list(Session.objects.values_list("session_key", flat=True)) == ["live"]
    assert list(UserSession.objects.values_list("session_key", flat=True)) == ["live"]


def test_session_cleanup_removes_a_bounded_batch_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    now = timezone.now()
    user = User.objects.create_user(username="owner")
    for index in range(3):
        session(f"expired-{index}", now - timedelta(minutes=index + 1))
        device(f"orphan-{index}", user)
    monkeypatch.setattr(retention, "BATCH_SIZE", 2)

    first = retention.end_expired_sessions(now)
    remaining = (Session.objects.count(), UserSession.objects.count())
    second = retention.end_expired_sessions(now)

    assert (first, remaining, second) == (4, (1, 1), 2)
    assert not Session.objects.exists()
    assert not UserSession.objects.exists()


def signup_account(name: str, *, days: int = 8) -> User:
    user = User.objects.create_user(username=name, email=f"{name}@example.com")
    User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(days=days))
    EmailAddress.objects.create(user=user, email=user.email, primary=True)
    tenant = Tenant.objects.create(name=f"{name} workspace")
    Membership.objects.create(tenant=tenant, user=user, role=WorkspaceRole.ADMIN)
    return user


def workspace(user: User) -> Tenant:
    return Tenant.objects.get(membership__user=user)


def verified(user: User) -> None:
    EmailAddress.objects.filter(user=user).update(verified=True)


def signed_in_once(user: User) -> None:
    User.objects.filter(pk=user.pk).update(last_login=timezone.now())


def staff(user: User) -> None:
    User.objects.filter(pk=user.pk).update(is_staff=True)


def shared(user: User) -> None:
    other = User.objects.create_user(username=f"{user.username}-viewer")
    Membership.objects.create(tenant=workspace(user), user=other)


def invited(user: User) -> None:
    owner = User.objects.create_user(username=f"{user.username}-inviter")
    WorkspaceInvitation.objects.create(
        tenant=workspace(user),
        email="guest@example.com",
        token_hash=user.username.ljust(64, "0"),
        created_by=owner,
        expires_at=timezone.now() + timedelta(days=7),
    )


def referenced(user: User) -> None:
    WorkspaceAdminChange.objects.create(tenant=workspace(user), actor=user, administrator=user)


def provider_linked(user: User) -> None:
    SocialAccount.objects.create(user=user, provider="microsoft", uid=f"{user.username}-uid")


def without_workspace(user: User) -> None:
    workspace(user).delete()


def unchanged(user: User) -> None:
    return None


SELECTION: dict[str, tuple[int, Callable[[User], None], bool]] = {
    "abandoned": (8, unchanged, True),
    "abandoned-with-provider-identity": (8, provider_linked, True),
    "abandoned-without-workspace": (8, without_workspace, True),
    "joined-within-seven-days": (6, unchanged, False),
    "verified": (8, verified, False),
    "signed-in-once": (8, signed_in_once, False),
    "staff": (8, staff, False),
    "workspace-with-another-member": (8, shared, False),
    "workspace-with-an-invitation": (8, invited, False),
    "referenced-by-a-kept-record": (8, referenced, False),
}


@pytest.mark.parametrize("case", SELECTION)
def test_the_retention_phase_reclaims_only_abandoned_signups(case: str) -> None:
    days, state, reclaimed = SELECTION[case]
    user = signup_account("owner", days=days)
    state(user)
    tenants = list(Tenant.objects.filter(membership__user=user).values_list("pk", flat=True))

    dispatch._retention()

    assert User.objects.filter(pk=user.pk).exists() is not reclaimed
    assert Tenant.objects.filter(pk__in=tenants).exists() is (bool(tenants) and not reclaimed)


AFTER_SELECTION: dict[str, Callable[[User], None]] = {
    "verified": verified,
    "workspace-with-another-member": shared,
    "workspace-with-an-invitation": invited,
}


@pytest.mark.parametrize("case", AFTER_SELECTION)
def test_a_signup_that_changes_after_selection_is_kept(case: str) -> None:
    user = signup_account("owner")
    cutoff = timezone.now() - retention.ABANDONED_AFTER
    AFTER_SELECTION[case](user)

    assert not retention.reclaim_signup(user.pk, cutoff)

    assert User.objects.filter(pk=user.pk).exists()
    assert Tenant.objects.filter(membership__user=user).exists()


@pytest.mark.parametrize("case", ["workspace-with-another-member", "workspace-with-an-invitation"])
def test_a_signup_that_changes_before_its_workspaces_are_locked_is_kept(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = signup_account("owner")
    changes: list[str] = []
    lock = retention._lock

    def changed_before_the_workspaces_are_locked(rows: Any) -> list[Any]:
        if rows.model is Tenant and not changes:
            changes.append(case)
            elsewhere(lambda: AFTER_SELECTION[case](user))
        return lock(rows)

    monkeypatch.setattr(retention, "_lock", changed_before_the_workspaces_are_locked)

    assert not retention.reclaim_signup(user.pk, timezone.now() - retention.ABANDONED_AFTER)

    assert changes == [case]
    assert User.objects.filter(pk=user.pk).exists()
    assert Tenant.objects.filter(membership__user=user).exists()


def test_an_invitation_to_a_workspace_being_reclaimed_waits_and_finds_it_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = signup_account("owner")
    tenant = workspace(user)
    administrator = User.objects.create_user(
        username="administrator", is_staff=True, is_superuser=True
    )
    invitations: list[Future[None]] = []
    collect = Collector.collect

    def invite() -> None:
        invite_workspace_member(administrator, tenant.pk, "guest@example.com", platform=True)

    def invited_meanwhile(self: Collector, *args: Any, **kwargs: Any) -> Any:
        if not invitations:
            invitations.append(alongside(invite))
        return collect(self, *args, **kwargs)

    monkeypatch.setattr(Collector, "collect", invited_meanwhile)

    with override_settings(MAILERS=LOCAL_MAIL):
        reclaimed = retention.reclaim_signup(user.pk, timezone.now() - retention.ABANDONED_AFTER)
        with pytest.raises(Tenant.DoesNotExist):
            invitations[0].result(timeout=10)

    assert reclaimed
    assert not WorkspaceInvitation.objects.exists()


def test_a_signup_that_signed_in_after_selection_keeps_its_invitation_while_it_is_swept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = signup_account("owner")
    tenant = workspace(user)
    verified(user)
    signed_in_once(user)
    invitations: list[Future[None]] = []
    lock = retention._lock

    def invite() -> None:
        invite_workspace_member(User.objects.get(pk=user.pk), tenant.pk, "guest@example.com")

    def invited_once_the_account_is_locked(rows: Any) -> list[Any]:
        locked = lock(rows)
        if rows.model is User and not invitations:
            invitations.append(alongside(invite))
        return locked

    monkeypatch.setattr(retention, "_lock", invited_once_the_account_is_locked)

    with override_settings(MAILERS=LOCAL_MAIL):
        reclaimed = retention.reclaim_signup(user.pk, timezone.now() - retention.ABANDONED_AFTER)
        invitations[0].result(timeout=10)

    assert not reclaimed
    assert User.objects.filter(pk=user.pk).exists()
    assert list(WorkspaceInvitation.objects.values_list("tenant_id", "created_by_id")) == [
        (tenant.pk, user.pk)
    ]


def test_a_verification_completing_while_its_signup_is_reclaimed_keeps_the_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = browser()
    with override_settings(MAILERS=LOCAL_MAIL):
        assert signup(client).status_code == 401
        code = mailed_code()
    user = User.objects.get()
    User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(days=8))
    sweeps: list[Future[None]] = []
    claim = headless.claim

    def reclaimed_meanwhile(address: EmailAddress) -> None:
        claim(address)
        sweeps.append(alongside(lambda: retention.reclaim_abandoned_signups(timezone.now())))

    monkeypatch.setattr(headless, "claim", reclaimed_meanwhile)

    response = submit(client, "auth/email/verify", {"key": code})
    sweeps[0].result(timeout=20)

    assert response.status_code == 200
    assert session_user(client) == user.pk
    assert Tenant.objects.filter(membership__user=user).exists()


def test_a_kept_signup_does_not_hold_back_later_abandoned_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept = signup_account("kept")
    referenced(kept)
    abandoned = signup_account("abandoned")
    monkeypatch.setattr(retention, "BATCH_SIZE", 1)

    reclaimed = retention.reclaim_abandoned_signups(timezone.now())

    assert (reclaimed, abandoned.pk > kept.pk) == (1, True)
    assert list(User.objects.values_list("pk", flat=True)) == [kept.pk]


def test_scanning_past_kept_signups_retains_at_most_one_page_of_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(3):
        referenced(signup_account(f"kept-{index}"))
    monkeypatch.setattr(retention, "BATCH_SIZE", 1)
    retained: list[int] = []

    def recording(*args: Any, **kwargs: Any) -> list[Any]:
        results = failures.isolate(*args, **kwargs)
        retained.append(len(results))
        return results

    monkeypatch.setattr(retention, "isolate", recording)

    assert retention.reclaim_abandoned_signups(timezone.now()) == 0
    assert (sum(retained), max(retained)) == (3, 1)


def test_signup_reclaim_removes_a_bounded_batch_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(3):
        signup_account(f"owner-{index}")
    monkeypatch.setattr(retention, "BATCH_SIZE", 2)

    counts = [retention.reclaim_abandoned_signups(timezone.now()) for _ in range(3)]

    assert counts == [2, 1, 0]
    assert not User.objects.exists()
    assert not Tenant.objects.exists()


def dispatch_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch, "make_broker", StubBroker)
    with contextlib.suppress(CommandError):
        call_command("dispatch")


def retention_error() -> str:
    return DispatcherStatus.objects.get(phase="retention").last_error


def expired_region(monkeypatch: pytest.MonkeyPatch, *, failing: bool) -> Region:
    system, _ = make_plant(monkeypatch)
    trash_region(system.member, system.tenant.pk, system.region.pk)
    Region.objects.update(deleted_at=NOW - RETENTION - timedelta(days=1))
    if failing:

        def purge(region: Region, plants: list[Plant], budget: trash._Budget) -> None:
            raise RuntimeError("private purge detail")

        monkeypatch.setattr(trash, "_purge_region", purge)
    return system.region


def failing_session_step(now: datetime) -> int:
    raise RuntimeError("private session detail")


def test_a_failing_region_purge_still_ends_sessions_and_reclaims_signups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    region = expired_region(monkeypatch, failing=True)
    session("expired", NOW - timedelta(seconds=1), User.objects.create_user(username="signed-in"))
    abandoned = signup_account("abandoned")
    abandoned_workspace = workspace(abandoned)

    dispatch_once(monkeypatch)

    assert Region.objects.filter(pk=region.pk).exists()
    assert not Session.objects.exists()
    assert not UserSession.objects.exists()
    assert not User.objects.filter(pk=abandoned.pk).exists()
    assert not Tenant.objects.filter(pk=abandoned_workspace.pk).exists()
    assert retention_error() == "items_failed:1"


def test_a_failing_account_sweep_keeps_the_committed_region_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    region = expired_region(monkeypatch, failing=False)
    monkeypatch.setattr(retention, "end_expired_sessions", failing_session_step)

    dispatch_once(monkeypatch)

    assert not Region.objects.filter(pk=region.pk).exists()
    assert retention_error() == "RuntimeError"


def test_when_purge_items_and_the_account_sweep_fail_the_sweep_failure_is_recorded(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    region = expired_region(monkeypatch, failing=True)
    monkeypatch.setattr(retention, "end_expired_sessions", failing_session_step)

    dispatch_once(monkeypatch)

    assert retention_error() == "RuntimeError"
    assert f"retention_purge_failed item=region:{region.pk}" in pyracast_logs.text
    assert "dispatch_phase_failed phase=retention error_type=RuntimeError" in pyracast_logs.text
    assert "private" not in pyracast_logs.text


def test_a_failing_signup_reclaim_does_not_stop_the_others_and_is_retried(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    failing = signup_account("failing-owner")
    failing_workspace = workspace(failing)
    later_workspace = workspace(signup_account("later-owner"))
    session("expired", timezone.now() - timedelta(seconds=1), failing)

    def fail_after_the_workspace_delete(sender: type[User], instance: User, **_: Any) -> None:
        if instance.pk == failing.pk:
            raise RuntimeError(f"private {failing.email} {failing.username}")

    pre_delete.connect(fail_after_the_workspace_delete, sender=User)
    try:
        dispatch_once(monkeypatch)
    finally:
        pre_delete.disconnect(fail_after_the_workspace_delete, sender=User)

    assert list(User.objects.values_list("pk", flat=True)) == [failing.pk]
    assert list(Membership.objects.values_list("tenant_id", "user_id")) == [
        (failing_workspace.pk, failing.pk)
    ]
    assert not Tenant.objects.filter(pk=later_workspace.pk).exists()
    assert not Session.objects.exists()
    assert not UserSession.objects.exists()
    assert retention_error() == "items_failed:1"
    assert f"signup_reclaim_failed item=account:{failing.pk}" in pyracast_logs.text
    assert "private" not in pyracast_logs.text
    assert "failing-owner" not in pyracast_logs.text

    dispatch_once(monkeypatch)

    assert not User.objects.exists()
    assert not Tenant.objects.exists()
    assert retention_error() == ""
