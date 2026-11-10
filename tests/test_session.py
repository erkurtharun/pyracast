import pytest
from django.contrib.auth.models import User
from django.test import Client

pytestmark = pytest.mark.django_db

PASSWORD = "test-only-" + "x" * 40


def test_session_login_logout_and_csrf_rotation() -> None:
    user = User.objects.create_user(username="member", password=PASSWORD, email="a@example.com")
    client = Client(enforce_csrf_checks=True)
    initial_response = client.get("/api/session/")
    assert "no-store" in initial_response.headers["Cache-Control"]
    assert "private" in initial_response.headers["Cache-Control"]
    initial = initial_response.json()
    assert initial["user"] is None
    assert (
        client.post(
            "/api/session/login/",
            {"username": "member", "password": PASSWORD},
            content_type="application/json",
        ).status_code
        == 403
    )
    response = client.post(
        "/api/session/login/",
        {"username": "member", "password": PASSWORD},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=initial["csrf_token"],
    )
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    logged_in = response.json()
    assert logged_in["user"] == {"id": user.pk, "username": "member", "email": "a@example.com"}
    assert client.get("/api/session/").json()["user"] == logged_in["user"]
    assert client.cookies["sessionid"]["httponly"]
    assert client.post("/api/session/logout/").status_code == 403
    assert (
        client.post("/api/session/logout/", HTTP_X_CSRFTOKEN=initial["csrf_token"]).status_code
        == 403
    )
    response = client.post("/api/session/logout/", HTTP_X_CSRFTOKEN=logged_in["csrf_token"])
    assert response.status_code == 200
    assert response.json()["user"] is None
    assert (
        client.post("/api/session/logout/", HTTP_X_CSRFTOKEN=logged_in["csrf_token"]).status_code
        == 403
    )
    assert client.get("/api/session/").json()["user"] is None


@pytest.mark.parametrize(
    ("username", "password"),
    [("member", "wrong"), ("missing", PASSWORD), ("disabled", PASSWORD)],
)
def test_invalid_credentials_are_generic(username: str, password: str) -> None:
    User.objects.create_user(username="member", password=PASSWORD)
    User.objects.create_user(username="disabled", password=PASSWORD, is_active=False)
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/session/").json()["csrf_token"]
    response = client.post(
        "/api/session/login/",
        {"username": username, "password": password},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 403
    assert response.json() == {"code": "http_403", "detail": {"detail": "Invalid credentials."}}
    assert client.get("/api/session/").json()["user"] is None


def test_email_update_requires_session_csrf_and_valid_address() -> None:
    user = User.objects.create_user(username="member", password=PASSWORD, email="a@example.com")
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/session/").json()["csrf_token"]
    assert (
        client.patch(
            "/api/session/",
            {"email": "b@example.com"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        ).status_code
        == 403
    )
    login_response = client.post(
        "/api/session/login/",
        {"username": "member", "password": PASSWORD},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    token = login_response.json()["csrf_token"]
    assert (
        client.patch(
            "/api/session/", {"email": "b@example.com"}, content_type="application/json"
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/api/session/",
            {"email": "bad"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        ).status_code
        == 400
    )
    user.refresh_from_db()
    assert user.email == "a@example.com"
    response = client.patch(
        "/api/session/",
        {"email": "b@example.com"},
        content_type="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert response.status_code == 200
    assert response.json()["user"]["email"] == "b@example.com"
    user.refresh_from_db()
    assert user.email == "b@example.com"


def test_login_input_is_bounded_and_rejects_unknown_fields() -> None:
    client = Client(enforce_csrf_checks=True)
    token = client.get("/api/session/").json()["csrf_token"]
    for data in (
        {"username": "member", "password": "x" * 1025},
        {"username": "member", "password": PASSWORD, "extra": "value"},
    ):
        response = client.post(
            "/api/session/login/", data, content_type="application/json", HTTP_X_CSRFTOKEN=token
        )
        assert response.status_code == 400
        assert response.json()["code"] == "http_400"


def test_session_schema_requires_cookie_for_email_update() -> None:
    client = Client()
    client.force_login(User.objects.create_user(username="member"))
    response = client.get("/api/schema/?format=json")
    assert response.status_code == 200
    session = response.json()["paths"]["/api/session/"]
    assert not session["get"].get("security")
    assert session["patch"]["security"] == [{"cookieAuth": []}]
