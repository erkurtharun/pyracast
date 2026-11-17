from typing import Any

from pyracast.errors import ErrorCode


def error_components(result: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    schemas = result.setdefault("components", {}).setdefault("schemas", {})
    schemas["ErrorCode"] = {"type": "string", "enum": [code.value for code in ErrorCode]}
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
