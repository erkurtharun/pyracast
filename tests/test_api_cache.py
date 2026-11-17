import pytest
from django.test import Client


@pytest.mark.parametrize("path", ["/api/session/", "/api/tenants/", "/api/missing/"])
def test_private_api_data_and_errors_are_not_cacheable(path: str) -> None:
    response = Client().get(path)
    assert "private" in response.headers["Cache-Control"]
    assert "no-store" in response.headers["Cache-Control"]


@pytest.mark.parametrize("path", ["/missing/", "/apiary/missing/"])
def test_api_cache_policy_does_not_change_unrelated_paths(path: str) -> None:
    response = Client().get(path)
    assert response.status_code == 404
    assert response["Content-Type"].startswith("text/html")
    assert "Cache-Control" not in response.headers
