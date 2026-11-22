import pytest
from django.test import Client
from django.utils import translation

from pyracast.notifications.models import NotificationSettings
from pyracast.plants.operations import create_plant
from pyracast.plants.parameters import PlantParameters
from pyracast.regions.operations import request_location_change
from tests.support.domain import make_system
from tests.support.errors import raises_code

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
        "code": "invalid_request",
        "message": "Bilinmeyen alanlar kabul edilmez.",
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
        "message": "Geçerli bir IANA saat dilimi girin.",
        "fields": {"timezone": "Geçerli bir IANA saat dilimi girin."},
    }


def test_location_reason_is_presented_in_turkish_without_changing_domain_contract() -> None:
    system = make_system(approved=True)
    client = Client()
    client.force_login(system.member)
    path = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/location-change-requests/"
    values = {"expected_revision": 2, "latitude": 41, "longitude": 30, "timezone": "UTC"}
    response = client.post(path, values, content_type="application/json", HTTP_ACCEPT_LANGUAGE="tr")
    assert response.status_code == 409
    assert response.json() == {
        "code": "region_revision_changed",
        "message": "Santral konumu değişti. Santrali yeniden aç\u0131p tekrar deneyin.",
    }
    with (
        translation.override("tr"),
        raises_code("region_revision_changed"),
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
            "include_workspace_members": False,
            "recipients": ["one@example.com", "two@example.com", "three@example.com"],
            "times": [],
            "language": "en",
            "expected_revision": 0,
        },
        content_type="application/json",
        HTTP_ACCEPT_LANGUAGE=language,
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": "recipient_limit_exceeded",
        "message": detail,
        "fields": {"recipients": detail},
    }


def test_turkish_forecast_completed_day_validation_preserves_its_code() -> None:
    system = make_system(approved=True)
    plant = create_plant(
        system.member,
        system.tenant.pk,
        system.region.pk,
        PlantParameters(name="PV", dc_capacity_mw=1),
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
        "message": (
            "Geçmiş tahminleri için son bir y\u0131l içinde tamamlanm\u0131ş bir yerel gün seçin."
        ),
    }
