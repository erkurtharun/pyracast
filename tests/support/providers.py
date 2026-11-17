from datetime import date

from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.secrets import encrypt_secret

TARGET = date(2026, 11, 10)


def binding_for(provider: str = "solcast") -> SiteBinding:
    reference = encrypt_secret("test-credential")
    return SiteBinding(
        id=7,
        region_id=8,
        region_revision=1,
        provider_account=ProviderAccount(
            provider=provider,
            encrypted_api_key=reference,
        ),
        site_id="site-1",
        capacity_kw=2,
        timezone="UTC",
        capacity_semantics_verified=True,
    )


def payload_for(power: object = 1.2) -> dict[str, object]:
    return {
        "forecasts": [
            {"period_end": "2026-11-10T02:30:00+02:00", "period": "PT30M", "pv_estimate": power}
        ]
    }
