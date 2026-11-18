from typing import cast

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.utils import timezone

from pyracast.accounts.models import Membership, WorkspaceRole
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


def test_a_throttled_read_answers_429_with_retry_after_in_the_error_contract() -> None:
    system = make_system()
    client = signed_in(system.member)
    base = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/"
    params = {"date": timezone.now().date().isoformat()}
    with one_forecast_a_minute():
        assert client.get(base + "forecast/", params).status_code == 200
        throttled = client.get(base + "forecast/", params)
        assert client.get(base + "plants/").status_code == 200

    assert throttled.status_code == 429
    body = throttled.json()
    assert body["code"] == "throttled"
    assert 0 < body["retry_after"] <= 60
    assert throttled["Retry-After"] == str(body["retry_after"])


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
