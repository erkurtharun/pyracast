from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.test import Client
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from pyracast.connections.models import ProviderAccount
from pyracast.connections.operations import create_account, rotate_secret
from pyracast.forecasts.models import RequestAttempt
from pyracast.regions.models import AuditEvent
from pyracast.regions.operations import approve_region
from tests.test_api import verified_admin
from tests.test_domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_account_creation_is_idempotent_and_audited_without_credentials() -> None:
    client, actor = verified_admin()
    reference = "env:PYRACAST_SOLCAST_FIRST"
    data = {
        "provider": "fixture",
        "external_account_id": "canonical",
        "secret_reference": reference,
    }

    first = client.post("/api/platform/accounts/", data, content_type="application/json")
    duplicate = client.post("/api/platform/accounts/", data, content_type="application/json")

    assert first.status_code == 201
    assert duplicate.json() == first.json()
    account = ProviderAccount.objects.get()
    assert first.json() == {"id": account.pk}
    event = AuditEvent.objects.get(provider_account=account)
    assert event.actor_id == actor.pk
    assert event.region_id is None
    assert event.action == "provider_account_created"
    assert event.revision == 1
    assert reference not in first.content.decode()
    assert reference not in str(AuditEvent.objects.values().get(pk=event.pk))


def test_rotation_is_write_only_invalidates_validation_and_preserves_account_quota() -> None:
    system = make_system(approved=True)
    client, actor = verified_admin()
    reference = "env:PYRACAST_SOLCAST_REPLACEMENT"
    attempts = list(RequestAttempt.objects.values())
    quota = system.account.daily_quota
    path = f"/api/platform/accounts/{system.account.pk}/rotate-secret/"

    response = client.post(path, {"secret_reference": reference}, content_type="application/json")

    assert response.status_code == 200
    assert response.json() == {"id": system.account.pk}
    system.account.refresh_from_db()
    assert system.account.secret_reference == reference
    assert system.account.credential_revision == 2
    assert system.account.daily_quota == quota
    assert list(RequestAttempt.objects.values()) == attempts
    event = AuditEvent.objects.get(
        provider_account=system.account, action="provider_secret_rotated"
    )
    assert event.actor_id == actor.pk
    assert event.region_id is None
    assert event.revision == 2
    assert reference not in response.content.decode()
    assert reference not in str(AuditEvent.objects.values().get(pk=event.pk))
    with pytest.raises(ValidationError):
        approve_region(system.admin, system.region.pk, system.binding.pk, NOW)


@pytest.mark.parametrize("identity", ["anonymous", "member", "staff", "unverified_admin"])
def test_rotation_requires_platform_administrator_with_mfa(identity: str) -> None:
    system = make_system()
    client = Client()
    if identity != "anonymous":
        user = (
            system.member
            if identity == "member"
            else User.objects.create_user(
                username=identity,
                is_staff=identity == "staff",
                is_superuser=identity == "unverified_admin",
            )
        )
        client.force_login(user)
    initial_events = AuditEvent.objects.count()

    response = client.post(
        f"/api/platform/accounts/{system.account.pk}/rotate-secret/",
        {"secret_reference": "env:REPLACEMENT"},
        content_type="application/json",
    )

    assert response.status_code == 403
    system.account.refresh_from_db()
    assert system.account.credential_revision == 1
    assert AuditEvent.objects.count() == initial_events


def test_rotation_requires_csrf_for_session_authentication() -> None:
    system = make_system()
    _, user = verified_admin()
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = TOTPDevice.objects.get(user=user).persistent_id
    session.save()
    path = f"/api/platform/accounts/{system.account.pk}/rotate-secret/"
    data = {"secret_reference": "env:REPLACEMENT"}

    assert client.post(path, data, content_type="application/json").status_code == 403
    assert client.get("/auth/login/").status_code == 200
    response = client.post(
        path,
        data,
        content_type="application/json",
        HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
    )

    assert response.status_code == 200
    assert AuditEvent.objects.filter(action="provider_secret_rotated").count() == 1


@pytest.mark.parametrize(
    "data",
    [
        {"secret_reference": "synthetic-raw-credential"},
        {"secret_reference": "env:invalid-name"},
        {"secret_reference": "env:VALID", "api_key": "synthetic-raw-credential"},
        {},
    ],
)
def test_invalid_rotation_inputs_remain_private_and_unchanged(data: dict[str, str]) -> None:
    system = make_system()
    client, _ = verified_admin()
    initial_events = AuditEvent.objects.count()

    response = client.post(
        f"/api/platform/accounts/{system.account.pk}/rotate-secret/",
        data,
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "synthetic-raw-credential" not in response.content.decode()
    system.account.refresh_from_db()
    assert system.account.credential_revision == 1
    assert AuditEvent.objects.count() == initial_events


def test_rotation_of_missing_account_returns_private_not_found() -> None:
    client, _ = verified_admin()

    response = client.post(
        "/api/platform/accounts/999999/rotate-secret/",
        {"secret_reference": "env:REPLACEMENT"},
        content_type="application/json",
    )

    assert response.status_code == 404
    assert "REPLACEMENT" not in response.content.decode()
    assert not AuditEvent.objects.exists()


@pytest.mark.parametrize("operation", ["create", "rotate"])
def test_account_mutation_rolls_back_if_audit_insert_fails(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    system = make_system()
    original_reference = system.account.secret_reference
    insert = AuditEvent.objects.create

    def reject_audit(**values: Any) -> AuditEvent:
        values["revision"] = -1
        return insert(**values)

    monkeypatch.setattr(AuditEvent.objects, "create", reject_audit)

    def mutate_account() -> None:
        if operation == "create":
            create_account(
                system.admin,
                provider="fixture",
                external_account_id="rollback-account",
                secret_reference=original_reference,
            )
        else:
            rotate_secret(system.admin, system.account.pk, "env:ROLLBACK")

    with pytest.raises(IntegrityError):
        mutate_account()

    assert not ProviderAccount.objects.filter(external_account_id="rollback-account").exists()
    system.account.refresh_from_db()
    assert system.account.secret_reference == original_reference
    assert system.account.credential_revision == 1


def test_concurrent_rotations_have_serial_credential_revisions_and_audits() -> None:
    system = make_system()
    barrier = Barrier(2)

    def rotate(reference: str) -> None:
        try:
            barrier.wait(timeout=5)
            rotate_secret(system.admin, system.account.pk, reference)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(rotate, f"env:ROTATION_{number}") for number in (1, 2)]
        for future in futures:
            future.result(timeout=10)

    system.account.refresh_from_db()
    assert system.account.credential_revision == 3
    assert system.account.secret_reference in {"env:ROTATION_1", "env:ROTATION_2"}
    assert list(
        AuditEvent.objects.filter(provider_account=system.account, action="provider_secret_rotated")
        .order_by("revision")
        .values_list("revision", flat=True)
    ) == [2, 3]


@pytest.mark.parametrize("targets", ["none", "both"])
def test_audit_event_database_constraint_requires_exactly_one_target(targets: str) -> None:
    system = make_system()

    with pytest.raises(IntegrityError), transaction.atomic():
        AuditEvent.objects.create(
            actor=system.admin,
            region=system.region if targets == "both" else None,
            provider_account=system.account if targets == "both" else None,
            action="invalid_target",
            revision=1,
        )
