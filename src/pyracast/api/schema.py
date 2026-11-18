from typing import Any

from django.db import models
from drf_spectacular.plumbing import build_choice_field, deep_import_string
from drf_spectacular.settings import spectacular_settings
from rest_framework import serializers


def error_components(result: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    schemas = result.setdefault("components", {}).setdefault("schemas", {})
    schemas["Error"] = {
        "type": "object",
        "description": "Body of every failed /api/ response.",
        "properties": {
            "code": {"$ref": "#/components/schemas/ErrorCode"},
            "message": {"type": "string", "description": "Localized text for the user."},
            "fields": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Localized text per dotted input path.",
            },
            "retry_after": {"type": "integer", "minimum": 0, "description": "Seconds."},
            "request_id": {"type": "string", "description": "Reference to the server log."},
        },
        "required": ["code", "message"],
    }
    for path in result.get("paths", {}).values():
        for method in ("get", "post", "put", "patch", "delete", "head", "options", "trace"):
            if method in path:
                path[method]["responses"]["default"] = {
                    "description": "API failure.",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
                    },
                }
    return result


def stored_vocabularies(result: dict[str, Any], **kwargs: object) -> dict[str, Any]:
    """Publish every enumeration of ENUM_NAME_OVERRIDES, also those no customer endpoint uses."""
    schemas = result.setdefault("components", {}).setdefault("schemas", {})
    for name, path in spectacular_settings.ENUM_NAME_OVERRIDES.items():
        choices = deep_import_string(path) if isinstance(path, str) else None
        if (
            name not in schemas
            and isinstance(choices, type)
            and issubclass(choices, models.TextChoices)
        ):
            schema = build_choice_field(serializers.ChoiceField(choices=choices.choices))
            schema.pop("x-spec-enum-id", None)
            schemas[name] = schema
    result["components"]["schemas"] = dict(sorted(schemas.items()))
    return result
