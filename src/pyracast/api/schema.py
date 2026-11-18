import re
from typing import Any

from django.db import models
from drf_spectacular import openapi
from drf_spectacular.plumbing import ComponentRegistry, build_choice_field, deep_import_string
from drf_spectacular.settings import spectacular_settings
from rest_framework import serializers

from pyracast.api.permissions import ROUTE_POLICIES, Access, Policy

REFUSALS = {
    "400": "The request input was refused.",
    "401": "Sign-in is required.",
    "403": "Access was refused.",
    "404": "The addressed item or page does not exist.",
    "429": "Too many requests; Retry-After gives the seconds to wait.",
}


def documented_refusals(policy: Policy, method: str, operation: dict[str, Any]) -> list[str]:
    parameters = operation.get("parameters", [])
    query = {parameter["name"] for parameter in parameters if parameter["in"] == "query"}
    path = {parameter["name"] for parameter in parameters if parameter["in"] == "path"}
    statuses = {
        "400": method in {"POST", "PUT", "PATCH"} or bool(query - {"page"}),
        "401": policy.access != Access.PUBLIC,
        # Every unsafe method also answers 403 when the session's CSRF check fails.
        "403": method not in {"GET", "HEAD"} or policy.access in {Access.MEMBER, Access.ADMIN},
        "404": bool(path - {"tenant_id"}) or "page" in query,
        "429": policy.throttle is not None,
    }
    return [status for status, applies in statuses.items() if applies]


class AutoSchema(openapi.AutoSchema):
    def get_operation(
        self,
        path: str,
        path_regex: str,
        path_prefix: str,
        method: str,
        registry: ComponentRegistry,
    ) -> dict[str, Any] | None:
        operation = super().get_operation(path, path_regex, path_prefix, method, registry)
        if operation is not None:
            route = re.sub(r"\{(\w+)\}", r"<int:\1>", path.removeprefix("/"))
            policy = ROUTE_POLICIES[route][self.method]
            for status in documented_refusals(policy, self.method, operation):
                operation["responses"][status] = {
                    "description": REFUSALS[status],
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
                    },
                }
        return operation


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
