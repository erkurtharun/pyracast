import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client

from tests.test_domain import make_system

pytestmark = pytest.mark.django_db(transaction=True)


def test_customer_schema_excludes_operator_configuration() -> None:
    system = make_system()
    client = Client()
    client.force_login(system.member)
    response = client.get("/api/schema/?format=json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["paths"]
    assert all(not path.startswith("/api/platform/") for path in schema["paths"])
    assert "solcast" not in json.dumps(schema).lower()
    assert "AccountInput" not in schema["components"]["schemas"]


def test_generated_customer_schema_validates_without_operator_components() -> None:
    output = StringIO()
    call_command(
        "spectacular", format="openapi-json", validate=True, fail_on_warn=True, stdout=output
    )
    schema = json.loads(output.getvalue())
    assert "/api/tenants/{tenant_id}/regions/{region_id}/" in schema["paths"]
    assert "solcast" not in output.getvalue().lower()
