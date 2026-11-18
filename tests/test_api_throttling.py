import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any, cast

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import caches
from django.test import Client, override_settings
from django.urls import resolve
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from pyracast.accounts.audit import logger as security_logger
from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.api import throttling
from tests.support.domain import make_system

pytestmark = pytest.mark.django_db(transaction=True)


def one_forecast_a_minute() -> override_settings:
    rates = cast(dict[str, str], settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}))
    rates = {**rates, "forecast": "1/min"}
    return override_settings(
        REST_FRAMEWORK={**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}
    )


def signed_in(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def test_production_rates_cover_every_endpoint_kind() -> None:
    rates = cast(dict[str, str], settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {}))
    assert set(rates) == {"forecast", "calibration", "production_import", "submission"}


def test_a_throttled_read_answers_429_with_retry_after_in_the_error_contract(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(security_logger, "handlers", [caplog.handler])
    monkeypatch.setattr(security_logger, "propagate", False)
    caplog.set_level(logging.INFO, logger="pyracast.security")
    system = make_system()
    client = signed_in(system.member)
    base = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    params = {"date": timezone.now().date().isoformat()}
    caplog.clear()
    with one_forecast_a_minute():
        assert client.get(base + "forecast/", params).status_code == 200
        throttled = client.get(base + "forecast/", params)
        assert client.get(base + "plants/").status_code == 200

    assert throttled.status_code == 429
    body = throttled.json()
    assert body["code"] == "throttled"
    assert 0 < body["retry_after"] <= 60
    assert throttled["Retry-After"] == str(body["retry_after"])
    assert [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "pyracast.security"
    ] == [
        {
            "event": "api_throttled",
            "user_id": system.member.pk,
            "client_ip": "127.0.0.1",
            "scope": "forecast",
        }
    ]


def test_each_user_has_a_budget_of_their_own() -> None:
    system = make_system()
    viewer = User.objects.create_user(username="viewer")
    Membership.objects.create(user=viewer, tenant=system.tenant, role=WorkspaceRole.VIEWER)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    params = {"date": timezone.now().date().isoformat()}
    with one_forecast_a_minute():
        assert signed_in(system.member).get(path, params).status_code == 200
        assert signed_in(viewer).get(path, params).status_code == 200
        assert signed_in(system.member).get(path, params).status_code == 429


def test_refused_requests_spend_no_budget() -> None:
    system = make_system()
    user = User.objects.create_user(username="outsider")
    client = signed_in(user)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/forecast/"
    params = {"date": timezone.now().date().isoformat()}
    with one_forecast_a_minute():
        assert client.get(path, params).status_code == 403
        assert client.get(path, params).status_code == 403
        Membership.objects.create(user=user, tenant=system.tenant, role=WorkspaceRole.VIEWER)
        assert client.get(path, params).status_code == 200
        assert client.get(path, params).status_code == 429


def test_concurrent_requests_share_one_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = caches["throttle"]
    write = cache.set
    first_write = Event()
    release_write = Event()
    second_started = Event()
    second_finished = Event()
    path = "/api/tenants/1/regions/1/forecast/"

    def delayed_write(key: str, value: Any, *args: Any, **kwargs: Any) -> None:
        if not first_write.is_set():
            first_write.set()
            assert release_write.wait(10)
        write(key, value, *args, **kwargs)

    def admit() -> bool:
        raw = APIRequestFactory().get(path)
        raw.resolver_match = resolve(path)
        request = Request(raw)
        request.user = User(pk=123)
        return throttling.RouteThrottle().allow_request(request, APIView())

    def admit_second() -> bool:
        second_started.set()
        try:
            return admit()
        finally:
            second_finished.set()

    monkeypatch.setattr(cache, "set", delayed_write)
    monkeypatch.setattr(throttling, "caches", {"throttle": cache})
    with one_forecast_a_minute(), ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(admit)
        try:
            assert first_write.wait(10)
            second = executor.submit(admit_second)
            assert second_started.wait(10)
            second_finished.wait(1)
        finally:
            release_write.set()
        assert sorted((first.result(timeout=10), second.result(timeout=10))) == [False, True]
