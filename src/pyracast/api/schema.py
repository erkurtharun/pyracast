from typing import Any

from django.db import models
from drf_spectacular.plumbing import build_choice_field, deep_import_string
from drf_spectacular.settings import spectacular_settings
from rest_framework import serializers


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
