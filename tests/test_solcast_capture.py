import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import django
import httpx
import pytest
from django.db import connections
from django.utils import timezone

from pyracast.accounts.models import Tenant
from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import QuotaExceededError, reserve_account_request
from pyracast.connections.secrets import encrypt_secret
from pyracast.forecasts.models import RequestAttempt
from pyracast.providers import normalize_payload
from pyracast.regions.models import Region
from scripts import check_solcast


def make_binding(account: ProviderAccount, site_id: str = "synthetic-site") -> SiteBinding:
    region = Region.objects.create(
        tenant=Tenant.objects.create(name=f"Capture tenant {account.pk}/{site_id}"),
        name="Capture site",
        latitude=37.8,
        longitude=34.46,
        timezone="Europe/Istanbul",
    )
    return SiteBinding.objects.create(
        region=region,
        region_revision=region.revision,
        provider_account=account,
        site_id=site_id,
        capacity_kw=1000,
        latitude=region.latitude,
        longitude=region.longitude,
        timezone=region.timezone,
    )


@pytest.fixture
def capture_binding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SiteBinding:
    monkeypatch.chdir(tmp_path)
    account = ProviderAccount.objects.create(
        provider="solcast",
        external_account_id="authorized-test-account",
        encrypted_api_key=encrypt_secret("synthetic-capture-credential"),
        authorized_at=timezone.now(),
        daily_quota=3,
    )
    return make_binding(account)


def forecast_payload() -> dict[str, object]:
    return {
        "forecasts": [
            {
                "period_end": "2026-11-10T00:30:00Z",
                "period": "PT30M",
                "pv_estimate": 0.3,
                "pv_estimate10": 0.2,
                "pv_estimate90": 0.4,
                "resource_id": "synthetic-site",
                "account_id": "private-account",
            }
        ],
        "account_id": "private-account",
    }


@pytest.mark.django_db
@pytest.mark.parametrize("absolute", [True, False])
def test_capture_rejects_output_outside_cwd_before_creating_files_or_consuming_quota(
    capture_binding: SiteBinding,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    absolute: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    outside = tmp_path / "outside"
    output = outside if absolute else Path("../outside")

    def reject_request(request: httpx.Request) -> httpx.Response:
        pytest.fail("An out-of-workspace output must be rejected before provider access")

    with pytest.raises(ValueError, match="current working directory"):
        check_solcast.capture(
            capture_binding, "forecast", output, transport=httpx.MockTransport(reject_request)
        )

    assert not outside.exists()
    assert not RequestAttempt.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("form", ["absolute", "relative", "parent_inside"])
def test_capture_accepts_canonical_output_inside_cwd(
    capture_binding: SiteBinding, tmp_path: Path, form: str
) -> None:
    output = tmp_path / "captures"
    if form == "relative":
        output = Path("captures")
    elif form == "parent_inside":
        output = Path("nested/../captures")
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=forecast_payload()))

    target = check_solcast.capture(capture_binding, "forecast", output, transport=transport)

    assert target.parent == tmp_path / "captures"
    assert target.is_file()
    assert target.with_suffix(".metadata.json").is_file()
    assert RequestAttempt.objects.get().outcome == "succeeded"


@pytest.mark.django_db
def test_capture_rejects_directory_symlink_escaping_cwd_before_consuming_quota(
    capture_binding: SiteBinding, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "linked-output"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows directory symlink creation requires Developer Mode or privilege")
        raise
    monkeypatch.chdir(workspace)

    def reject_request(request: httpx.Request) -> httpx.Response:
        pytest.fail("An escaping output symlink must be rejected before provider access")

    with pytest.raises(ValueError, match="current working directory"):
        check_solcast.capture(
            capture_binding, "forecast", link, transport=httpx.MockTransport(reject_request)
        )

    assert list(outside.iterdir()) == []
    assert not RequestAttempt.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_capture_preserves_other_account_artifact(
    capture_binding: SiteBinding, tmp_path: Path
) -> None:
    authorizations: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers["Authorization"])
        return httpx.Response(200, json=forecast_payload())

    transport = httpx.MockTransport(handle)
    first = check_solcast.capture(capture_binding, "forecast", tmp_path, transport=transport)
    other = ProviderAccount.objects.create(
        provider="solcast",
        external_account_id="other-authorized-test-account",
        encrypted_api_key=encrypt_secret("other-capture-credential"),
        authorized_at=timezone.now(),
    )
    second = check_solcast.capture(make_binding(other), "forecast", tmp_path, transport=transport)
    assert first != second
    assert first.is_file()
    assert second.is_file()
    assert first.read_text() == second.read_text()
    assert authorizations == [
        "Bearer synthetic-capture-credential",
        "Bearer other-capture-credential",
    ]


@pytest.mark.django_db(transaction=True)
def test_capture_uses_current_database_key_and_site_ignoring_environment(
    capture_binding: SiteBinding, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOLCAST_API_KEY", "unrelated-environment-key")
    monkeypatch.setenv("SOLCAST_RESOURCE_ID", "unrelated-environment-site")
    ProviderAccount.objects.filter(pk=capture_binding.provider_account_id).update(
        encrypted_api_key=encrypt_secret("rotated-database-key"), credential_revision=2
    )

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer rotated-database-key"
        assert request.url.path == "/rooftop_sites/synthetic-site/forecasts"
        return httpx.Response(200, json=forecast_payload())

    check_solcast.capture(
        capture_binding, "forecast", tmp_path, transport=httpx.MockTransport(handle)
    )
    assert RequestAttempt.objects.get().binding_id == capture_binding.pk


@pytest.mark.django_db
def test_capture_allows_each_binding_within_the_shared_account_budget(
    capture_binding: SiteBinding, tmp_path: Path
) -> None:
    other = make_binding(capture_binding.provider_account, "other-resource")
    requested_paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(200, json=forecast_payload())

    transport = httpx.MockTransport(handle)
    check_solcast.capture(capture_binding, "forecast", tmp_path, transport=transport)
    check_solcast.capture(other, "forecast", tmp_path, transport=transport)

    assert requested_paths == [
        "/rooftop_sites/synthetic-site/forecasts",
        "/rooftop_sites/other-resource/forecasts",
    ]
    assert (
        RequestAttempt.objects.filter(provider_account=capture_binding.provider_account).count()
        == 2
    )
    with pytest.raises(ValueError, match="already attempted today"):
        check_solcast.capture(other, "forecast", tmp_path, transport=transport)


@pytest.mark.django_db
def test_capture_rejects_a_superseded_binding_before_consuming_quota(
    capture_binding: SiteBinding, tmp_path: Path
) -> None:
    Region.objects.filter(pk=capture_binding.region_id).update(revision=2)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=forecast_payload()))

    with pytest.raises(SiteBinding.DoesNotExist):
        check_solcast.capture(capture_binding, "forecast", tmp_path, transport=transport)

    assert not RequestAttempt.objects.exists()


def test_capture_cli_requires_explicit_live_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["check_solcast.py", "--binding-id", "1", "--kind", "settings"]
    )

    def reject_setup() -> None:
        pytest.fail("Django and provider setup must not run without --live")

    monkeypatch.setattr(django, "setup", reject_setup)
    with pytest.raises(SystemExit) as caught:
        check_solcast.main()

    assert caught.value.code == 2
    assert "Live requests require --live." in capsys.readouterr().err


def test_settings_sanitization_selects_exact_site_and_allowlisted_fields() -> None:
    payload = {
        "sites": [
            {"resource_id": "other-site", "capacity": 99},
            {
                "resource_id": "synthetic-site",
                "capacity": 1.5,
                "latitude": 39.9,
                "longitude": 32.8,
                "tilt": 0,
                "install_date": "2024-01-01",
                "account_id": "private-account",
                "name": "private-name",
            },
        ],
        "account_id": "private-account",
    }

    assert check_solcast.sanitize(payload, "settings", "synthetic-site") == {
        "capacity": 1.5,
        "latitude": 39.9,
        "longitude": 32.8,
        "tilt": 0,
        "install_date": "2024-01-01",
    }


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"sites": {}},
        {"sites": []},
        {"sites": [{"resource_id": "other"}]},
        {"sites": [{"resource_id": "synthetic-site"}, {"resource_id": "synthetic-site"}]},
    ],
)
def test_settings_sanitization_rejects_invalid_or_ambiguous_sites(payload: object) -> None:
    with pytest.raises(ProviderError):
        check_solcast.sanitize(payload, "settings", "synthetic-site")


def test_forecast_sanitization_omits_unknown_fields() -> None:
    assert check_solcast.sanitize(forecast_payload(), "forecast", "synthetic-site") == {
        "forecasts": [
            {
                "period_end": "2026-11-10T00:30:00Z",
                "period": "PT30M",
                "pv_estimate": 0.3,
                "pv_estimate10": 0.2,
                "pv_estimate90": 0.4,
            }
        ]
    }


def test_captured_forecast_fixture_preserves_documented_intervals_and_missing_issue_time() -> None:
    fixture = Path(__file__).parent / "fixtures/solcast_forecasts.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    normalized = normalize_payload(payload)

    assert normalized.source_kind == "forecast"
    assert normalized.issued_at is None
    assert normalized.raw_payload == payload
    assert len(normalized.intervals) == 97
    assert normalized.intervals[0]["end"] == "2026-11-09T15:30:00+00:00"
    assert normalized.intervals[-1]["end"] == "2026-11-11T15:30:00+00:00"
    assert all("ghi" not in row for row in payload["forecasts"])


@pytest.mark.django_db(transaction=True)
def test_capture_persists_budget_before_http_and_saves_sanitized_output(
    capture_binding: SiteBinding,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.INFO, logger="httpx")

    def read_committed_attempts() -> list[str]:
        try:
            return list(RequestAttempt.objects.values_list("purpose", flat=True))
        finally:
            connections.close_all()

    def handle(request: httpx.Request) -> httpx.Response:
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(read_committed_attempts).result(timeout=5) == [
                "inspect_forecast"
            ]
        assert str(request.url) == (
            "https://api.solcast.com.au/rooftop_sites/synthetic-site/forecasts?format=json"
        )
        assert request.headers["Authorization"] == "Bearer synthetic-capture-credential"
        return httpx.Response(
            200,
            json=forecast_payload(),
            headers={"X-RateLimit-Remaining": "2", "X-Private": "private-account"},
        )

    target = check_solcast.capture(
        capture_binding, "forecast", tmp_path, transport=httpx.MockTransport(handle)
    )

    assert json.loads(target.read_text(encoding="utf-8")) == check_solcast.sanitize(
        forecast_payload(), "forecast", "synthetic-site"
    )
    metadata = json.loads(target.with_suffix(".metadata.json").read_text(encoding="utf-8"))
    assert metadata["issued_at"] is None
    assert metadata["source_kind"] == "forecast"
    assert metadata["accuracy"] == "unvalidated"
    assert metadata["rate_limit_headers"] == {"x-ratelimit-remaining": "2"}
    assert RequestAttempt.objects.get().outcome == "succeeded"
    artifacts = "".join(path.read_text(encoding="utf-8") for path in tmp_path.iterdir())
    captured = capsys.readouterr()
    for private in ("synthetic-capture-credential", "synthetic-site", "private-account"):
        assert private not in artifacts
        assert private not in captured.out + captured.err
    assert "synthetic-capture-credential" not in caplog.text


@pytest.mark.django_db
def test_capture_shares_account_budget_and_prevents_duplicate_daily_kind(
    capture_binding: SiteBinding, tmp_path: Path
) -> None:
    reserve_account_request(capture_binding.provider_account, timezone.now(), purpose="validation")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rooftop_sites":
            return httpx.Response(
                200, json={"sites": [{"resource_id": "synthetic-site", "tilt": 0}]}
            )
        return httpx.Response(200, json=forecast_payload())

    transport = httpx.MockTransport(handle)
    check_solcast.capture(capture_binding, "settings", tmp_path, transport=transport)
    with pytest.raises(ValueError, match="already attempted today"):
        check_solcast.capture(capture_binding, "settings", tmp_path, transport=transport)
    check_solcast.capture(capture_binding, "forecast", tmp_path, transport=transport)
    assert sorted(RequestAttempt.objects.values_list("purpose", flat=True)) == [
        "inspect_forecast",
        "inspect_settings",
        "validation",
    ]
    with pytest.raises(QuotaExceededError):
        reserve_account_request(
            capture_binding.provider_account, timezone.now(), purpose="forecast"
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "failure", ["status", "timeout", "json", "schema", "body_secret", "header_secret"]
)
def test_failed_capture_is_private_consumes_quota_and_is_not_retried(
    capture_binding: SiteBinding, tmp_path: Path, failure: str
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "status":
            return httpx.Response(429, text="synthetic-capture-credential")
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic-capture-credential", request=request)
        if failure == "json":
            return httpx.Response(200, content=b"not-json")
        if failure == "schema":
            return httpx.Response(200, json={"forecasts": [{"period": "invalid"}]})
        if failure == "body_secret":
            return httpx.Response(
                200, json={"forecasts": [], "error": "synthetic-capture-credential"}
            )
        return httpx.Response(
            200, json=forecast_payload(), headers={"Retry-After": "synthetic-capture-credential"}
        )

    transport = httpx.MockTransport(handle)
    with pytest.raises(ProviderError, match="no retry was made") as caught:
        check_solcast.capture(capture_binding, "forecast", tmp_path, transport=transport)
    assert "synthetic-capture-credential" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert RequestAttempt.objects.get().outcome == "failed"
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="already attempted today"):
        check_solcast.capture(capture_binding, "forecast", tmp_path, transport=transport)
    assert len(requests) == 1


@pytest.mark.django_db
def test_output_failure_preserves_consumed_quota_and_daily_guard(
    capture_binding: SiteBinding, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_write(path: Path, data: str, **kwargs: object) -> int:
        raise OSError("Simulated full disk")

    monkeypatch.setattr(Path, "write_text", fail_write)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=forecast_payload()))

    with pytest.raises(OSError, match="Simulated full disk"):
        check_solcast.capture(capture_binding, "forecast", tmp_path, transport=transport)
    assert (
        RequestAttempt.objects.filter(provider_account=capture_binding.provider_account).count()
        == 1
    )
    with pytest.raises(ValueError, match="already attempted today"):
        check_solcast.capture(capture_binding, "forecast", tmp_path, transport=transport)
