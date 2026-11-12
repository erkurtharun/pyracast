from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.contrib.auth.models import User
from django.db import close_old_connections, connection, transaction
from django.test import Client

from pyracast.notifications.operations import recipient_limit


@pytest.mark.django_db(transaction=True)
def test_account_email_update_waits_for_notification_settings_lock() -> None:
    user = User.objects.create_user(username="member", email="old@example.com")
    client = Client()
    client.force_login(user)
    started, finished = Event(), Event()

    def update_email() -> int:
        close_old_connections()
        try:
            started.set()
            return client.patch(
                "/api/session/", {"email": "new@example.com"}, content_type="application/json"
            ).status_code
        finally:
            finished.set()
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
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
