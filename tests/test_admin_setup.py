import re
from datetime import date, timedelta

import httpx
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import rotate_secret
from pyracast.connections.secrets import decrypt_api_key, encrypt_secret
from pyracast.connections.setup import SetupOutcome, save_provider_setup
from pyracast.forecasts.models import CollectionJob, RequestAttempt
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.platform_admin.forms import BindingActionForm, ProviderSetupForm
from pyracast.provider_retry import retryable_status
from pyracast.providers import SolcastProvider
from pyracast.regions.models import AuditEvent, Region
from tests.support.admin_setup import (
    VALIDATION_FAILURES,
    admin_system,
    review_fields,
    setup_input,
    setup_url,
    submitted_plant,
)
from tests.support.api import verified_admin
from tests.support.domain import NOW, FakeAdapter, System, make_system

pytestmark = pytest.mark.django_db(transaction=True)


def action_input(system: System, action: str, **changes: object) -> dict[str, object]:
    return {
        "action": action,
        "expected_revision": system.region.revision,
        "binding_id": system.binding.pk,
        "credential_revision": system.account.credential_revision,
        **review_fields(system),
        **changes,
    }


def save_setup(form: ProviderSetupForm, system: System) -> SetupOutcome:
    return save_provider_setup(
        system.admin,
        system.region.pk,
        expected_revision=form.cleaned_data["expected_revision"],
        expected_binding_id=form.cleaned_data["expected_binding_id"],
        account=form.account(),
        binding=form.binding(),
    )


def visible_text(html: str) -> str:
    return re.sub(r"<[^>]*>", "", html)


def test_admin_can_review_configure_validate_and_approve_without_exposing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = admin_system()
    plant = submitted_plant(system)
    client, _ = verified_admin()
    original_account_id = system.account.pk
    original_ciphertext = system.account.encrypted_api_key
    response = client.get(setup_url(system))
    assert response.status_code == 200
    assert b"Customer solar park" in response.content
    assert b"4.2" in response.content
    assert b"Solcast setup for this plant" in response.content
    assert b"shared key and quota" in response.content
    assert b"every plant using the selected account" in response.content
    assert system.account.encrypted_api_key.encode() not in response.content
    listing = client.get("/admin/regions/region/")
    assert setup_url(system).encode() in listing.content
    detail = client.get(f"/admin/regions/region/{system.region.pk}/change/")
    assert b"Review and set up provider" in detail.content
    key = "test-only-provider-key"
    response = client.post(
        setup_url(system),
        setup_input(
            system,
            account_mode="new",
            existing_account="",
            external_account_id="New-owner",
            api_key=key,
            authorization_reference="Authorized service use",
        ),
    )
    assert response.status_code == 302
    assert not RequestAttempt.objects.exists()
    system.region.refresh_from_db()
    system.binding = SiteBinding.objects.get(
        region=system.region, region_revision=system.region.revision
    )
    system.account = system.binding.provider_account
    assert system.account.provider == "solcast"
    assert system.account.external_account_id == "new-owner"
    assert system.account.encrypted_api_key.startswith("encrypted:v1:")
    assert key not in system.account.encrypted_api_key
    assert (
        ProviderAccount.objects.get(pk=original_account_id).encrypted_api_key == original_ciphertext
    )
    page = client.get(setup_url(system))
    assert key.encode() not in page.content
    assert system.account.encrypted_api_key.encode() not in page.content
    assert page.context["can_approve"] is False
    assert client.post(setup_url(system), action_input(system, "approve")).status_code == 400
    monkeypatch.setattr(
        "pyracast.platform_admin.plant_review.adapter_for", lambda binding: FakeAdapter()
    )
    assert client.post(setup_url(system), action_input(system, "validate")).status_code == 302
    assert RequestAttempt.objects.get().outcome == "succeeded"
    assert client.get(setup_url(system)).context["can_approve"] is True
    assert client.post(setup_url(system), action_input(system, "approve")).status_code == 302
    system.region.refresh_from_db()
    plant.refresh_from_db()
    assert system.region.status == "approved"
    assert plant.current_configuration is not None
    assert plant.current_configuration.dc_capacity_mw == 4.2
    assert CollectionJob.objects.filter(binding=system.binding, endpoint="forecasts").count() == 1
    assert AuditEvent.objects.filter(region=system.region, action="region_approved").count() == 1


@pytest.mark.parametrize("identity", ["anonymous", "member", "staff", "unverified_admin"])
def test_setup_requires_superuser_and_mfa(identity: str) -> None:
    system = admin_system()
    client = Client()
    if identity != "anonymous":
        user = User.objects.create_user(
            username=f"setup-{identity}",
            is_staff=identity in {"staff", "unverified_admin"},
            is_superuser=identity == "unverified_admin",
        )
        client.force_login(user)
    for method in (client.get, client.post):
        assert method(setup_url(system)).status_code == 302
    assert SiteBinding.objects.count() == 1


def test_setup_csrf_deleted_regions_and_missing_binding_states() -> None:
    system = admin_system()
    client, _ = verified_admin()
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.cookies = client.cookies
    assert csrf_client.post(setup_url(system), setup_input(system)).status_code == 403
    region = Region.objects.create(
        tenant=system.tenant, name="Empty", latitude=41, longitude=30, timezone="Europe/Istanbul"
    )
    response = client.get(reverse("admin:workspace_plant", args=(region.pk,)))
    assert response.status_code == 200
    assert b"No plant has been submitted" in response.content
    assert (
        b'<span class="step-state">To do</span><span class="step-name">Plant submitted</span>'
        in response.content
    )
    assert b"Save the private connection to continue" in response.content
    Plant.objects.create(region=region, name="Legacy plant")
    response = client.get(reverse("admin:workspace_plant", args=(region.pk,)))
    assert b"No saved technical configuration or customer submission" in response.content
    system.region.deleted_at = timezone.now()
    system.region.save(update_fields=("deleted_at",))
    assert client.get(setup_url(system)).status_code == 404


def test_provider_setup_requires_a_private_key_and_offers_no_environment_reference() -> None:
    system = admin_system()
    fixture_account = ProviderAccount.objects.create(
        provider="fixture", external_account_id="fixture-only-account"
    )
    client, _ = verified_admin()
    page = client.get(setup_url(system))
    assert page.status_code == 200
    assert b'name="secret_reference"' not in page.content
    assert b"fixture-only-account" not in page.content
    forged = client.post(
        setup_url(system),
        setup_input(
            system, existing_account=f"{fixture_account.pk}:{fixture_account.credential_revision}"
        ),
    )
    assert forged.status_code == 400
    assert "existing_account" in forged.context["setup_form"].errors
    obsolete_reference = "env:SHOULD_NOT_BE_ACCEPTED"
    response = client.post(
        setup_url(system),
        setup_input(
            system,
            account_mode="new",
            existing_account="",
            external_account_id="new-account",
            authorization_reference="Authorized service use",
            secret_reference=obsolete_reference,
            api_key="",
        ),
    )
    assert response.status_code == 400
    assert "api_key" in response.context["setup_form"].errors


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        (
            {"account_mode": "new", "external_account_id": "", "authorization_reference": ""},
            "api_key",
        ),
        ({"existing_account": ""}, "existing_account"),
        ({"api_key": "never-render-this"}, "confirm_key_rotation"),
        ({"capacity_kw": "nan"}, "capacity_kw"),
        ({"capacity_semantics_verified": ""}, "capacity_semantics_verified"),
        ({"account_mode": "invalid"}, "account_mode"),
    ],
)
def test_invalid_setup_preserves_safe_fields_and_never_renders_secrets(
    changes: dict[str, object],
    field: str,
) -> None:
    system = admin_system()
    client, _ = verified_admin()
    response = client.post(setup_url(system), setup_input(system, **changes))
    assert response.status_code == 400
    assert field in response.context["setup_form"].errors
    assert b"never-render-this" not in response.content
    assert ProviderAccount.objects.count() == SiteBinding.objects.count() == 1


def test_existing_key_can_be_rotated_and_invalid_site_rolls_back_rotation() -> None:
    system = admin_system()
    client, _ = verified_admin()
    response = client.post(
        setup_url(system), setup_input(system, api_key="replacement-key", confirm_key_rotation=True)
    )
    assert response.status_code == 302
    system.account.refresh_from_db()
    system.region.refresh_from_db()
    system.binding = SiteBinding.objects.get(
        region=system.region, region_revision=system.region.revision
    )
    assert system.account.credential_revision == 2
    assert decrypt_api_key(system.account.encrypted_api_key) == "replacement-key"
    assert client.post(setup_url(system), setup_input(system)).status_code == 302
    system.region.refresh_from_db()
    system.account.refresh_from_db()
    system.binding = SiteBinding.objects.get(
        region=system.region, region_revision=system.region.revision
    )
    assert system.account.credential_revision == 2
    assert (
        client.post(
            setup_url(system),
            setup_input(system, api_key="rollback-key", confirm_key_rotation=True, capacity_kw=0),
        ).status_code
        == 400
    )
    system.account.refresh_from_db()
    assert decrypt_api_key(system.account.encrypted_api_key) == "replacement-key"
    assert SiteBinding.objects.count() == 2


def test_stale_setup_and_action_revisions_are_rejected() -> None:
    system = admin_system()
    client, _ = verified_admin()
    assert (
        client.post(setup_url(system), setup_input(system, expected_revision=999)).status_code
        == 400
    )
    assert (
        client.post(
            setup_url(system), action_input(system, "validate", expected_revision=999)
        ).status_code
        == 400
    )
    assert (
        client.post(
            setup_url(system), action_input(system, "validate", credential_revision=999)
        ).status_code
        == 400
    )
    assert client.post(setup_url(system), action_input(system, "bogus")).status_code == 400
    assert client.post(setup_url(system), {"action": "validate"}).status_code == 400
    assert not RequestAttempt.objects.exists()
    form = ProviderSetupForm(setup_input(system))
    assert form.is_valid(), form.errors
    rotate_secret(system.admin, system.account.pk, "rotated-key")
    with pytest.raises(ValidationError, match="account changed"):
        save_setup(form, system)
    action = BindingActionForm(action_input(system, "validate"))
    assert action.is_valid()
    stale = Region.objects.get(pk=system.region.pk)
    stale.revision += 1
    with pytest.raises(ValidationError, match="setup changed"):
        action.current_binding(stale)


def test_new_account_duplicate_identity_is_not_silently_reused() -> None:
    system = admin_system()
    client, _ = verified_admin()
    system.account.provider = "solcast"
    system.account.save(update_fields=("provider",))
    original_key = system.account.encrypted_api_key
    response = client.post(
        setup_url(system),
        setup_input(
            system,
            account_mode="new",
            external_account_id=system.account.external_account_id,
            api_key="new-key",
            authorization_reference="Authorized",
        ),
    )
    assert response.status_code == 400
    assert ProviderAccount.objects.count() == 1
    system.account.refresh_from_db()
    assert system.account.encrypted_api_key == original_key


def test_setup_rechecks_selected_account_provider_under_lock() -> None:
    system = admin_system()
    form = ProviderSetupForm(setup_input(system))
    assert form.is_valid(), form.errors
    system.account.provider = "fixture"
    system.account.save(update_fields=("provider",))
    with pytest.raises(ProviderAccount.DoesNotExist):
        save_setup(form, system)
    assert SiteBinding.objects.count() == 1


def test_invalid_new_account_key_is_rejected_without_being_rendered() -> None:
    system = admin_system()
    client, _ = verified_admin()
    invalid_key = "invalid key with spaces"
    response = client.post(
        setup_url(system),
        setup_input(
            system,
            account_mode="new",
            existing_account="",
            external_account_id="new-account",
            authorization_reference="Authorized service use",
            api_key=invalid_key,
        ),
    )
    assert response.status_code == 400
    assert invalid_key.encode() not in response.content
    assert ProviderAccount.objects.count() == 1


def test_validation_failures_and_quota_exhaustion_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = admin_system()
    client, _ = verified_admin()

    def fail() -> None:
        assert not connection.in_atomic_block
        raise ProviderError("secret-must-not-be-rendered")

    monkeypatch.setattr(
        "pyracast.platform_admin.plant_review.adapter_for", lambda binding: FakeAdapter(fail)
    )
    response = client.post(setup_url(system), action_input(system, "validate"))
    assert response.status_code == 400
    assert b"Connection validation failed" in response.content
    assert b"secret-must-not-be-rendered" not in response.content
    assert RequestAttempt.objects.get().outcome == "failed"
    system.account.daily_quota = 1
    system.account.save(update_fields=("daily_quota",))
    response = client.post(setup_url(system), action_input(system, "validate"))
    assert response.status_code == 400
    assert b"request budget is unavailable" in response.content
    assert RequestAttempt.objects.count() == 1


@pytest.mark.parametrize(("code", "status", "message"), VALIDATION_FAILURES)
def test_failed_validation_shows_its_cause_once_and_logs_one_line(
    monkeypatch: pytest.MonkeyPatch,
    pyracast_logs: pytest.LogCaptureFixture,
    code: str,
    status: int | None,
    message: str,
) -> None:
    system = admin_system()
    client, _ = verified_admin()

    retryable = status is None or retryable_status(status)

    def fail() -> None:
        raise ProviderError(code, retryable=retryable, status_code=status)

    monkeypatch.setattr(
        "pyracast.platform_admin.plant_review.adapter_for", lambda binding: FakeAdapter(fail)
    )
    response = client.post(setup_url(system), action_input(system, "validate"))

    assert response.status_code == 400
    html = response.content.decode()
    assert html.count(escape(message)) == 1
    assert "__all__" not in html
    assert [
        record.getMessage()
        for record in pyracast_logs.records
        if record.name == "pyracast.forecasts.validation"
    ] == [
        f"provider_validation_failed provider=solcast binding_id={system.binding.pk} "
        f"error_code={code} status_code={status}"
    ]
    assert RequestAttempt.objects.get().outcome == "failed"
    system.binding.refresh_from_db()
    system.account.refresh_from_db()
    transient = code == "provider_request_failed" and retryable
    assert system.binding.health == ("unknown" if transient else "error")
    assert (system.account.authentication_failed_revision is not None) is (status == 401)
    assert (system.account.request_blocked_until is not None) is (status == 429)


def test_a_site_the_key_cannot_see_is_reported_as_such_without_the_provider_body(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system = admin_system()
    client, _ = verified_admin()
    body = '{"response_status":{"error_code":"NotFound","message":"Location is not accessible"}}'

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text=body)

    monkeypatch.setattr(
        "pyracast.platform_admin.plant_review.adapter_for",
        lambda binding: SolcastProvider(httpx.MockTransport(handle)),
    )
    response = client.post(setup_url(system), action_input(system, "validate"))

    assert response.status_code == 400
    html = response.content.decode()
    assert "The provider found no site with this resource ID for this API key." in html
    assert "Location is not accessible" not in html + pyracast_logs.text
    assert "original-fixture-key" not in pyracast_logs.text
    assert f"binding_id={system.binding.pk} error_code=provider_request_failed status_code=404" in (
        pyracast_logs.text
    )


def test_stale_action_fields_ask_for_a_reload_without_naming_form_keys() -> None:
    system = admin_system()
    client, _ = verified_admin()
    url = setup_url(system)

    response = client.post(url, {"action": "validate"})

    assert response.status_code == 400
    html = response.content.decode()
    assert html.count(f'<li>This field is required. <a href="{url}">Reload this page</a></li>') == 3
    for key in ("__all__", "expected_revision", "binding_id", "credential_revision"):
        assert key not in visible_text(html)
    assert not RequestAttempt.objects.exists()


def test_a_missing_recovery_confirmation_is_named_by_its_label() -> None:
    system = admin_system()
    system.account.request_blocked_until = timezone.now() + timedelta(hours=1)
    system.account.save(update_fields=("request_blocked_until",))
    client, _ = verified_admin()

    response = client.post(setup_url(system), action_input(system, "clear_cooldown"))

    assert response.status_code == 400
    html = response.content.decode()
    assert (
        '<a href="#id_provider_recovered">The provider has confirmed that this account can '
        "make requests again: This field is required.</a>"
    ) in html
    assert "provider_recovered" not in visible_text(html)


@pytest.mark.parametrize("authentication_failure", [True, False])
def test_admin_connection_admission_explains_credential_or_rate_limit_action(
    monkeypatch: pytest.MonkeyPatch, authentication_failure: bool
) -> None:
    system = admin_system()
    client, _ = verified_admin()
    system.account.authorized_at = timezone.now()
    if authentication_failure:
        system.account.authentication_failed_revision = system.account.credential_revision
    else:
        system.account.request_blocked_until = timezone.now() + timedelta(hours=1)
    system.account.save()
    monkeypatch.setattr(
        "pyracast.platform_admin.plant_review.adapter_for", lambda binding: FakeAdapter()
    )
    response = client.post(setup_url(system), action_input(system, "validate"))
    assert response.status_code == 400
    assert (
        b"Update the private API key"
        if authentication_failure
        else b"provider is limiting requests"
    ) in response.content
    assert not RequestAttempt.objects.exists()
    assert system.account.encrypted_api_key.encode() not in response.content


def test_initial_setup_duplicate_post_does_not_replace_the_first_binding() -> None:
    system = admin_system()
    client, _ = verified_admin()
    region = Region.objects.create(
        tenant=system.tenant,
        name="New site",
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    path = reverse("admin:workspace_plant", args=(region.pk,))
    data = setup_input(system, expected_binding_id="")
    assert client.post(path, data).status_code == 302
    binding = SiteBinding.objects.get(region=region)
    assert client.post(path, data).status_code == 400
    region.refresh_from_db()
    assert region.revision == 1
    assert list(SiteBinding.objects.filter(region=region)) == [binding]


def approved_admin_system() -> tuple[System, list[int]]:
    from pyracast.plants.operations import create_plant

    system = make_system(approved=True)
    system.account.provider = "solcast"
    system.account.encrypted_api_key = encrypt_secret("original-fixture-key")
    system.account.save(update_fields=("provider", "encrypted_api_key"))
    system.binding.installation_date = date(2020, 1, 1)
    system.binding.settings_verification_reference = "Verified synthetic site settings"
    system.binding.save(update_fields=("installation_date", "settings_verification_reference"))
    create_plant(system.member, system.tenant.pk, system.region.pk, name="Plant", dc_capacity_mw=1)
    Plant.objects.update(
        automatic_calibration_enabled=True,
        automatic_calibration_enabled_by=system.member,
        next_calibration_check_at=NOW,
    )
    return system, list(PlantConfiguration.objects.order_by("pk").values_list("pk", flat=True))


def unchanged_setup_input(system: System, **changes: object) -> dict[str, object]:
    return setup_input(
        system,
        site_id="site",
        capacity_kw=1,
        settings_verification_reference="Verified synthetic site settings",
        **changes,
    )


def assert_setup_kept(system: System, configurations: list[int]) -> None:
    system.region.refresh_from_db()
    assert (system.region.revision, system.region.status, system.region.approved_binding_id) == (
        1,
        "approved",
        system.binding.pk,
    )
    assert list(SiteBinding.objects.values_list("pk", flat=True)) == [system.binding.pk]
    assert (
        list(PlantConfiguration.objects.order_by("pk").values_list("pk", flat=True))
        == configurations
    )
    plant = Plant.objects.get()
    assert (
        plant.current_configuration_id,
        plant.automatic_calibration_enabled,
        plant.automatic_calibration_enabled_by_id,
        plant.next_calibration_check_at,
    ) == (configurations[-1], True, system.member.pk, NOW)
    assert not AuditEvent.objects.filter(action="binding_configured", revision=2).exists()


def test_identical_setup_save_changes_nothing() -> None:
    system, configurations = approved_admin_system()
    client, _ = verified_admin()
    response = client.post(setup_url(system), unchanged_setup_input(system), follow=True)
    assert response.status_code == 200
    assert b"Nothing changed." in response.content
    assert_setup_kept(system, configurations)
    system.account.refresh_from_db()
    assert system.account.credential_revision == 1


def test_key_only_setup_save_rotates_key_and_keeps_approval() -> None:
    system, configurations = approved_admin_system()
    client, actor = verified_admin()
    response = client.post(
        setup_url(system),
        unchanged_setup_input(system, api_key="replacement-key", confirm_key_rotation=True),
        follow=True,
    )
    assert response.status_code == 200
    assert b"API key replaced. Validate the connection to resume collection." in response.content
    assert_setup_kept(system, configurations)
    system.account.refresh_from_db()
    assert system.account.credential_revision == 2
    assert decrypt_api_key(system.account.encrypted_api_key) == "replacement-key"
    rotated = AuditEvent.objects.get(action="provider_secret_rotated")
    assert (rotated.actor_id, rotated.provider_account_id) == (actor.pk, system.account.pk)
