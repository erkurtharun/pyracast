import runpy
from unittest.mock import patch

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError
from django.test import Client


def test_liveness_has_no_database_dependency(client: Client) -> None:
    response = client.get("/health/live/")
    assert response.status_code == 200
    assert response.content == b"ok"
    assert "no-store" in response.headers["Cache-Control"]
    assert not response.cookies


@pytest.mark.django_db
def test_readiness_checks_real_postgres(client: Client) -> None:
    response = client.get("/health/ready/")
    assert response.status_code == 200
    assert response.content == b"ok"
    assert "no-store" in response.headers["Cache-Control"]


def test_readiness_failure_exposes_no_database_details(client: Client) -> None:
    with patch(
        "pyracast.health.connection.cursor", side_effect=OperationalError("private details")
    ):
        response = client.get("/health/ready/")
    assert response.status_code == 503
    assert response.content == b"unavailable"
    assert "no-store" in response.headers["Cache-Control"]


@pytest.mark.parametrize("path", ["/health/live/", "/health/ready/"])
def test_health_rejects_mutation(client: Client, path: str) -> None:
    assert client.post(path).status_code == 405


def test_production_rejects_debug() -> None:
    with (
        patch("pyracast.settings.DEBUG", True),
        pytest.raises(ImproperlyConfigured, match="DEBUG=0"),
    ):
        runpy.run_module("pyracast.production")
    with patch("pyracast.settings.DEBUG", False):
        configuration = runpy.run_module("pyracast.production")
    assert configuration["STORAGES"]["staticfiles"]["BACKEND"] == (
        "whitenoise.storage.CompressedManifestStaticFilesStorage"
    )
