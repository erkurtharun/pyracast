import json
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from threading import Event
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.db.models import Model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from pyracast.accounts import session_lock
from pyracast.accounts.invitations import invite_workspace_member
from pyracast.accounts.models import (
    Membership,
    Tenant,
    UserLanguagePreference,
    WorkspaceInvitation,
    WorkspaceRole,
)
from pyracast.connections.models import ProviderAccount
from pyracast.connections.operations import validate_binding
from pyracast.forecasts.models import CollectionJob, HistoricalReferenceImport, RequestAttempt
from pyracast.metering.models import EpiasConnection
from pyracast.notifications.models import NotificationSettings
from pyracast.plants.models import Plant
from pyracast.providers import SolcastProvider
from pyracast.regions.models import Region
from tests.support.admin_setup import admin_system, setup_input, submitted_plant
from tests.support.api import verified_admin
from tests.support.domain import FakeAdapter, System, make_system
from tests.support.historical_reference_import import system_for_import
from tests.support.metering import PROVIDER_PLANT, FixtureClient
from tests.support.plant_operations import make_plant

pytestmark = pytest.mark.django_db(transaction=True)
RETRY = "Another change to this record is in progress, so nothing was saved. Try again in a moment."


@contextmanager
def row_locked_elsewhere(model: type[Model], pk: int) -> Iterator[None]:
    held, release = Event(), Event()

    def hold() -> None:
        try:
            with transaction.atomic():
                model._default_manager.select_for_update().get(pk=pk)
                held.set()
                assert release.wait(timeout=30)
        finally:
            connection.close()

    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '200ms'")
    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        try:
            assert held.wait(timeout=10)
            yield
        finally:
            release.set()
            holder.result(timeout=10)
            with connection.cursor() as cursor:
                cursor.execute("RESET lock_timeout")


@dataclass(frozen=True)
class Form:
    page: str
    action: str
    data: dict[str, Any]
    locked: Model
    unchanged: Callable[[], bool]


def plant_url(region: Region) -> str:
    return reverse("admin:workspace_plant", args=(region.pk,))


def binding_action(system: System, action: str, **changes: object) -> dict[str, object]:
    return {
        "action": action,
        "expected_revision": system.region.revision,
        "binding_id": system.binding.pk,
        "credential_revision": system.account.credential_revision,
        **changes,
    }


def configure(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    system = admin_system()
    url = plant_url(system.region)
    revision = system.region.revision
    return Form(
        url,
        url,
        setup_input(system, site_id="moved-site"),
        system.region,
        lambda: Region.objects.get(pk=system.region.pk).revision == revision,
    )


def validate(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    system = admin_system()
    monkeypatch.setattr(SolcastProvider, "validate", lambda self, binding: None)
    url = plant_url(system.region)
    return Form(
        url,
        url,
        binding_action(system, "validate"),
        system.account,
        lambda: not RequestAttempt.objects.exists(),
    )


def approve(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    system = admin_system()
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), timezone.now())
    submitted_plant(system)
    url = plant_url(system.region)
    return Form(
        url,
        url,
        binding_action(system, "approve"),
        system.region,
        lambda: Region.objects.get(pk=system.region.pk).status == "pending",
    )


def clear_cooldown(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    system = admin_system()
    blocked_until = timezone.now() + timedelta(hours=2)
    ProviderAccount.objects.filter(pk=system.account.pk).update(request_blocked_until=blocked_until)
    url = plant_url(system.region)
    return Form(
        url,
        url,
        binding_action(
            system,
            "clear_cooldown",
            expected_cooldown_until=blocked_until.isoformat(),
            provider_recovered="on",
        ),
        system.account,
        lambda: (
            ProviderAccount.objects.get(pk=system.account.pk).request_blocked_until == blocked_until
        ),
    )


def epias_connect(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    system, plant = make_plant(monkeypatch)
    monkeypatch.setattr("pyracast.metering.operations.get_epias_client", FixtureClient)
    url = plant_url(system.region)
    return Form(
        url,
        url,
        {
            "action": "epias_connect",
            "epias-provider_plant_id": 7,
            "epias-expected_eic": PROVIDER_PLANT.eic,
        },
        system.region,
        lambda: not EpiasConnection.objects.filter(plant=plant).exists(),
    )


def epias_disconnect(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    system, plant = make_plant(monkeypatch)
    saved = EpiasConnection.objects.create(
        plant=plant,
        provider_plant_id=8,
        eic="40X0000000000002",
        name="Saved plant",
        connected_by=system.admin,
    )
    url = plant_url(system.region)
    return Form(
        url,
        url,
        {"action": "epias_disconnect"},
        system.region,
        lambda: EpiasConnection.objects.get(pk=saved.pk).enabled,
    )


def history_file() -> SimpleUploadedFile:
    rows = [{"period_end": "2026-11-07T23:00:00+02:00", "period": "PT30M", "pv_power_rooftop": 0.5}]
    return SimpleUploadedFile(
        "history.json",
        json.dumps({"estimated_actuals": rows}).encode(),
        content_type="application/json",
    )


def historical_save(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    system = system_for_import(monkeypatch)
    Plant.objects.create(region=system.region, name="Reference plant")
    url = reverse("admin:workspace_historical_import", args=(system.region.pk,))
    preview = client.post(url, {"action": "preview", "file": history_file()})
    return Form(
        url,
        url,
        {
            "action": "save",
            "file": history_file(),
            "preview_token": preview.context["preview_token"],
            "settings_confirmed": "on",
        },
        system.region,
        lambda: not HistoricalReferenceImport.objects.exists(),
    )


def decision(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    system = admin_system()
    return Form(
        plant_url(system.region),
        reverse("admin:workspace_decision", args=(system.region.pk,)),
        {
            "expected_revision": system.region.revision,
            "expected_status": system.region.status,
            "action": "reject",
        },
        system.region,
        lambda: Region.objects.get(pk=system.region.pk).status == "pending",
    )


def plant_details(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    system, plant = make_plant(monkeypatch)
    url = reverse("admin:workspace_plant_details", args=(system.region.pk,))
    form = client.get(url).context["form"]
    return Form(
        url,
        url,
        {
            **{field: form[field].value() for field in form.fields},
            "name": "Renamed plant",
            "reason": "",
        },
        system.tenant,
        lambda: Plant.objects.get(pk=plant.pk).name == "Plant",
    )


def settings_form(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    row, _ = NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})
    url = reverse("admin:workspace_settings")
    return Form(
        url,
        url,
        {"max_recipients": 8, "expected_max_recipients": 5},
        row,
        lambda: NotificationSettings.objects.get().max_recipients == 5,
    )


def workspace() -> tuple[Tenant, User, User]:
    tenant = Tenant.objects.create(name="Solar workspace")
    owner = User.objects.create_user(username="owner", email="owner@example.com")
    viewer = User.objects.create_user(username="viewer", email="viewer@example.com")
    Membership.objects.create(tenant=tenant, user=owner, role=WorkspaceRole.ADMIN)
    Membership.objects.create(tenant=tenant, user=viewer)
    return tenant, owner, viewer


def tenant_url(tenant: Tenant) -> str:
    return reverse("admin:workspace_tenant", args=(tenant.pk,))


def rename(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    tenant, _, _ = workspace()
    url = reverse("admin:workspace_tenant_name", args=(tenant.pk,))
    return Form(
        url,
        url,
        {"name": "Renamed", "expected_name": tenant.name},
        tenant,
        lambda: Tenant.objects.get(pk=tenant.pk).name == "Solar workspace",
    )


def administrator(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    tenant, owner, viewer = workspace()
    url = reverse("admin:workspace_tenant_administrator", args=(tenant.pk,))
    return Form(
        url,
        url,
        {"administrator": viewer.pk, "expected_admin_id": owner.pk},
        tenant,
        lambda: Membership.objects.get(user=owner).role == WorkspaceRole.ADMIN,
    )


def invitation(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    tenant, _, _ = workspace()
    url = reverse("admin:workspace_invitation_new", args=(tenant.pk,))
    return Form(
        url,
        url,
        {"email": "guest@example.com", "role": WorkspaceRole.VIEWER, "language": "en"},
        tenant,
        lambda: not WorkspaceInvitation.objects.exists(),
    )


def reissue(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    tenant, _, _ = workspace()
    sent = invite_workspace_member(operator, tenant.pk, "guest@example.com", platform=True)
    WorkspaceInvitation.objects.update(created_at=timezone.now() - timedelta(minutes=2))
    return Form(
        tenant_url(tenant),
        reverse("admin:workspace_invitation_reissue", args=(tenant.pk, sent.pk)),
        {},
        tenant,
        lambda: WorkspaceInvitation.objects.count() == 1,
    )


def revocation(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    tenant, _, _ = workspace()
    sent = invite_workspace_member(operator, tenant.pk, "guest@example.com", platform=True)
    return Form(
        tenant_url(tenant),
        reverse("admin:workspace_invitation_revocation", args=(tenant.pk, sent.pk)),
        {},
        tenant,
        lambda: WorkspaceInvitation.objects.get().expires_at > timezone.now(),
    )


def removal(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    tenant, _, viewer = workspace()
    url = reverse("admin:workspace_member_removal", args=(tenant.pk, viewer.pk))
    return Form(url, url, {}, tenant, lambda: Membership.objects.filter(user=viewer).exists())


def new_plant(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    tenant, _, _ = workspace()
    url = reverse("admin:workspace_plant_new", args=(tenant.pk,))
    return Form(
        url,
        url,
        {
            "name": "Console plant",
            "latitude": "41.0082",
            "longitude": "28.9784",
            "timezone": "Europe/Istanbul",
            "dc_capacity_mw": "1.5",
            "measurement_basis": "net_export",
            "mounting_type": "unknown",
        },
        tenant,
        lambda: not Region.objects.filter(tenant=tenant).exists(),
    )


def language(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    page = reverse("admin:workspace_plants")
    return Form(
        page,
        reverse("admin:workspace_language"),
        {"language": "tr", "next": page},
        operator,
        lambda: not UserLanguagePreference.objects.filter(user=operator).exists(),
    )


FORMS: dict[str, Callable[[pytest.MonkeyPatch, Client, User], Form]] = {
    "configure": configure,
    "validate": validate,
    "approve": approve,
    "clear_cooldown": clear_cooldown,
    "epias_connect": epias_connect,
    "epias_disconnect": epias_disconnect,
    "historical_save": historical_save,
    "decision": decision,
    "plant_details": plant_details,
    "settings": settings_form,
    "rename": rename,
    "administrator": administrator,
    "invitation": invitation,
    "reissue": reissue,
    "revocation": revocation,
    "removal": removal,
    "new_plant": new_plant,
    "language": language,
}


@pytest.mark.parametrize("name", FORMS)
def test_each_console_form_posted_under_a_held_row_lock_answers_a_retry_message(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    client, operator = verified_admin()
    client.raise_request_exception = False
    form = FORMS[name](monkeypatch, client, operator)

    with row_locked_elsewhere(type(form.locked), form.locked.pk):
        response = client.post(form.action, form.data, HTTP_REFERER=f"http://testserver{form.page}")

    assert (response.status_code, response["Location"]) == (302, form.page)
    assert RETRY in client.get(form.page).content.decode()
    assert form.unchanged()


def test_a_retry_returns_to_the_console_overview_when_the_form_page_is_unknown() -> None:
    client, _ = verified_admin()
    client.raise_request_exception = False
    row, _ = NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})

    with row_locked_elsewhere(NotificationSettings, row.pk):
        response = client.post(
            reverse("admin:workspace_settings"),
            {"max_recipients": 8, "expected_max_recipients": 5},
            HTTP_REFERER="https://elsewhere.example/admin/settings/",
        )

    assert (response.status_code, response["Location"]) == (302, reverse("admin:index"))
    assert RETRY in client.get(reverse("admin:index")).content.decode()


def busy(session_key: str) -> Any:
    raise session_lock.SessionBusyError


@pytest.mark.parametrize("method", ["get", "post"])
def test_a_busy_session_answers_a_console_request_with_a_page(method: str) -> None:
    client = Client()
    client.cookies["sessionid"] = "a" * 32
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_lock, "serialize_session", busy)
        response = getattr(client, method)("/admin/settings/?session_retry=1")

    assert response.status_code == 503
    assert response["Content-Type"].startswith("text/html")
    assert response["Retry-After"] == "1"
    assert "Another request from this session is still running." in response.content.decode()
    assert "Refresh" not in response


def test_a_busy_session_never_replays_a_console_write() -> None:
    client = Client()
    client.cookies["sessionid"] = "a" * 32
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(session_lock, "serialize_session", busy)
        response = client.post("/admin/settings/", HTTP_SEC_FETCH_MODE="navigate")

    assert response.status_code == 503
    assert "Refresh" not in response


def test_a_stale_validation_page_spends_no_second_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = admin_system()
    monkeypatch.setattr(SolcastProvider, "validate", lambda self, binding: None)
    client, _ = verified_admin()
    page = client.get(plant_url(system.region))
    fields = {
        name: page.context["action_form"][name].value()
        for name in page.context["action_form"].fields
    }
    data = {
        key: value
        for key, value in fields.items()
        if value is not None and key != "provider_recovered"
    }

    first = client.post(plant_url(system.region), {**data, "action": "validate"})
    second = client.post(plant_url(system.region), {**data, "action": "validate"})

    assert first.status_code == 302
    assert second.status_code == 400
    assert RequestAttempt.objects.count() == 1
    assert "validated after this page opened" in second.content.decode()


def turkish(client: Client, path: str) -> str:
    return str(client.get(path, HTTP_ACCEPT_LANGUAGE="tr").content.decode())


def test_the_console_names_plant_and_job_states_in_turkish() -> None:
    system = make_system(approved=True)
    CollectionJob.objects.filter(binding=system.binding).update(status="stale")
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})
    client, _ = verified_admin()

    plants = turkish(client, reverse("admin:workspace_plants"))
    operations = turkish(client, reverse("admin:workspace_operations"))
    settings_page = turkish(client, reverse("admin:workspace_settings"))
    review = turkish(client, plant_url(system.region))

    assert ">Approved<" not in plants
    assert "Onayland\u0131" in plants
    assert ">Stale<" not in operations
    assert "Güncel değil" in operations
    assert "al\u0131c\u0131s" not in settings_page
    assert "Santral baş\u0131na 5 al\u0131c\u0131" in settings_page
    assert "Approved" not in review
