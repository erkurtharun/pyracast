import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Queue
from types import SimpleNamespace
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.totp.internal import auth as totp_auth
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret
from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, connections
from django.test import Client

from pyracast.accounts.audit import logger as security_logger
from tests.support.auth_lifecycle import PASSWORD, submit
from tests.support.concurrency import _waits_for


def browser(language: str | None = None) -> Client:
    headers = {} if language is None else {"Accept-Language": language}
    return Client(enforce_csrf_checks=True, headers=headers)


def answer(response: Any) -> tuple[int, Any]:
    return response.status_code, response.json()


def account(name: str, *, verified: bool = True) -> User:
    user = User.objects.create_user(username=name, email=f"{name}@example.com", password=PASSWORD)
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=verified)
    return user


def passwordless(name: str) -> User:
    user = account(name)
    user.set_unusable_password()
    user.save(update_fields=["password"])
    return user


def signed_in(user: User, *, reauthenticated: bool = False) -> Client:
    client = browser()
    if user.has_usable_password():
        credentials = {"email": user.email, "password": PASSWORD}
        assert submit(client, "auth/login", credentials).status_code == 200
    else:
        client.force_login(user)
    if reauthenticated:
        assert submit(client, "auth/reauthenticate", {"password": PASSWORD}).status_code == 200
    return client


def session_user(client: Client) -> int | None:
    user = client.get("/api/session/").json()["user"]
    return None if user is None else int(user["id"])


def session_key(client: Client) -> str:
    return client.cookies[settings.SESSION_COOKIE_NAME].value


def session_cookie(response: Any) -> tuple[str, Any, str] | None:
    cookie = response.cookies.get(settings.SESSION_COOKIE_NAME)
    return None if cookie is None else (cookie.value, cookie["max-age"], cookie["expires"])


def carrying_key(key: str, *, enforce_csrf_checks: bool = True) -> Client:
    client = Client(enforce_csrf_checks=enforce_csrf_checks)
    client.cookies[settings.SESSION_COOKIE_NAME] = key
    return client


def arrive(jar: Client, *answers: Any) -> None:
    # A browser applies the session cookie of each answer in the order the answers arrive.
    for answer in answers:
        cookie = answer.cookies.get(settings.SESSION_COOKIE_NAME)
        if cookie is None:
            continue
        if cookie["max-age"] == 0:
            jar.cookies.pop(settings.SESSION_COOKIE_NAME, None)
        else:
            jar.cookies[settings.SESSION_COOKIE_NAME] = cookie.value


def totp_clock(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    monkeypatch.setattr(totp_auth, "time", SimpleNamespace(time=lambda: now))


def with_totp(user: User) -> str:
    secret = generate_totp_secret()
    TOTP.activate(user, secret)
    return secret


def elsewhere(work: Callable[[], object]) -> None:
    def run() -> None:
        try:
            work()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(1) as pool:
        pool.submit(run).result(timeout=10)


def alongside(work: Callable[[], object]) -> Future[None]:
    # Returns once the work has finished or waits for a lock held by this thread's connection.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        holder = cursor.fetchone()[0]
    started: Queue[int] = Queue()

    def run() -> None:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                started.put(cursor.fetchone()[0])
            work()
        finally:
            connections.close_all()

    pool = ThreadPoolExecutor(1)
    running = pool.submit(run)
    pool.shutdown(wait=False)
    waiter = started.get(timeout=10)
    deadline = time.monotonic() + 10
    while not running.done() and not _waits_for(holder, waiter):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return running


def capture_security_events(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], list[dict[str, Any]]]:
    monkeypatch.setattr(security_logger, "handlers", [caplog.handler])
    monkeypatch.setattr(security_logger, "propagate", False)
    caplog.set_level(logging.INFO, logger="pyracast.security")
    return lambda: [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "pyracast.security"
    ]


def moment(seconds: float) -> SimpleNamespace:
    return SimpleNamespace(time=lambda: seconds, monotonic=time.monotonic, sleep=time.sleep)
