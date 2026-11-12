from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.db import close_old_connections, connections, transaction
from django.test import Client, override_settings

from pyracast.notifications.operations import recipient_limit
from tests.test_auth_lifecycle import PASSWORD, mailed_code, submit


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@override_settings(
    MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
)
def test_account_email_update_waits_for_notification_settings_lock() -> None:
    user = User.objects.create_user(username="member", email="old@example.com", password=PASSWORD)
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    client = Client(enforce_csrf_checks=True)
    assert (
        submit(client, "auth/login", {"email": user.email, "password": PASSWORD}).status_code == 200
    )
    assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    assert submit(client, "account/email", {"email": "new@example.com"}).status_code == 200
    key = mailed_code()
    token = client.get("/api/session/").json()["csrf_token"]
    started, finished = Event(), Event()

    def update_email() -> int:
        close_old_connections()
        try:
            started.set()
            return client.post(
                "/api/auth/browser/v1/auth/email/verify",
                {"key": key},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=token,
            ).status_code
        finally:
            finished.set()
            connections.close_all()

    with (
        override_settings(
            MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
        ),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        with transaction.atomic():
            recipient_limit(lock=True)
            future = pool.submit(update_email)
            assert started.wait(timeout=10)
            assert not finished.wait(timeout=1)
            user.refresh_from_db()
            assert user.email == "old@example.com"
        assert future.result(timeout=10) == 200
    user.refresh_from_db()
    assert user.email == "new@example.com"
