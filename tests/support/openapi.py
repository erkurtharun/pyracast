import json
from functools import cache
from io import StringIO
from typing import Any

from django.core.management import call_command
from jsonschema import Draft202012Validator


@cache
def components() -> dict[str, Any]:
    output = StringIO()
    call_command("spectacular", format="openapi-json", stdout=output)
    schemas: dict[str, Any] = json.loads(output.getvalue())["components"]["schemas"]
    return schemas


def _nullable(node: Any) -> Any:
    """OpenAPI 3.0 `nullable` as JSON Schema 2020-12, as a client reads a response."""
    if isinstance(node, list):
        return [_nullable(item) for item in node]
    if not isinstance(node, dict):
        return node
    converted = {key: _nullable(value) for key, value in node.items() if key != "nullable"}
    if node.get("nullable") is True:
        return {"anyOf": [converted, {"type": "null"}]}
    return converted


def validate(component: str, payload: Any) -> list[str]:
    schema = {"$ref": f"#/$defs/{component}", "$defs": _nullable(components())}
    text = json.dumps(schema).replace("#/components/schemas/", "#/$defs/")
    validator = Draft202012Validator(json.loads(text))
    return sorted(
        f"{'/'.join(str(part) for part in error.absolute_path)}: {error.message}"
        for error in validator.iter_errors(payload)
    )
