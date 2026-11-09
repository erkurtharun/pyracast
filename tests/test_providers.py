from datetime import UTC, date, datetime
from typing import Literal, cast

import httpx
import pytest

from pyracast.connections.contracts import ProviderFailure
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.providers import FixtureProvider, SolcastProvider, adapter_for, normalize_payload

TARGET = date(2026, 11, 10)


def binding_for(provider: str = "solcast") -> SiteBinding:
    reference = "env:PYRACAST_SOLCAST_TEST"
    return SiteBinding(
        id=7,
        region_id=8,
        region_revision=1,
        provider_account=ProviderAccount(
            provider=provider,
            secret_reference=reference,
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
    with pytest.raises(ProviderFailure, match="endpoint"):
        normalize_payload(raw, cast(Literal["forecasts", "estimated_actuals"], "wrong"))


@pytest.mark.parametrize("payload", [None, [], {}, {"forecasts": {}}, {"forecasts": None}])
def test_invalid_top_level_schemas_are_rejected(payload: object) -> None:
    with pytest.raises(ProviderFailure, match="schema"):
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
    with pytest.raises(ProviderFailure, match="interval"):
        normalize_payload({"forecasts": [row]})


@pytest.mark.parametrize("metadata", [object(), float("nan")])
def test_non_json_metadata_is_rejected(metadata: object) -> None:
    with pytest.raises(ProviderFailure, match="schema"):
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
    with pytest.raises(ProviderFailure, match="fixture_account_required"):
        provider.validate(binding_for())


def test_adapter_selection() -> None:
    assert isinstance(adapter_for(binding_for("fixture")), FixtureProvider)
    assert isinstance(adapter_for(binding_for()), SolcastProvider)
    with pytest.raises(ProviderFailure, match="unsupported_provider"):
        adapter_for(binding_for("unknown"))


@pytest.mark.parametrize("variable", ["PYRACAST_SOLCAST_TEST", "SOLCAST_API_KEY"])
def test_request_uses_fixed_https_origin_header_auth_and_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    monkeypatch.setenv(variable, "test-credential")
    binding = binding_for()
    binding.provider_account.secret_reference = f"env:{variable}"

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
        ("literal-secret", "site-1", "invalid_secret_reference"),
        ("env:HOME", "site-1", "invalid_secret_reference"),
        ("env:SOLCAST_API_KEY_OTHER", "site-1", "invalid_secret_reference"),
        ("env:PYRACAST_SOLCAST_lowercase", "site-1", "invalid_secret_reference"),
        ("env:PYRACAST_SOLCAST_", "site-1", "invalid_secret_reference"),
        ("env:PYRACAST_SOLCAST_ABSENT", "site-1", "missing_provider_secret"),
        ("env:SOLCAST_API_KEY", "site-1", "missing_provider_secret"),
        ("env:PYRACAST_SOLCAST_TEST", "../site", "invalid_site_id"),
    ],
)
def test_credentials_and_site_ids_are_validated_before_transport(
    monkeypatch: pytest.MonkeyPatch, reference: str, site: str, code: str
) -> None:
    monkeypatch.setenv("PYRACAST_SOLCAST_TEST", "test-credential")
    monkeypatch.delenv("PYRACAST_SOLCAST_ABSENT", raising=False)
    monkeypatch.delenv("SOLCAST_API_KEY", raising=False)
    binding = binding_for()
    binding.provider_account.secret_reference = reference
    binding.site_id = site
    with pytest.raises(ProviderFailure, match=code):
        SolcastProvider().fetch(binding, TARGET)


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_http_errors_never_expose_provider_body_or_follow_redirects(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("PYRACAST_SOLCAST_TEST", "test-credential")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.solcast.com.au"
        return httpx.Response(
            status, text="test-credential", headers={"Location": "https://evil.test"}
        )

    with pytest.raises(ProviderFailure, match=r"^provider_request_failed$") as caught:
        SolcastProvider(httpx.MockTransport(handle)).fetch(binding_for(), TARGET)
    assert "test-credential" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_timeouts_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYRACAST_SOLCAST_TEST", "test-credential")

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test-credential", request=request)

    with pytest.raises(ProviderFailure, match=r"^provider_request_failed$"):
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
def test_malformed_or_secret_echoing_success_bodies_are_rejected(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    monkeypatch.setenv("PYRACAST_SOLCAST_TEST", "test-credential")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with pytest.raises(ProviderFailure, match=r"provider_request_failed|unsafe_provider_response"):
        SolcastProvider(httpx.MockTransport(handle)).fetch(binding_for(), TARGET)
