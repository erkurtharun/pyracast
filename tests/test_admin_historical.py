import json
import time
from typing import Any, cast

import pytest
from django.conf import settings
from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from pyracast.admin_historical import PREVIEW_SALT
from pyracast.forecasts.models import HistoricalReferenceImport
from pyracast.plants.models import Plant
from tests.test_api import verified_admin
from tests.test_historical_reference_import import system_for_import

pytestmark = pytest.mark.django_db(transaction=True)


def export_file(*, power: float = 0.5) -> SimpleUploadedFile:
    return SimpleUploadedFile(
        "history.json",
        json.dumps(
            {
                "estimated_actuals": [
                    {
                        "period_end": "2026-11-07T23:00:00+02:00",
                        "period": "PT30M",
                        "pv_power_rooftop": power,
                    }
                ]
            }
        ).encode(),
        content_type="application/json",
    )


def preview(client: Client, url: str) -> dict[str, Any]:
    response = client.post(
        url, {"action": "preview", "file": export_file()}, headers={"Accept": "application/json"}
    )
    assert response.status_code == 200
    return cast(dict[str, Any], response.json())


def test_preview_then_save_is_explicit_and_repeat_upload_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    Plant.objects.create(region=system.region, name="Reference plant")
    client, _ = verified_admin()
    url = reverse("admin:workspace_historical_import", args=(system.region.pk,))
    page = client.get(url)
    assert page.status_code == 200
    assert b'type="file"' in page.content
    assert b"admin-historical.js" in page.content
    assert f'action="{url}"'.encode() in page.content
    checked = preview(client, url)
    assert checked["can_save"]
    assert "Not present in file" in checked["html"]
    assert not HistoricalReferenceImport.objects.exists()
    for _ in range(2):
        response = client.post(
            url,
            {
                "action": "save",
                "file": export_file(),
                "preview_token": checked["token"],
                "settings_confirmed": "on",
            },
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["saved"]
    assert HistoricalReferenceImport.objects.count() == 1


@pytest.mark.parametrize(
    "change", ["file", "token", "confirmation", "options", "configuration", "reference"]
)
def test_save_rejects_changed_or_unconfirmed_preview(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    system = system_for_import(monkeypatch)
    client, _ = verified_admin()
    url = reverse("admin:workspace_historical_import", args=(system.region.pk,))
    checked = preview(client, url)
    data: dict[str, Any] = {
        "action": "save",
        "file": export_file(power=0.6 if change == "file" else 0.5),
        "preview_token": "invalid" if change == "token" else checked["token"],
        "settings_confirmed": "" if change == "confirmation" else "on",
    }
    if change == "options":
        data["source_timezone"] = "UTC"
    if change == "configuration":
        system.region.timezone = "UTC"
        system.region.save(update_fields=("timezone",))
    if change == "reference":
        system.binding.capacity_kw = 2
        system.binding.save(update_fields=("capacity_kw",))
    response = client.post(url, data, headers={"Accept": "application/json"})
    assert response.status_code == 400
    assert not response.json()["can_save"]
    assert not HistoricalReferenceImport.objects.exists()


def test_preview_token_cannot_be_shared_between_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    system = system_for_import(monkeypatch)
    client, actor = verified_admin()
    url = reverse("admin:workspace_historical_import", args=(system.region.pk,))
    checked = preview(client, url)
    session = client.session
    session.cycle_key()
    session.save()
    assert session.session_key is not None
    client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
    response = client.post(
        url,
        {
            "action": "save",
            "file": export_file(),
            "preview_token": checked["token"],
            "settings_confirmed": "on",
        },
    )
    assert actor.is_superuser
    assert response.status_code in {302, 400}
    assert not HistoricalReferenceImport.objects.exists()


def test_upload_access_methods_csrf_and_missing_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    url = reverse("admin:workspace_historical_import", args=(system.region.pk,))
    client = Client()
    assert client.get(url).status_code == 302
    client.force_login(system.member)
    assert client.get(url).status_code == 302
    client.force_login(system.admin)
    assert client.get(url).status_code == 302
    client, _ = verified_admin(enforce_csrf_checks=True)
    assert client.post(url, {"action": "preview", "file": export_file()}).status_code == 403
    method_client = Client()
    method_client.cookies = client.cookies
    assert method_client.put(url).status_code == 405
    system.region.approved_binding = None
    system.region.save(update_fields=("approved_binding",))
    assert b"Approve the current Solcast configuration" in client.get(url).content


def test_invalid_file_is_reported_without_import(monkeypatch: pytest.MonkeyPatch) -> None:
    system = system_for_import(monkeypatch)
    client, _ = verified_admin()
    url = reverse("admin:workspace_historical_import", args=(system.region.pk,))
    response = client.post(url, {"file": SimpleUploadedFile("bad.json", b"not json")})
    assert response.status_code == 400
    assert b"Review the file" in response.content
    assert not HistoricalReferenceImport.objects.exists()


def test_expired_preview_requires_a_new_check(monkeypatch: pytest.MonkeyPatch) -> None:
    system = system_for_import(monkeypatch)
    client, _ = verified_admin()
    url = reverse("admin:workspace_historical_import", args=(system.region.pk,))
    checked = preview(client, url)
    identity = signing.loads(checked["token"], salt=PREVIEW_SALT)
    earlier = time.time() - 1801
    with monkeypatch.context() as signing_clock:
        signing_clock.setattr("django.core.signing.time.time", lambda: earlier)
        expired = signing.dumps(identity, salt=PREVIEW_SALT, compress=True)
    response = client.post(
        url,
        {
            "action": "save",
            "file": export_file(),
            "preview_token": expired,
            "settings_confirmed": "on",
        },
    )
    assert response.status_code == 400
    assert b"preview expired" in response.content
    assert not HistoricalReferenceImport.objects.exists()


def test_plain_html_preview_and_save_reselect_the_same_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = system_for_import(monkeypatch)
    client, _ = verified_admin()
    url = reverse("admin:workspace_historical_import", args=(system.region.pk,))
    checked = client.post(url, {"action": "preview", "file": export_file()})
    assert checked.status_code == 200
    assert b"Select the same file again before saving" in checked.content
    response = client.post(
        url,
        {
            "action": "save",
            "file": export_file(),
            "preview_token": checked.context["preview_token"],
            "settings_confirmed": "on",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"] == url
    assert HistoricalReferenceImport.objects.count() == 1


@pytest.mark.parametrize("invalid", ["action", "conflict", "binding", "oversized"])
def test_upload_validation_blocks_all_writes(monkeypatch: pytest.MonkeyPatch, invalid: str) -> None:
    system = system_for_import(monkeypatch)
    client, _ = verified_admin()
    url = reverse("admin:workspace_historical_import", args=(system.region.pk,))
    file = export_file()
    if invalid == "conflict":
        payload = json.loads(file.read())
        payload["metadata"] = {"capacity_kw": 9999}
        file = SimpleUploadedFile("history.json", json.dumps(payload).encode())
    if invalid == "binding":
        system.region.approved_binding = None
        system.region.save(update_fields=("approved_binding",))
    if invalid == "oversized":
        file = SimpleUploadedFile("large.json", b"x" * (7 * 1024 * 1024 + 1))
    response = client.post(
        url,
        {"action": "unknown" if invalid == "action" else "preview", "file": file},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == (200 if invalid == "conflict" else 400)
    assert not response.json()["can_save"]
    if invalid == "conflict":
        response = client.post(
            url,
            {
                "action": "save",
                "file": SimpleUploadedFile("history.json", json.dumps(payload).encode()),
                "preview_token": response.json()["token"],
                "settings_confirmed": "on",
            },
            headers={"Accept": "application/json"},
        )
        assert response.status_code == 400
        assert not response.json()["can_save"]
    assert not HistoricalReferenceImport.objects.exists()
