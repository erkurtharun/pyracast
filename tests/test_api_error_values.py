import json
import re
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
        {
            "paths": {
                "/api/example/": {
                    "summary": "Example API",
                    "parameters": [],
                    "get": {"responses": {"200": {"description": "OK"}}},
                }
            }
        }
    )
    responses = result["paths"]["/api/example/"]["get"]["responses"]
    assert responses["200"] == {"description": "OK"}
    assert responses["default"] == {
        "description": "API failure.",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
    }

    schema = json.loads(OPENAPI.read_text(encoding="utf-8"))
    schema["paths"]["/api/example/"] = result["paths"]["/api/example/"]
    for path in schema["paths"].values():
        for method in ("get", "post", "put", "patch", "delete", "head", "options", "trace"):
            if method in path:
                assert path[method]["responses"]["default"] == responses["default"]
    assert result["paths"]["/api/example/"]["summary"] == "Example API"
    assert result["paths"]["/api/example/"]["parameters"] == []


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


@pytest.mark.parametrize("content", [b"{", b"[]", b"null", b'"private detail"'])
@pytest.mark.parametrize(
    ("language", "message"),
    [
        ("en", "The server could not complete this request."),
        ("tr", "Sunucu bu isteği tamamlayamad\u0131."),
    ],
)
def test_non_object_error_responses_receive_the_localized_contract(
    content: bytes, language: str, message: str
) -> None:
    from django.http import HttpResponse
    from django.test import RequestFactory

    from pyracast.api.errors import with_error_contract

    response = HttpResponse(content, status=502, content_type="application/json")
    request = RequestFactory().get("/api/example/", HTTP_ACCEPT_LANGUAGE=language)
    result = with_error_contract(request, response)
    assert result is response
    assert result.status_code == 502
    body = json.loads(result.content)
    assert re.fullmatch(r"[a-f0-9]{32}", body["request_id"])
    assert body == {
        "code": "server_error",
        "message": message,
        "request_id": body["request_id"],
    }


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


def test_error_bodies_beyond_the_parse_bound_are_left_alone() -> None:
    from django.http import HttpResponse
    from django.test import RequestFactory

    from pyracast.api.errors import with_error_contract

    content = json.dumps({"status": 400, "padding": "x" * 1_000_000}).encode()
    response = HttpResponse(content, status=400, content_type="application/json")
    response["ETag"] = '"original"'
    response.set_cookie("example", "original")
    result = with_error_contract(RequestFactory().get("/api/example/"), response)
    assert result is response
    assert result.content == content
    assert result["ETag"] == '"original"'
    assert result.cookies["example"].value == "original"


def test_rewritten_error_bodies_drop_the_stale_entity_tag() -> None:
    from django.http import HttpResponse, HttpResponseNotAllowed
    from django.test import RequestFactory

    from pyracast.api.errors import refuse_in_place, with_error_contract
    from pyracast.errors import ErrorCode

    request = RequestFactory().get("/api/example/")
    rewritten = HttpResponse('{"status": 400}', status=400, content_type="application/json")
    rewritten["ETag"] = '"before"'
    assert "ETag" not in with_error_contract(request, rewritten)
    refused = HttpResponseNotAllowed(["GET"])
    refused["ETag"] = '"before"'
    assert "ETag" not in refuse_in_place(request, refused, ErrorCode.METHOD_NOT_ALLOWED)


def test_framework_refusals_preserve_existing_transport_headers() -> None:
    from django.http import HttpResponseNotAllowed
    from django.test import RequestFactory

    from pyracast.api.errors import refuse_in_place
    from pyracast.errors import ErrorCode

    response = HttpResponseNotAllowed(["GET"])
    response["Retry-After"] = "3"
    response["WWW-Authenticate"] = "Session"
    result = refuse_in_place(
        RequestFactory().get("/api/example/", HTTP_ACCEPT_LANGUAGE="en"),
        response,
        ErrorCode.METHOD_NOT_ALLOWED,
    )
    assert result is response
    assert result.status_code == 405
    assert result["Allow"] == "GET"
    assert result["Retry-After"] == "3"
    assert result["WWW-Authenticate"] == "Session"
    assert json.loads(result.content) == {
        "code": "method_not_allowed",
        "message": "This action is not available here.",
    }
