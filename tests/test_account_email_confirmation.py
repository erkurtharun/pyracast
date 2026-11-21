from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import pytest
from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db import IntegrityError, close_old_connections, connections, transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.test import Client, override_settings

from pyracast.notifications.operations import configure_reminders, recipient_limit
from tests.support.auth_lifecycle import PASSWORD, mailed_code, submit
from tests.support.domain import NOW, make_system
from tests.support.notifications import schedule_addresses

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


def pending_email_change(user: User) -> tuple[Client, str]:
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    assert submit(client, "account/email", {"email": "new@example.com"}).status_code == 200
    return client, mailed_code()


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_account_email_confirmation_ignores_notification_lock_and_preserves_saved_recipients() -> (
    None
):
    system = make_system()
    user = system.member
    user.email = "old@example.com"
    user.set_password(PASSWORD)
    user.save(update_fields=["email", "password"])
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    schedule = configure_reminders(
        user,
        system.tenant.pk,
        system.region.pk,
        enabled=False,
        recipients=["old@example.com"],
        times=[],
        language="en",
        now=NOW,
    )
    client, key = pending_email_change(user)

    def update_email() -> int:
        close_old_connections()
        try:
            return int(submit(client, "auth/email/verify", {"key": key}).status_code)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool, transaction.atomic():
        recipient_limit(lock=True)
        future = pool.submit(update_email)
        assert future.result(timeout=10) == 200
        user.refresh_from_db()
        assert user.email == "new@example.com"
    user.refresh_from_db()
    assert user.email == "new@example.com"
    schedule.refresh_from_db()
    assert schedule_addresses(schedule) == ["old@example.com"]


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_confirmation_waits_for_its_user_before_entering_primary_email_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User.objects.create_user(username="member", email="old@example.com", password=PASSWORD)
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    client, key = pending_email_change(user)
    attempted, entered = Event(), Event()
    original_get = QuerySet.get
    original_confirm = DefaultAccountAdapter.confirm_email

    def observe_user_lock(queryset: QuerySet[Any], *args: Any, **kwargs: Any) -> Any:
        if queryset.model is User and queryset.query.select_for_update:
            attempted.set()
        return original_get(queryset, *args, **kwargs)

    def observe_primary_write(
        adapter: DefaultAccountAdapter,
        request: HttpRequest,
        address: EmailAddress,
    ) -> bool:
        entered.set()
        return original_confirm(adapter, request, address)

    monkeypatch.setattr(QuerySet, "get", observe_user_lock)
    monkeypatch.setattr(DefaultAccountAdapter, "confirm_email", observe_primary_write)

    def confirm() -> int:
        close_old_connections()
        try:
            return int(submit(client, "auth/email/verify", {"key": key}).status_code)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            User.objects.select_for_update().get(pk=user.pk)
            attempted.clear()
            future = pool.submit(confirm)
            assert attempted.wait(timeout=10)
            assert not entered.is_set()
            assert not future.done()
        assert future.result(timeout=10) == 200
    assert entered.is_set()
    user.refresh_from_db()
    assert user.email == "new@example.com"
    assert EmailAddress.objects.filter(user=user, primary=True).count() == 1
    assert EmailAddress.objects.get(user=user, primary=True).email == user.email


@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_confirmation_does_not_swallow_unexpected_database_integrity_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User.objects.create_user(username="member", email="old@example.com", password=PASSWORD)
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    client, key = pending_email_change(user)

    def conflicting_primary(
        adapter: DefaultAccountAdapter,
        request: HttpRequest,
        address: EmailAddress,
    ) -> bool:
        EmailAddress.objects.create(
            user_id=address.user_id, email="unexpected@example.com", primary=True
        )
        return True

    monkeypatch.setattr(DefaultAccountAdapter, "confirm_email", conflicting_primary)
    with pytest.raises(IntegrityError) as error:
        submit(client, "auth/email/verify", {"key": key})
    assert (
        getattr(getattr(error.value.__cause__, "diag", None), "constraint_name", None)
        == "unique_primary_email"
    )
    user.refresh_from_db()
    assert user.email == "old@example.com"
    assert not EmailAddress.objects.filter(email="unexpected@example.com").exists()
