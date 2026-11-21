import logging
from dataclasses import replace
from datetime import date, timedelta

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone, translation

from pyracast.connections import operations
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.recovery import clear_provider_cooldown
from pyracast.connections.secrets import decrypt_api_key, encrypt_secret
from pyracast.forecasts.models import RequestAttempt
from pyracast.forecasts.validation import validate_binding
from pyracast.plants.approval import approve_region, decide_review
from pyracast.platform_admin.provider_accounts import account_context, account_rows
from pyracast.regions.models import AuditEvent
from pyracast.regions.operations import configure_binding, submit_region
from tests.support.admin_setup import setup_input, setup_url
from tests.support.api import verified_admin
from tests.support.domain import FakeAdapter, System, make_system, provider_key

pytestmark = pytest.mark.django_db(transaction=True)

ACCOUNTS = "/admin/provider-accounts/"
STALE = "The account changed. Reload this page."


def page(account: ProviderAccount, action: str = "") -> str:
    return f"{ACCOUNTS}{account.pk}/{action}"


def solcast_system(identity: str = "solar-owner") -> System:
    system = make_system()
    account = operations.create_account(
        system.admin,
        provider="solcast",
        external_account_id=identity,
        api_key=provider_key("CURRENT"),
        authorization_reference="Authorized service use",
    )
    binding = configure_binding(
        system.admin,
        system.region.pk,
        provider_account_id=account.pk,
        site_id="solar-site",
        capacity_kw=1,
        capacity_semantics_verified=True,
        installation_date=date(2020, 1, 1),
        settings_verification_reference="Verified site settings",
    )
    validate_binding(system.admin, binding.pk, FakeAdapter(), timezone.now())
    approve_region(system.admin, system.region.pk, binding.pk, timezone.now())
    system.region.refresh_from_db()
    return replace(system, account=account, binding=binding)


def spend(account: ProviderAccount, purpose: str, outcome: str, count: int = 1) -> None:
    now = timezone.now()
    for _ in range(count):
        RequestAttempt.objects.create(
            provider_account=account,
            utc_day=now.date(),
            created_at=now,
            purpose=purpose,
            outcome=outcome,
        )


def replacement(account: ProviderAccount, **changes: object) -> dict[str, object]:
    return {
        "expected_revision": account.credential_revision,
        "api_key": provider_key("REPLACEMENT"),
        "confirm_replacement": "on",
        **changes,
    }


@pytest.mark.parametrize("identity", ["anonymous", "member", "staff", "unverified_admin"])
def test_only_verified_platform_administrators_reach_provider_accounts(identity: str) -> None:
    system = solcast_system()
    client = Client()
    if identity != "anonymous":
        user = User.objects.create_user(
            username=f"accounts-{identity}",
            is_staff=identity in {"staff", "unverified_admin"},
            is_superuser=identity == "unverified_admin",
        )
        client.force_login(user)

    for path in (ACCOUNTS, page(system.account)):
        assert client.get(path).status_code == 302
    for action in ("key/", "delay/"):
        assert (
            client.post(page(system.account, action), replacement(system.account)).status_code
            == 302
        )
    assert decrypt_api_key(
        ProviderAccount.objects.get(pk=system.account.pk).encrypted_api_key
    ) == provider_key("CURRENT")


def test_the_account_list_shows_each_accounts_plants_and_remaining_budget() -> None:
    system = solcast_system()
    spend(system.account, "forecast", "succeeded", 2)
    spend(system.account, "validation", "failed")
    client, _ = verified_admin()

    response = client.get(ACCOUNTS)

    assert response.status_code == 200
    row = next(row for row in response.context["accounts"] if row["account"] == system.account)
    assert (row["plants"], row["used_today"], row["remaining_today"]) == (1, 4, 6)
    assert b"solar-owner" in response.content
    assert page(system.account).encode() in response.content


def test_the_account_page_shows_bindings_health_budget_and_never_the_key() -> None:
    system = solcast_system()
    spend(system.account, "forecast", "succeeded", 2)
    spend(system.account, "validation", "failed")
    client, _ = verified_admin()

    response = client.get(page(system.account))

    assert response.status_code == 200
    content = response.content.decode()
    assert system.region.name in content
    assert system.tenant.name in content
    plant = response.context["bindings"][0]
    assert (plant["binding"], plant["current"], plant["approved"]) == (system.binding, True, True)
    budget = response.context["budget"]
    assert (budget.quota, budget.used, budget.remaining) == (10, 4, 6)
    assert ("forecast", "succeeded", 2) in [
        (row["purpose"], row["outcome"], row["count"]) for row in response.context["ledger"]
    ]
    account = ProviderAccount.objects.get(pk=system.account.pk)
    for secret in (provider_key("CURRENT"), account.encrypted_api_key, account.api_key_fingerprint):
        assert str(secret) not in content


def test_replacing_the_key_checks_the_revision_the_page_showed(
    pyracast_logs: pytest.LogCaptureFixture,
) -> None:
    pyracast_logs.set_level(logging.DEBUG)
    system = solcast_system()
    client, operator = verified_admin()
    operations.replace_api_key(
        system.admin, system.account.pk, provider_key("INTERIM"), expected_revision=1
    )

    stale = client.post(page(system.account, "key/"), replacement(system.account))

    assert stale.status_code == 400
    assert STALE in stale.content.decode()
    account = ProviderAccount.objects.get(pk=system.account.pk)
    assert decrypt_api_key(account.encrypted_api_key) == provider_key("INTERIM")

    saved = client.post(page(system.account, "key/"), replacement(account), follow=True)

    assert saved.redirect_chain[0] == (page(system.account), 302)
    account.refresh_from_db()
    assert account.credential_revision == 3
    assert decrypt_api_key(account.encrypted_api_key) == provider_key("REPLACEMENT")
    assert account.revalidation_due_at is not None
    assert AuditEvent.objects.filter(
        provider_account=account, action="provider_secret_rotated", actor=operator
    ).exists()
    assert any("API key replaced" in str(notice) for notice in saved.context["messages"])
    for response in (stale, saved):
        assert provider_key("REPLACEMENT").encode() not in response.content
    assert provider_key("REPLACEMENT") not in pyracast_logs.text


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"confirm_replacement": ""}, "confirm_replacement"),
        ({"api_key": "with a space"}, "api_key"),
        ({"api_key": ""}, "api_key"),
    ],
)
def test_an_incomplete_key_replacement_keeps_the_key(
    changes: dict[str, object], field: str
) -> None:
    system = solcast_system()
    client, _ = verified_admin()

    response = client.post(page(system.account, "key/"), replacement(system.account, **changes))

    assert response.status_code == 400
    assert field in response.context["key_form"].errors
    assert b"with a space" not in response.content
    account = ProviderAccount.objects.get(pk=system.account.pk)
    assert (account.credential_revision, decrypt_api_key(account.encrypted_api_key)) == (
        1,
        provider_key("CURRENT"),
    )


def test_a_key_stored_on_another_account_is_refused_on_the_page() -> None:
    system = solcast_system()
    operations.create_account(
        system.admin,
        provider="solcast",
        external_account_id="other-owner",
        api_key=provider_key("REPLACEMENT"),
        authorization_reference="Authorized service use",
    )
    client, _ = verified_admin()

    response = client.post(page(system.account, "key/"), replacement(system.account))

    assert response.status_code == 400
    assert "other-owner" in response.content.decode()
    assert ProviderAccount.objects.get(pk=system.account.pk).credential_revision == 1


def test_a_confirmed_provider_delay_is_cleared_on_the_account_page() -> None:
    system = solcast_system()
    until = timezone.now().replace(microsecond=0) + timedelta(hours=3)
    ProviderAccount.objects.filter(pk=system.account.pk).update(request_blocked_until=until)
    client, operator = verified_admin()
    data = {"expected_revision": 1, "expected_blocked_until": until.isoformat()}

    refused = client.post(page(system.account, "delay/"), data)
    cleared = client.post(page(system.account, "delay/"), {**data, "provider_recovered": "on"})

    assert refused.status_code == 400
    assert cleared.status_code == 302
    assert ProviderAccount.objects.get(pk=system.account.pk).request_blocked_until is None
    assert AuditEvent.objects.filter(action="provider_cooldown_cleared", actor=operator).exists()


def test_an_account_whose_key_is_also_stored_elsewhere_asks_for_a_new_key() -> None:
    system = solcast_system()
    ProviderAccount.objects.filter(pk=system.account.pk).update(api_key_fingerprint=None)
    client, _ = verified_admin()

    response = client.get(page(system.account))

    assert response.context["fingerprint_missing"] is True
    assert "Replace this key" in response.content.decode()


def test_the_plant_page_links_to_its_account_and_no_longer_replaces_its_key() -> None:
    system = solcast_system()
    client, _ = verified_admin()

    response = client.get(setup_url(system))
    client.post(
        setup_url(system),
        setup_input(system, api_key=provider_key("IGNORED"), confirm_key_rotation=True),
    )

    assert page(system.account).encode() in response.content
    assert b"confirm_key_rotation" not in response.content
    account = ProviderAccount.objects.get(pk=system.account.pk)
    assert account.credential_revision == 1
    assert decrypt_api_key(account.encrypted_api_key) == provider_key("CURRENT")


def test_key_and_recovery_refusals_are_translated() -> None:
    system = solcast_system()
    with translation.override("tr"):
        with pytest.raises(ValidationError) as key:
            encrypt_secret("with a space")
        with pytest.raises(ValidationError) as recovery:
            clear_provider_cooldown(
                system.admin, system.account.pk, 99, expected_blocked_until=None
            )
        with pytest.raises(ValidationError) as replacement_refusal:
            operations.replace_api_key(
                system.admin, system.account.pk, provider_key("NEW"), expected_revision=99
            )

    assert key.value.messages != ["Enter an API key without spaces, up to 1024 characters."]
    assert recovery.value.messages != ["The account changed. Reload before confirming recovery."]
    assert replacement_refusal.value.messages != [STALE]


def test_a_new_account_with_a_stored_key_is_refused_next_to_the_key_field() -> None:
    system = solcast_system()
    client, _ = verified_admin()

    response = client.post(
        setup_url(system),
        setup_input(
            system,
            account_mode="new",
            existing_account="",
            external_account_id="another-owner",
            api_key=provider_key("CURRENT"),
            authorization_reference="Authorized service use",
        ),
    )

    assert response.status_code == 400
    assert "api_key" in response.context["setup_form"].errors
    assert "solar-owner" in response.content.decode()
    assert provider_key("CURRENT").encode() not in response.content
    assert not ProviderAccount.objects.filter(external_account_id="another-owner").exists()


def plant_on(system: System, name: str, latitude: float) -> SiteBinding:
    region = submit_region(
        system.member,
        system.tenant.pk,
        name=name,
        latitude=latitude,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    return configure_binding(
        system.admin,
        region.pk,
        provider_account_id=system.account.pk,
        site_id=name,
        capacity_kw=1,
        capacity_semantics_verified=True,
        installation_date=date(2020, 1, 1),
        settings_verification_reference="Verified site settings",
    )


def test_the_account_list_counts_the_plants_its_page_lists() -> None:
    system = solcast_system()
    decide_review(system.admin, system.region.pk, "suspend")
    plant_on(system, "pending-site", 41)
    now = timezone.now()

    row = next(row for row in account_rows(now) if row["account"] == system.account)

    listed = account_context(ProviderAccount.objects.get(pk=system.account.pk), now)["bindings"]
    assert row["plants"] == len(listed) == 2


def test_the_account_list_is_one_statement_however_many_attempts_and_plants() -> None:
    system = solcast_system()
    now = timezone.now()
    with CaptureQueriesContext(connection) as few:
        account_rows(now)
    spend(system.account, "forecast", "succeeded", 5)
    for index in range(3):
        plant_on(system, f"more-site-{index}", 41 + index)

    with CaptureQueriesContext(connection) as many:
        rows = account_rows(now)

    assert len(few.captured_queries) == len(many.captured_queries) == 1
    assert 'JOIN "forecasts_requestattempt"' not in many.captured_queries[0]["sql"]
    row = next(row for row in rows if row["account"] == system.account)
    assert (row["plants"], row["used_today"]) == (4, 6)
