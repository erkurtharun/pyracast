import argparse
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import django
import httpx

if TYPE_CHECKING:
    from pyracast.connections.models import ProviderAccount


SITE_FIELDS = (
    "capacity",
    "capacity_dc",
    "latitude",
    "longitude",
    "tilt",
    "azimuth",
    "install_date",
    "loss_factor",
    "efficiency_factor",
    "timezone",
    "time_zone",
    "terrain_shading",
    "snow_soiling",
)
FORECAST_FIELDS = ("period_end", "period", "pv_estimate", "pv_estimate10", "pv_estimate90", "ghi")


def sanitize(payload: object, kind: str, site_id: str) -> dict[str, object]:
    from pyracast.connections.contracts import ProviderFailure
    from pyracast.providers import normalize_payload

    if kind == "forecast":
        normalize_payload(payload)
        rows = cast(dict[str, list[dict[str, object]]], payload)["forecasts"]
        return {
            "forecasts": [{key: row[key] for key in FORECAST_FIELDS if key in row} for row in rows]
        }
    if not isinstance(payload, dict) or not isinstance(payload.get("sites"), list):
        raise ProviderFailure("invalid_site_schema")
    sites = [
        row
        for row in payload["sites"]
        if isinstance(row, dict) and row.get("resource_id") == site_id
    ]
    if len(sites) != 1:
        raise ProviderFailure("reference_site_not_found")
    return {key: sites[0][key] for key in SITE_FIELDS if key in sites[0]}


def capture(
    account: ProviderAccount,
    kind: str,
    output: Path,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    from django.db import transaction
    from django.utils import timezone

    from pyracast.connections.contracts import ProviderFailure
    from pyracast.connections.models import ProviderAccount
    from pyracast.connections.operations import reserve_account_request
    from pyracast.forecasts.models import RequestAttempt
    from pyracast.providers import _contains_secret

    if kind not in {"settings", "forecast"}:
        raise ValueError("Unknown capture kind.")
    secret = os.environ.get("SOLCAST_API_KEY", "")
    site_id = os.environ.get("SOLCAST_RESOURCE_ID", "")
    if not secret or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", site_id):
        raise ValueError("SOLCAST_API_KEY and a valid SOLCAST_RESOURCE_ID are required.")
    output.mkdir(parents=True, exist_ok=True)
    purpose = "inspect_" + kind
    with transaction.atomic():
        account = ProviderAccount.objects.select_for_update().get(pk=account.pk)
        if account.provider != "solcast" or account.secret_reference != "env:SOLCAST_API_KEY":  # noqa: S105 -- environment variable name.
            raise ValueError("A Solcast account using env:SOLCAST_API_KEY is required.")
        now = timezone.now()
        if RequestAttempt.objects.filter(
            provider_account=account, utc_day=now.date(), purpose=purpose
        ).exists():
            raise ValueError("This check was already attempted today; use the saved fixture.")
        attempt = reserve_account_request(account, now, purpose=purpose)
    path = "/rooftop_sites" if kind == "settings" else f"/rooftop_sites/{site_id}/forecasts"
    try:
        with httpx.Client(
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False, transport=transport
        ) as client:
            response = client.get(
                "https://api.solcast.com.au" + path,
                headers={"Authorization": f"Bearer {secret}"},
                params={"format": "json"},
            )
            if response.status_code != 200:
                raise ProviderFailure(f"provider_http_{response.status_code}")
            payload = response.json()
        if _contains_secret(payload, secret) or _contains_secret(dict(response.headers), secret):
            raise ProviderFailure("unsafe_provider_response")
        safe = sanitize(payload, kind, site_id)
    except httpx.HTTPError, ValueError, ProviderFailure:
        RequestAttempt.objects.filter(pk=attempt.pk).update(outcome="failed")
        raise ProviderFailure(
            "Live check failed; no retry was made. Inspect the account in Toolkit."
        ) from None
    collected_at = timezone.now()
    target = output / f"solcast_{kind}_{attempt.utc_day.isoformat()}_{attempt.pk}.json"
    target.write_text(json.dumps(safe, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    metadata: dict[str, object] = {
        "collected_at": collected_at.isoformat(),
        "issued_at": None,
        "source_kind": "forecast" if kind == "forecast" else "site_configuration",
        "authentication": "bearer",
        "http_status": 200,
        "sanitization": "allowlisted fields; account and site identifiers omitted",
        "accuracy": "unvalidated",
        "request_attempt_id": attempt.pk,
        "rate_limit_headers": {
            key: value
            for key, value in response.headers.items()
            if key.lower()
            in {
                "x-ratelimit-limit",
                "x-ratelimit-remaining",
                "x-ratelimit-reset",
                "retry-after",
            }
        },
    }
    target.with_suffix(".metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    RequestAttempt.objects.filter(pk=attempt.pk).update(outcome="succeeded")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture one quota-counted Solcast response without retries."
    )
    parser.add_argument(
        "--live", action="store_true", help="Explicitly enable a live provider request."
    )
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--kind", choices=("settings", "forecast"), required=True)
    parser.add_argument("--output", type=Path, default=Path(".codex/solcast"))
    args = parser.parse_args()
    if not args.live:
        parser.error("Live requests require --live.")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pyracast.settings")
    django.setup()
    from pyracast.connections.models import ProviderAccount

    try:
        target = capture(ProviderAccount.objects.get(pk=args.account_id), args.kind, args.output)
    except Exception as error:
        raise SystemExit(
            f"Live check stopped ({type(error).__name__}); no automatic retry."
        ) from None
    print(f"Saved sanitized {args.kind} response: {target}")


if __name__ == "__main__":
    main()
