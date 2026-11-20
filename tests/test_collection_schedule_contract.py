from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.test import Client

from pyracast.forecasts.models import CollectionJob
from pyracast.forecasts.schedule import configure_schedule
from tests.support.domain import NOW, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)
MOMENT = NOW + timedelta(hours=1)
JOB_FIELDS = {
    "id": int,
    "endpoint": str,
    "status": str,
    "scheduled_for": str,
    "available_at": str,
    "retry_until": (str, type(None)),
    "attempts": int,
    "last_error": str,
}


def read(system: System, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr("pyracast.forecasts.views.timezone.now", lambda: MOMENT)
    client = Client()
    client.force_login(system.member)
    response = client.get(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/collection-schedule/"
    )
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


def moment(value: object) -> datetime:
    assert isinstance(value, str)
    return datetime.fromisoformat(value)


def assert_shape(payload: dict[str, Any]) -> None:
    assert type(payload["enabled"]) is bool
    assert isinstance(payload["times"], list)
    assert all(isinstance(time, str) for time in payload["times"])
    assert isinstance(payload["timezone"], str)
    moment(payload["effective_from"])
    for key in ("last_success_at", "next_collection_at"):
        assert payload[key] is None or moment(payload[key])
    assert isinstance(payload["recent_jobs"], list)
    for job in payload["recent_jobs"]:
        assert set(JOB_FIELDS) <= set(job)
        for field, kind in JOB_FIELDS.items():
            assert isinstance(job[field], kind), field
    assert payload["history_requests_reserved_per_day"] == 1
    assert payload["history_local_time"] == "00:00"
    assert payload["history_retry_until_local"] == "09:00"
    assert payload["history_retry_minutes"] == 30
    assert type(payload["max_forecast_times"]) is int


def test_the_default_schedule_read_keeps_its_fields_and_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    approval = CollectionJob.objects.get(binding=system.binding)
    CollectionJob.objects.filter(pk=approval.pk).update(status="succeeded")

    payload = read(system, monkeypatch)

    assert_shape(payload)
    assert payload["enabled"] is True
    assert payload["times"] == ["16:00"]
    assert payload["timezone"] == "Europe/Istanbul"
    assert moment(payload["effective_from"]) == MOMENT
    assert payload["last_success_at"] is None
    assert moment(payload["next_collection_at"]) == datetime(2026, 11, 9, 21, tzinfo=UTC)
    assert [job["id"] for job in payload["recent_jobs"]] == [approval.pk]
    assert payload["recent_jobs"][0]["status"] == "succeeded"
    assert payload["max_forecast_times"] == system.account.daily_quota - 1


def test_a_persisted_schedule_read_keeps_its_fields_and_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = make_system(approved=True)
    CollectionJob.objects.filter(binding=system.binding).update(status="succeeded")
    configure_schedule(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        times=["15:00", "09:00"],
        now=NOW,
    )

    payload = read(system, monkeypatch)

    assert_shape(payload)
    assert payload["enabled"] is True
    assert payload["times"] == ["09:00", "15:00"]
    assert moment(payload["effective_from"]) == NOW
    assert moment(payload["next_collection_at"]) == datetime(2026, 11, 9, 21, tzinfo=UTC)
