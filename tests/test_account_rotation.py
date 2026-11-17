from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connections, transaction

from pyracast.connections.models import ProviderAccount
from pyracast.connections.operations import create_account, rotate_secret
from pyracast.connections.secrets import decrypt_api_key
from pyracast.forecasts.models import RequestAttempt
from pyracast.regions.models import AuditEvent
from pyracast.regions.operations import approve_region
from tests.support.api import verified_admin
from tests.support.domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_account_creation_rejects_duplicate_identity_without_rotating_credentials() -> None:
    _, actor = verified_admin()
    reference = "test-only-first-key"
    account = create_account(
        actor,
        provider="fixture",
        external_account_id="canonical",
        api_key=reference,
        daily_quota=1,
        require_new=True,
    )
    with pytest.raises(ValidationError) as duplicate:
        create_account(
            actor,
            provider="fixture",
            external_account_id="canonical",
            api_key="test-only-discarded-key",
            require_new=True,
        )

    account = ProviderAccount.objects.get()
    assert decrypt_api_key(account.encrypted_api_key) == reference
    assert account.credential_revision == 1
    assert account.daily_quota == 1
    assert "test-only-discarded-key" not in str(duplicate.value)
    event = AuditEvent.objects.get(provider_account=account)
    assert event.actor_id == actor.pk
    assert event.region_id is None
    assert event.action == "provider_account_created"
    assert event.revision == 1
    assert reference not in str(AuditEvent.objects.values().get(pk=event.pk))


def test_rotation_is_write_only_invalidates_validation_and_preserves_account_quota() -> None:
    system = make_system(approved=True)
    _, actor = verified_admin()
    reference = "test-only-replacement-key"
    attempts = list(RequestAttempt.objects.values())
    quota = system.account.daily_quota

    rotate_secret(actor, system.account.pk, reference)

    system.account.refresh_from_db()
    assert decrypt_api_key(system.account.encrypted_api_key) == reference
    assert system.account.credential_revision == 2
    assert system.account.daily_quota == quota
    assert list(RequestAttempt.objects.values()) == attempts
    event = AuditEvent.objects.get(
        provider_account=system.account, action="provider_secret_rotated"
    )
    assert event.actor_id == actor.pk
    assert event.region_id is None
    assert event.revision == 2
    assert reference not in str(AuditEvent.objects.values().get(pk=event.pk))
    with pytest.raises(ValidationError):
        approve_region(system.admin, system.region.pk, system.binding.pk, NOW)


@pytest.mark.parametrize("identity", ["member", "staff", "superuser"])
def test_rotation_requires_platform_administrator(identity: str) -> None:
    system = make_system()
    user = (
        system.member
        if identity == "member"
        else User.objects.create_user(
            username=identity,
            is_staff=identity == "staff",
            is_superuser=identity == "superuser",
        )
    )
    initial_events = AuditEvent.objects.count()

    with pytest.raises(PermissionDenied):
        rotate_secret(user, system.account.pk, "test-only-replacement")

    system.account.refresh_from_db()
    assert system.account.credential_revision == 1
    assert AuditEvent.objects.count() == initial_events


@pytest.mark.parametrize("api_key", ["", "a b", "x" * 1025])
def test_invalid_rotation_inputs_remain_private_and_unchanged(api_key: str) -> None:
    system = make_system()
    initial_events = AuditEvent.objects.count()

    with pytest.raises(ValidationError, match="without spaces"):
        rotate_secret(system.admin, system.account.pk, api_key)

    system.account.refresh_from_db()
    assert system.account.credential_revision == 1
    assert AuditEvent.objects.count() == initial_events


def test_rotation_of_missing_account_changes_nothing() -> None:
    system = make_system()
    initial_events = AuditEvent.objects.count()

    with pytest.raises(ProviderAccount.DoesNotExist):
        rotate_secret(system.admin, 999999, "test-only-replacement")

    assert AuditEvent.objects.count() == initial_events


@pytest.mark.parametrize("operation", ["create", "rotate"])
def test_account_mutation_rolls_back_if_audit_insert_fails(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    system = make_system()
    original_reference = system.account.encrypted_api_key
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
                api_key=original_reference,
            )
        else:
            rotate_secret(system.admin, system.account.pk, "test-only-rollback")

    with pytest.raises(IntegrityError):
        mutate_account()

    assert not ProviderAccount.objects.filter(external_account_id="rollback-account").exists()
    system.account.refresh_from_db()
    assert system.account.encrypted_api_key == original_reference
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
        futures = [executor.submit(rotate, f"test-only-rotation-{number}") for number in (1, 2)]
        for future in futures:
            future.result(timeout=10)

    system.account.refresh_from_db()
    assert system.account.credential_revision == 3
    assert decrypt_api_key(system.account.encrypted_api_key) in {
        "test-only-rotation-1",
        "test-only-rotation-2",
    }
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


def test_solcast_creation_without_key_is_refused() -> None:
    _, actor = verified_admin()
    with pytest.raises(ValidationError):
        create_account(
            actor,
            provider="solcast",
            external_account_id="missing-key",
            api_key="",
            authorization_reference="test-only-authorized",
        )
    assert not ProviderAccount.objects.exists()
