import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal, cast

import httpx
import pytest

from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.secrets import encrypt_secret
from pyracast.providers import FixtureProvider, SolcastProvider, adapter_for, normalize_payload

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


def test_normalization_preserves_raw_missingness_units_bounds_and_collection() -> None:
    collected = datetime(2026, 11, 9, 16, tzinfo=UTC)
    raw = payload_for()
    raw["metadata"] = {"central_semantics": "unspecified"}
    result = normalize_payload(raw, collected_at=collected)
    assert result.raw_payload == raw
    assert result.raw_payload is not raw
    assert result.intervals == [
        {
            "start": "2026-11-10T00:00:00+00:00",
            "end": "2026-11-10T00:30:00+00:00",
            "power_kw": 1.2,
            "sunlight": "unknown",
        }
    ]
    assert result.issued_at is None
    assert result.collected_at == collected
    assert result.source_kind == "forecast"
    assert normalize_payload(payload_for(None)).intervals[0]["power_kw"] is None
    assert normalize_payload(payload_for(-1)).intervals[0]["power_kw"] == -1
    assert normalize_payload(payload_for(0)).intervals[0]["power_kw"] == 0


def test_actuals_are_distinct_from_archived_forecasts() -> None:
    raw: dict[str, object] = {"estimated_actuals": []}
    result = normalize_payload(raw, "estimated_actuals")
    assert result.source_kind == "estimated_actuals"
    assert result.collected_at is not None
    assert result.collected_at.utcoffset() is not None
    with pytest.raises(ProviderError, match="endpoint"):
        normalize_payload(raw, cast(Literal["forecasts", "estimated_actuals"], "wrong"))


def test_captured_seven_day_actuals_preserves_source_and_interval_bounds() -> None:
    fixture = Path(__file__).parent / "fixtures" / "solcast_estimated_actuals.json"
    metadata = json.loads(fixture.with_suffix(".metadata.json").read_text(encoding="utf-8"))
    collected_at = datetime.fromisoformat(metadata["collected_at"])
    data = normalize_payload(
        json.loads(fixture.read_text(encoding="utf-8")),
        "estimated_actuals",
        collected_at=collected_at,
    )
    assert data.source_kind == "estimated_actuals"
    assert data.issued_at is None
    assert len(data.intervals) == 337
    assert min(cast(str, row["start"]) for row in data.intervals) == "2026-11-03T01:00:00+00:00"
    assert max(cast(str, row["end"]) for row in data.intervals) == "2026-11-10T01:30:00+00:00"


@pytest.mark.parametrize("payload", [None, [], {}, {"forecasts": {}}, {"forecasts": None}])
def test_invalid_top_level_schemas_are_rejected(payload: object) -> None:
    with pytest.raises(ProviderError, match="schema"):
        normalize_payload(payload)


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        {"period": "PT15M"},
        {"period": "PT30M"},
        {"period": "PT30M", "period_end": None},
        {"period": "PT30M", "period_end": "invalid"},
        {"period": "PT30M", "period_end": "2026-11-10T00:30:00"},
        {"period": "PT30M", "period_end": "2026-11-10T00:30:00Z", "pv_estimate": True},
        {"period": "PT30M", "period_end": "2026-11-10T00:30:00Z", "pv_estimate": "1.2"},
        {"period": "PT30M", "period_end": "2026-11-10T00:30:00Z", "pv_estimate": float("nan")},
        {"period": "PT30M", "period_end": "2026-11-10T00:30:00Z", "pv_estimate": float("inf")},
        {"period": "PT30M", "period_end": "2026-11-10T00:30:00Z", "pv_estimate": 10**1000},
    ],
)
def test_invalid_rows_are_rejected(row: object) -> None:
    with pytest.raises(ProviderError, match="interval"):
        normalize_payload({"forecasts": [row]})


@pytest.mark.parametrize("metadata", [object(), float("nan")])
def test_non_json_metadata_is_rejected(metadata: object) -> None:
    with pytest.raises(ProviderError, match="schema"):
        normalize_payload({"forecasts": [], "metadata": metadata})


def test_fixture_is_explicitly_synthetic_and_supports_dst_days() -> None:
    binding = binding_for("fixture")
    binding.timezone = "America/New_York"
    provider = FixtureProvider()
    result = provider.fetch(binding, date(2026, 11, 1))
    assert len(result.intervals) == 50
    assert result.raw_payload["synthetic"] is True
    assert {row["sunlight"] for row in result.intervals} == {"day", "night"}
    assert {row["power_kw"] for row in result.intervals} == {0, 1.2}
    with pytest.raises(ProviderError, match="fixture_account_required"):
        provider.validate(binding_for())


def test_adapter_selection() -> None:
    assert isinstance(adapter_for(binding_for("fixture")), FixtureProvider)
    assert isinstance(adapter_for(binding_for()), SolcastProvider)
    with pytest.raises(ProviderError, match="unsupported_provider"):
        adapter_for(binding_for("unknown"))


def test_request_uses_fixed_https_origin_header_auth_and_bounded_timeout() -> None:
    binding = binding_for()

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        assert request.url.host == "api.solcast.com.au"
        assert request.url.path == "/rooftop_sites/site-1/forecasts"
        assert request.headers["Authorization"] == "Bearer test-credential"
        assert "test-credential" not in str(request.url)
        assert dict(request.url.params) == {"format": "json", "hours": "72"}
        assert request.extensions["timeout"] == {
            "connect": 5,
            "read": 20,
            "write": 20,
            "pool": 20,
        }
        return httpx.Response(200, json=payload_for())

    provider = SolcastProvider(httpx.MockTransport(handle))
    assert provider.fetch(binding, TARGET).issued_at is None
    provider.validate(binding)


@pytest.mark.parametrize(
    ("reference", "site", "code"),
    [
        ("literal-secret", "site-1", "invalid_provider_secret"),
        ("env:SOLCAST_API_KEY", "site-1", "invalid_provider_secret"),
        ("encrypted:v1:invalid", "site-1", "invalid_provider_secret"),
        ("", "site-1", "invalid_provider_secret"),
        (None, "../site", "invalid_site_id"),
    ],
)
def test_credentials_and_site_ids_are_validated_before_transport(
    reference: str | None, site: str, code: str
) -> None:
    binding = binding_for()
    if reference is not None:
        binding.provider_account.encrypted_api_key = reference
    binding.site_id = site
    with pytest.raises(ProviderError, match=code):
        SolcastProvider().fetch(binding, TARGET)


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_http_errors_never_expose_provider_body_or_follow_redirects(status: int) -> None:

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.solcast.com.au"
        return httpx.Response(
            status, text="test-credential", headers={"Location": "https://evil.test"}
        )

    with pytest.raises(ProviderError, match=r"^provider_request_failed$") as caught:
        SolcastProvider(httpx.MockTransport(handle)).fetch(binding_for(), TARGET)
    assert "test-credential" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_timeouts_are_redacted() -> None:

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test-credential", request=request)

    with pytest.raises(ProviderError, match=r"^provider_request_failed$"):
        SolcastProvider(httpx.MockTransport(handle)).fetch(binding_for(), TARGET)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"forecasts":[],"error":"test-credential"}',
        b'{"forecasts":[],"nested":[{"error":"test-\\u0063redential"}]}',
        b'{"forecasts":[],"test-\\u0063redential":"hidden"}',
    ],
)
def test_malformed_or_secret_echoing_success_bodies_are_rejected(body: bytes) -> None:

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with pytest.raises(ProviderError, match=r"provider_request_failed|unsafe_provider_response"):
        SolcastProvider(httpx.MockTransport(handle)).fetch(binding_for(), TARGET)
