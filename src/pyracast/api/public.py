import re
from collections.abc import Mapping
from typing import Any, cast

_PRIVATE_FIELDS = frozenset(
    {
        "provider_account",
        "provider_account_id",
        "external_account_id",
        "account_id",
        "site_id",
        "resource_id",
        "resource_ids",
        "secret_reference",
        "encrypted_api_key",
        "api_key",
        "authorization_reference",
        "credential_revision",
        "raw_payload",
    }
)
_TECHNICAL_FIELDS = frozenset(
    {
        "result",
        "dataset_manifest",
        "provenance",
        "vintage",
        "model_version",
        "method_version",
        "method",
        "source_kind",
        "source",
        "provider",
        "selection_policy",
        "quality_flags",
        "assessment",
        "uncertainty",
        "last_error",
        "code",
        "detail",
    }
)
_USER_TEXT_FIELDS = frozenset({"name", "username", "email", "recipient"})
_PUBLIC_KEYS = {"solcast_ghi_wm2": "reference_ghi_wm2", "solcast_kwh_m2": "reference_kwh_m2"}
_PUBLIC_SOURCE_KINDS = {
    "solcast_forecast": "forecast",
    "solcast_estimated_actuals": "estimated_actuals",
}


def _public_string(value: str, field: str) -> str:
    normalized = value.casefold()
    if "solcast" not in normalized:
        return value
    if normalized in _PUBLIC_SOURCE_KINDS:
        return _PUBLIC_SOURCE_KINDS[normalized]
    if field == "quality_flags":
        return re.sub("solcast", "reference", value, flags=re.IGNORECASE)
    return "source_details_unavailable"


def public_source_data(value: Any, *, technical: bool = False, field: str = "") -> Any:
    # Projection occurs only at delivery boundaries; stored acquisitions remain immutable.
    if isinstance(value, Mapping):
        result: dict[str | int, Any] = {}
        observation = "source" in value and ("starts_at" in value or "observation_id" in value)
        for key, item in cast(Mapping[str | int, Any], value).items():
            normalized = str(key).casefold()
            if normalized in _PRIVATE_FIELDS or (
                normalized == "provider"
                and (not isinstance(item, str) or item.casefold() in ("solcast", "fixture"))
            ):
                continue
            if "solcast" in normalized and normalized not in _PUBLIC_KEYS:
                continue
            public_key = _PUBLIC_KEYS.get(normalized, key)
            user_text = (not technical and normalized in _USER_TEXT_FIELDS) or (
                observation and normalized == "source"
            )
            result[public_key] = (
                item
                if user_text
                else public_source_data(
                    item,
                    technical=technical or normalized in _TECHNICAL_FIELDS,
                    field=normalized,
                )
            )
        return result
    if isinstance(value, list | tuple):
        return [public_source_data(item, technical=technical, field=field) for item in value]
    if technical and isinstance(value, str):
        return _public_string(value, field)
    return value
