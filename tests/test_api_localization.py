from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone, translation

from pyracast.api.errors import exception_handler
from pyracast.notifications.models import NotificationSettings
from pyracast.plants.operations import create_plant
from pyracast.regions.operations import request_location_change
from tests.support.domain import make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_turkish_api_validation_preserves_field_names_and_error_code() -> None:
    system = make_system(approved=True)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/sites/"
    site = {"name": "PV", "latitude": 41, "longitude": 30, "dc_capacity_mw": 1}
    response = client.post(
        path,
        {**site, "timezone": "UTC", "extra": True},
        content_type="application/json",
        HTTP_ACCEPT_LANGUAGE="tr",
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": "http_400",
        "detail": {"non_field_errors": ["Bilinmeyen alanlar kabul edilmez."]},
    }
    response = client.post(
        path,
        {**site, "timezone": "invalid"},
        content_type="application/json",
        HTTP_ACCEPT_LANGUAGE="tr",
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": "invalid_request",
        "detail": ["Geçerli bir IANA saat dilimi girin."],
    }


def test_location_reason_is_presented_in_turkish_without_changing_domain_contract() -> None:
    system = make_system(approved=True)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/location-change-requests/"
    values = {"expected_revision": 2, "latitude": 41, "longitude": 30, "timezone": "UTC"}
    response = client.post(path, values, content_type="application/json", HTTP_ACCEPT_LANGUAGE="tr")
    assert response.status_code == 400
    assert response.json() == {
        "code": "invalid_request",
        "detail": ["Santral konumu değişti. Santrali yeniden aç\u0131p tekrar deneyin."],
    }
    with (
        translation.override("tr"),
        pytest.raises(ValidationError, match="region_revision_changed"),
    ):
        request_location_change(
            system.member,
            system.region.pk,
            system.tenant.pk,
            expected_revision=2,
            latitude=41,
            longitude=30,
            timezone="UTC",
        )


@pytest.mark.parametrize(
    ("language", "detail"),
    [
        ("en", "This plant allows at most 2 distinct recipient addresses in total."),
        ("tr", "Bu santral toplam en fazla 2 farkl\u0131 al\u0131c\u0131 adresine izin verir."),
    ],
)
def test_reminder_limit_translates_parameters_without_changing_english(
    language: str, detail: str
) -> None:
    system = make_system(approved=True)
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 2})
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"
    response = client.put(
        path,
        {
            "enabled": False,
            "recipients": ["one@example.com", "two@example.com", "three@example.com"],
            "times": [],
            "language": "en",
        },
        content_type="application/json",
        HTTP_ACCEPT_LANGUAGE=language,
    )
    assert response.status_code == 400
    assert response.json() == {"code": "invalid_request", "detail": [detail]}


def test_outage_capacity_validation_is_localized_at_the_http_boundary() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=1
    )
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/outages/"
    response = client.post(
        path,
        {
            "kind": "panels",
            "title": "Panels",
            "starts_at": (timezone.now() - timedelta(hours=1)).isoformat(),
            "available_dc_mw": 2,
        },
        content_type="application/json",
        HTTP_ACCEPT_LANGUAGE="tr",
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": "invalid_request",
        "detail": [
            "available_dc_mw sonlu, s\u0131f\u0131r veya pozitif olmal\u0131 ve bilinen "
            "yap\u0131land\u0131r\u0131lm\u0131ş "
            "kapasiteyi aşmamal\u0131d\u0131r."
        ],
    }


def test_django_validation_field_mapping_is_not_flattened() -> None:
    with translation.override("tr"):
        response = exception_handler(
            ValidationError({"timezone": ["An IANA timezone is required."]}), {}
        )
    assert response is not None
    assert response.status_code == 400
    assert response.data == {
        "code": "invalid_request",
        "detail": {"timezone": ["Geçerli bir IANA saat dilimi girin."]},
    }


def test_turkish_forecast_completed_day_validation_preserves_its_code() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member, system.tenant.pk, system.region.pk, name="PV", dc_capacity_mw=1
    )
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/plants/{plant.pk}/forecast/"
    response = client.get(
        path,
        {"date": "9999-12-31", "data_source": "estimated_actuals"},
        HTTP_ACCEPT_LANGUAGE="tr",
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": "invalid_request",
        "detail": [
            "Geçmiş tahminleri için son bir y\u0131l içinde tamamlanm\u0131ş bir yerel gün seçin."
        ],
    }
