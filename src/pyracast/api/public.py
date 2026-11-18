import re
from collections.abc import Mapping
from typing import Any, cast

type Allowlist = Mapping[str, "Allowlist | None"]

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
        "authorized_by_id",
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
# Every "source" key carries text a customer or operator entered for a production value.
_CUSTOMER_TEXT_FIELDS = frozenset({"source"})
_NORMALIZATION: Allowlist = dict.fromkeys(
    ("policy", "source_capacity_kw", "target_capacity_kw", "factor", "normalized_at")
)
# Stored provenance keeps operator detail; customers receive these fields only.
_PUBLIC_SUBTREES: Mapping[str, Allowlist] = {
    "capacity_normalization": _NORMALIZATION,
    "reference_capacity_normalization": _NORMALIZATION,
    "reference_import": {
        "id": None,
        "sha256": None,
        "imported_at": None,
        "source_settings": {
            **dict.fromkeys(
                ("product", "settings_matched", "unit", "timestamp_convention", "period")
            ),
            "upload_provenance": dict.fromkeys(
                ("file_verified_fields", "operator_declared_fields", "warnings")
            ),
        },
    },
}
_PUBLIC_KEYS = {"solcast_ghi_wm2": "reference_ghi_wm2", "solcast_kwh_m2": "reference_kwh_m2"}
_PUBLIC_SOURCE_KINDS = {
    "solcast_forecast": "forecast",
    "solcast_estimated_actuals": "estimated_actuals",
}


def _public_string(value: str, field: str) -> str | None:
    normalized = value.casefold()
    if "solcast" not in normalized:
        return value
    if normalized in _PUBLIC_SOURCE_KINDS:
        return _PUBLIC_SOURCE_KINDS[normalized]
    if field == "quality_flags":
        return re.sub("solcast", "reference", value, flags=re.IGNORECASE)
    return None


def _allowed(value: Any, allowlist: Allowlist) -> Any:
    if not isinstance(value, Mapping):
        return value
    return {
        key: item if (nested := allowlist[key]) is None else _allowed(item, nested)
        for key, item in cast(Mapping[str, Any], value).items()
        if key in allowlist
    }


def public_source_data(value: Any, *, technical: bool = False, field: str = "") -> Any:
    # Projection occurs only at delivery boundaries; stored acquisitions remain immutable.
    if isinstance(value, Mapping):
        result: dict[str | int, Any] = {}
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
            if (normalized in _CUSTOMER_TEXT_FIELDS and isinstance(item, str)) or (
                not technical and normalized in _USER_TEXT_FIELDS
            ):
                result[public_key] = item
                continue
            if normalized in _PUBLIC_SUBTREES:
                item = _allowed(item, _PUBLIC_SUBTREES[normalized])
            result[public_key] = public_source_data(
                item, technical=technical or normalized in _TECHNICAL_FIELDS, field=normalized
            )
        return result
    if isinstance(value, list | tuple):
        return [public_source_data(item, technical=technical, field=field) for item in value]
    if technical and isinstance(value, str):
        return _public_string(value, field)
    return value
