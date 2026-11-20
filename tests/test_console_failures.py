import json
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from threading import Event
from typing import Any

import pytest
from allauth.mfa.models import Authenticator
from allauth.usersessions.models import UserSession
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, connection, transaction
from django.db.models import Model
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from psycopg.errors import DeadlockDetected, LockNotAvailable, QueryCanceled

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
from pyracast.forecasts.models import CollectionJob, HistoricalReferenceImport, RequestAttempt
from pyracast.forecasts.validation import validate_binding
from pyracast.metering.models import EpiasConnection
from pyracast.notifications.models import NotificationSettings
from pyracast.plants.models import Plant
from pyracast.providers import SolcastProvider
from pyracast.regions.models import Region
from tests.support.account_sessions import totp_clock, with_totp
from tests.support.admin_setup import admin_system, review_fields, setup_input, submitted_plant
from tests.support.api import verified_admin
from tests.support.auth_lifecycle import totp_code
from tests.support.domain import FakeAdapter, System, make_system
from tests.support.historical_reference_import import system_for_import
from tests.support.metering import PROVIDER_PLANT, FixtureClient
from tests.support.plant_operations import make_plant
from tests.support.step_up import proofs, with_proof

pytestmark = pytest.mark.django_db(transaction=True)
RETRY = "Another change to this record is in progress, so nothing was saved. Try again in a moment."


@contextmanager
def held_elsewhere(lock: Callable[[], object]) -> Iterator[None]:
    held, release = Event(), Event()

    def hold() -> None:
        try:
            with transaction.atomic():
                lock()
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


def row_locked_elsewhere(
    model: type[Model], pk: int, *, no_key: bool = False
) -> AbstractContextManager[None]:
    return held_elsewhere(
        lambda: model._default_manager.select_for_update(no_key=no_key).get(pk=pk)
    )


@dataclass(frozen=True)
class Form:
    page: str
    action: str
    data: dict[str, Any]
    locked: Model
    unchanged: Callable[[], bool]
    no_key: bool = False


def plant_url(region: Region) -> str:
    return reverse("admin:workspace_plant", args=(region.pk,))


def binding_action(system: System, action: str, **changes: object) -> dict[str, object]:
    return {
        "action": action,
        "expected_revision": system.region.revision,
        "binding_id": system.binding.pk,
        "credential_revision": system.account.credential_revision,
        **review_fields(system),
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


def clear_delay(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    system = admin_system()
    blocked_until = timezone.now() + timedelta(hours=2)
    ProviderAccount.objects.filter(pk=system.account.pk).update(request_blocked_until=blocked_until)
    url = reverse("admin:workspace_provider_account", args=(system.account.pk,))
    return Form(
        url,
        f"{url}delay/",
        {
            "expected_revision": system.account.credential_revision,
            "expected_blocked_until": blocked_until.isoformat(),
            "provider_recovered": "on",
        },
        system.account,
        lambda: (
            ProviderAccount.objects.get(pk=system.account.pk).request_blocked_until == blocked_until
        ),
    )


def key_replacement(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    system = admin_system()
    original = system.account.encrypted_api_key
    url = reverse("admin:workspace_provider_account", args=(system.account.pk,))
    return Form(
        url,
        f"{url}key/",
        {
            "expected_revision": system.account.credential_revision,
            "api_key": "replacement-key",
            "confirm_replacement": "on",
        },
        system.account,
        lambda: ProviderAccount.objects.get(pk=system.account.pk).encrypted_api_key == original,
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
        lambda: WorkspaceInvitation.objects.get().revoked_at is None,
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
        # Account operations hold the user row without its key; a full row lock would also stop
        # the session record a request inserts before any console view runs.
        no_key=True,
    )


def confirm(monkeypatch: pytest.MonkeyPatch, client: Client, operator: User) -> Form:
    now = time.time()
    totp_clock(monkeypatch, now)
    Authenticator.objects.filter(user=operator).delete()
    secret = with_totp(operator)
    factor = Authenticator.objects.get(user=operator)
    with_proof(
        client, operator, "mfa", age=60, reauthenticated=False, type=factor.type, id=factor.pk
    )
    url = reverse("admin:workspace_confirm")
    settings_page = reverse("admin:workspace_settings")
    return Form(
        f"{url}?next={settings_page}",
        url,
        {"code": totp_code(secret, now), "next": settings_page},
        operator,
        lambda: factor.pk not in {proof.get("id") for proof in proofs(client)},
        no_key=True,
    )


FORMS: dict[str, Callable[[pytest.MonkeyPatch, Client, User], Form]] = {
    "configure": configure,
    "validate": validate,
    "approve": approve,
    "clear_delay": clear_delay,
    "key_replacement": key_replacement,
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
    "confirm": confirm,
}


@pytest.mark.parametrize("name", FORMS)
def test_each_console_form_posted_under_a_held_row_lock_answers_a_retry_message(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    client, operator = verified_admin()
    client.raise_request_exception = False
    form = FORMS[name](monkeypatch, client, operator)

    with row_locked_elsewhere(type(form.locked), form.locked.pk, no_key=form.no_key):
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


def test_a_script_sent_console_form_receives_its_retry_message_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, operator = verified_admin()
    client.raise_request_exception = False
    form = historical_save(monkeypatch, client, operator)

    with row_locked_elsewhere(type(form.locked), form.locked.pk):
        response = client.post(form.action, form.data, HTTP_ACCEPT="application/json")

    assert (response.status_code, response["Retry-After"]) == (503, "1")
    assert response.json() == {"message": RETRY}
    assert RETRY not in client.get(form.page).content.decode()
    assert form.unchanged()


def test_a_language_change_never_waits_for_a_record_that_references_the_account() -> None:
    client, operator = verified_admin()
    page = reverse("admin:workspace_plants")

    def record_session() -> None:
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        UserSession.objects.create(
            user=operator, session_key="b" * 32, ip="127.0.0.1", user_agent="Browser"
        )

    with held_elsewhere(record_session):
        response = client.post(
            reverse("admin:workspace_language"), {"language": "tr", "next": page}
        )

    assert (response.status_code, response["Location"]) == (302, page)
    assert UserLanguagePreference.objects.get(user=operator).language == "tr"


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


def database_failure(cause: Exception) -> OperationalError:
    error = OperationalError(str(cause))
    error.__cause__ = cause
    return error


SETTINGS_CHANGE = {"max_recipients": 8, "expected_max_recipients": 5}


@pytest.mark.parametrize(
    ("cause", "code", "message"),
    [
        (DeadlockDetected("deadlock detected"), "database_conflict", RETRY),
        (
            QueryCanceled("canceling statement due to statement timeout"),
            "request_timeout",
            "The change took too long, so nothing was saved. Try again in a moment.",
        ),
    ],
    ids=["deadlock", "timeout"],
)
def test_a_console_write_that_collides_or_times_out_returns_with_a_retry_message(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cause: Exception,
    code: str,
    message: str,
) -> None:
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})
    client, _ = verified_admin()
    page = reverse("admin:workspace_settings")

    def fail(actor: User, value: int, *, expected: int) -> None:
        raise database_failure(cause)

    monkeypatch.setattr("pyracast.platform_admin.pages.set_recipient_limit", fail)
    with caplog.at_level("WARNING", logger="pyracast.platform_admin"):
        response = client.post(page, SETTINGS_CHANGE, HTTP_REFERER=f"http://testserver{page}")

    assert (response.status_code, response["Location"]) == (302, page)
    assert message in client.get(page).content.decode()
    assert [
        record.getMessage() for record in caplog.records if record.name == "pyracast.platform_admin"
    ] == [f"console_write_not_saved code={code} view=admin:workspace_settings"]
    assert NotificationSettings.objects.get().max_recipients == 5


def test_other_database_failures_and_failing_reads_keep_the_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    NotificationSettings.objects.update_or_create(pk=1, defaults={"max_recipients": 5})
    client, _ = verified_admin()
    client.raise_request_exception = False
    page = reverse("admin:workspace_settings")

    def fail(actor: User, value: int, *, expected: int) -> None:
        raise database_failure(ValueError("connection lost"))

    def busy_read(data: object) -> dict[str, Any]:
        raise database_failure(LockNotAvailable("canceling statement due to lock timeout"))

    monkeypatch.setattr("pyracast.platform_admin.pages.set_recipient_limit", fail)
    write = client.post(page, SETTINGS_CHANGE, HTTP_REFERER=f"http://testserver{page}")
    monkeypatch.setattr("pyracast.platform_admin.pages.settings_context", busy_read)
    read = client.get(page)

    assert (write.status_code, read.status_code) == (500, 500)


@pytest.mark.parametrize("valid", [True, False])
def test_an_unknown_plant_action_changes_nothing(valid: bool) -> None:
    system = admin_system()
    client, _ = verified_admin()
    fields = binding_action(system, "unknown") if valid else {"action": "unknown"}

    response = client.post(plant_url(system.region), fields)

    assert response.status_code == 400
    refused = "The setup could not be applied." in response.content.decode()
    assert refused is valid
    assert not RequestAttempt.objects.exists()
