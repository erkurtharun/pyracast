from datetime import date

from django.urls import reverse

from pyracast.connections.secrets import encrypt_secret
from pyracast.plants.models import Plant, PlantSubmission
from tests.support.domain import System, make_system

REJECTED_KEY = (
    "The provider rejected the API key. Enter the current key for this account, "
    "then validate again."
)
UNREADABLE_ANSWER = "The provider's answer could not be read. Try validating again later."
UNSUPPORTED_ACCOUNT = "This account cannot be validated here. Select a Solcast account."
REFUSED_REQUEST = (
    "The provider refused the request. Check the resource ID and site settings, "
    "then validate again."
)
PROVIDER_ANSWERED_WITH_ERROR = "The provider answered with an error. Try validating again later."
VALIDATION_FAILURES: tuple[tuple[str, int | None, str], ...] = (
    ("provider_request_failed", 401, REJECTED_KEY),
    ("provider_request_failed", 403, REJECTED_KEY),
    (
        "provider_request_failed",
        404,
        "The provider found no site with this resource ID for this API key. Check the "
        "resource ID and that the key belongs to the account that owns the site.",
    ),
    (
        "provider_request_failed",
        429,
        "The provider is limiting requests. Wait before validating again.",
    ),
    (
        "provider_request_failed",
        None,
        "The provider could not be reached. Try validating again in a few minutes.",
    ),
    ("provider_request_failed", 500, PROVIDER_ANSWERED_WITH_ERROR),
    ("provider_request_failed", 503, PROVIDER_ANSWERED_WITH_ERROR),
    ("provider_request_failed", 400, REFUSED_REQUEST),
    ("provider_request_failed", 302, REFUSED_REQUEST),
    (
        "invalid_site_id",
        None,
        "The resource ID contains characters the provider does not use. Use only letters, "
        "digits, hyphens and underscores.",
    ),
    (
        "invalid_provider_secret",
        None,
        "The saved API key cannot be read. Enter the key again, then validate.",
    ),
    ("invalid_provider_schema", None, UNREADABLE_ANSWER),
    ("invalid_provider_interval", None, UNREADABLE_ANSWER),
    ("invalid_provider_endpoint", None, UNREADABLE_ANSWER),
    ("unsafe_provider_response", None, UNREADABLE_ANSWER),
    ("provider_response_too_large", None, UNREADABLE_ANSWER),
    ("unsupported_provider", None, UNSUPPORTED_ACCOUNT),
    ("fixture_account_required", None, UNSUPPORTED_ACCOUNT),
)


def admin_system() -> System:
    system = make_system()
    system.account.provider = "solcast"
    system.account.encrypted_api_key = encrypt_secret("original-fixture-key")
    system.account.save(update_fields=("provider", "encrypted_api_key"))
    system.binding.installation_date = date(2020, 1, 1)
    system.binding.settings_verification_reference = "Verified synthetic site settings"
    system.binding.save(update_fields=("installation_date", "settings_verification_reference"))
    return system


def setup_url(system: System) -> str:
    return reverse("admin:workspace_plant", args=(system.region.pk,))


def setup_input(system: System, **changes: object) -> dict[str, object]:
    return {
        "action": "configure",
        "expected_revision": system.region.revision,
        "expected_binding_id": system.binding.pk,
        "account_mode": "existing",
        "existing_account": f"{system.account.pk}:{system.account.credential_revision}",
        "api_key": "",
        "daily_quota": 10,
        "site_id": "solar-resource",
        "capacity_kw": 1000,
        "dc_capacity_kw": "",
        "power_basis": "ac",
        "installation_date": "2020-01-01",
        "azimuth_degrees": 0,
        "losses_percent": 0,
        "settings_verification_reference": "Checked site settings with authorized account owner",
        "capacity_semantics_verified": True,
        **changes,
    }


def submitted_plant(system: System) -> Plant:
    plant = Plant.objects.create(region=system.region, name="Customer solar park")
    PlantSubmission.objects.create(
        plant=plant,
        dc_capacity_mw=4.2,
        submitted_by=system.member,
        inverter_capacity_mw=4,
        export_limit_mw=3.8,
    )
    return plant
