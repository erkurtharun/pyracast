from datetime import timedelta
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.db.models import F

from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import create_account, rotate_secret
from pyracast.connections.recovery import clear_provider_cooldown
from pyracast.forecasts.quota import record_provider_failure
from pyracast.forecasts.validation import validate_binding
from pyracast.regions.operations import configure_binding
from tests.support.domain import NOW, FakeAdapter, make_system, provider_key
from tests.support.invariants import PROVIDER_ACCOUNT_RULE, SITE_BINDING_RULE, violations

pytestmark = pytest.mark.django_db(transaction=True)

MAX_DAILY_QUOTA = 10


def _clean() -> tuple[int, int]:
    return (
        violations("connections_provideraccount", PROVIDER_ACCOUNT_RULE),
        violations("connections_sitebinding", SITE_BINDING_RULE),
    )


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"daily_quota": 0}, "provideraccount_daily_quota_chk"),
        ({"daily_quota": MAX_DAILY_QUOTA + 1}, "provideraccount_daily_quota_chk"),
        ({"credential_revision": 0}, "provideraccount_credential_revision_chk"),
        (
            {"authentication_failed_revision": 0},
            "provideraccount_authentication_failed_revision_chk",
        ),
        (
            {"authentication_failed_revision": F("credential_revision") + 1},
            "provideraccount_authentication_failed_revision_chk",
        ),
    ],
)
def test_provider_account_rejects_an_impossible_quota_or_credential_state(
    values: dict[str, Any], constraint: str
) -> None:
    system = make_system()
    accounts = ProviderAccount.objects.filter(pk=system.account.pk)

    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        accounts.update(**values)

    accounts.update(daily_quota=MAX_DAILY_QUOTA, authentication_failed_revision=1)
    assert _clean() == (0, 0)


@pytest.mark.parametrize(
    "values",
    [
        {"validated_at": NOW},
        {"validated_credential_revision": 1},
        {"validated_at": NOW, "validated_credential_revision": 0},
    ],
)
def test_binding_validation_is_stored_as_one_moment_and_revision(values: dict[str, Any]) -> None:
    system = make_system()
    bindings = SiteBinding.objects.filter(pk=system.binding.pk)

    with pytest.raises(IntegrityError, match="sitebinding_validated_chk"), transaction.atomic():
        bindings.update(**values)

    bindings.update(validated_at=NOW, validated_credential_revision=1)
    assert _clean() == (0, 0)


def test_one_binding_per_region_revision() -> None:
    system = make_system()
    duplicate = SiteBinding.objects.get(pk=system.binding.pk)
    duplicate.pk = None

    with (
        pytest.raises(IntegrityError, match="sitebinding_region_region_revision_uniq"),
        transaction.atomic(),
    ):
        duplicate.save()

    duplicate.region_revision += 1
    duplicate.save()
    assert SiteBinding.objects.filter(region=system.region).count() == 2


def test_provider_account_and_binding_writers_keep_their_invariants() -> None:
    system = make_system(approved=True)
    assert _clean() == (0, 0)
    record_provider_failure(
        system.account, ProviderError("rejected", status_code=401), NOW + timedelta(minutes=1)
    )
    record_provider_failure(
        system.account, ProviderError("throttled", status_code=429), NOW + timedelta(minutes=1)
    )
    account = ProviderAccount.objects.get(pk=system.account.pk)
    assert account.authentication_failed_revision == account.credential_revision
    assert _clean() == (0, 0)
    clear_provider_cooldown(
        system.admin,
        account.pk,
        account.credential_revision,
        expected_blocked_until=account.request_blocked_until,
    )
    rotate_secret(system.admin, account.pk, provider_key("ROTATED"))
    assert _clean() == (0, 0)
    configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=account.pk,
        site_id="moved",
        capacity_kw=2,
        capacity_semantics_verified=True,
    )
    assert SiteBinding.objects.filter(region=system.region).count() == 2
    assert _clean() == (0, 0)
    created = create_account(
        system.admin,
        provider="fixture",
        external_account_id="limited",
        daily_quota=MAX_DAILY_QUOTA,
    )
    assert created.daily_quota == MAX_DAILY_QUOTA
    assert _clean() == (0, 0)


def test_validation_records_the_moment_with_the_credential_revision() -> None:
    system = make_system()

    def refuse() -> None:
        raise ProviderError("unavailable", status_code=503)

    with pytest.raises(ProviderError):
        validate_binding(system.admin, system.binding.pk, FakeAdapter(refuse), NOW)
    assert _clean() == (0, 0)
    validated = validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    assert (validated.validated_at, validated.validated_credential_revision) == (NOW, 1)
    assert _clean() == (0, 0)
