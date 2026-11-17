import json
from pathlib import Path

import pytest

from pyracast.accounts import session_lock

OPENAPI = Path(__file__).resolve().parents[1] / "web" / "openapi.json"


@pytest.mark.parametrize("path", ["//outside.example/admin/", "///outside.example/admin/"])
def test_busy_navigation_reloads_on_the_same_origin(path: str) -> None:
    from django.test import RequestFactory

    request = RequestFactory().get("/admin/?q=solar")
    request.path = path
    response = session_lock._busy_page(request)
    assert response["Refresh"] == "1; url=/outside.example/admin/?q=solar&session_retry=1"


def test_every_openapi_operation_declares_the_shared_error_body() -> None:
    from pyracast.api.schema import error_components

    result = error_components(
        {"paths": {"/api/example/": {"get": {"responses": {"200": {"description": "OK"}}}}}}
    )
    responses = result["paths"]["/api/example/"]["get"]["responses"]
    assert responses["200"] == {"description": "OK"}
    assert responses["default"] == {
        "description": "API failure.",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
    }

    schema = json.loads(OPENAPI.read_text(encoding="utf-8"))
    for path in schema["paths"].values():
        for operation in path.values():
            assert operation["responses"]["default"] == responses["default"]


def test_validation_errors_identify_each_item_in_a_list() -> None:
    from rest_framework.exceptions import ValidationError

    from pyracast.api.errors import api_error

    error = api_error(
        ValidationError({"plants": [{"name": ["Required."]}, {"name": ["Too long."]}]}),
        None,
        "-",
    )
    assert error.body() == {
        "code": "invalid_request",
        "message": "Check the highlighted values and try again.",
        "fields": {"plants.0.name": "Required.", "plants.1.name": "Too long."},
    }


@pytest.mark.parametrize("content", [b"{", b"[]"])
def test_non_object_error_responses_keep_their_original_content(content: bytes) -> None:
    from django.http import HttpResponse
    from django.test import RequestFactory

    from pyracast.api.errors import with_error_contract

    response = HttpResponse(content, status=502, content_type="application/json")
    result = with_error_contract(RequestFactory().get("/api/example/"), response)
    assert result is response
    assert (result.status_code, result.content) == (502, content)


def test_unmapped_client_status_has_a_localized_contract_without_a_length_header() -> None:
    from django.http import HttpResponse
    from django.test import RequestFactory

    from pyracast.api.errors import with_error_contract

    request = RequestFactory().get("/api/example/", HTTP_ACCEPT_LANGUAGE="en")
    response = HttpResponse('{"status": 418}', status=418, content_type="application/json")
    result = with_error_contract(request, response)
    assert result.status_code == 418
    assert "Content-Length" not in result
    assert json.loads(result.content) == {
        "status": 418,
        "code": "bad_request",
        "message": "The request could not be processed.",
    }
