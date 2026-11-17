import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.test import Client, override_settings

PASSWORD = "test-only-" + "harbor-lantern-42!"


@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@override_settings(
    SESSION_COOKIE_NAME="__Host-pyracast-sessionid",
    CSRF_COOKIE_NAME="__Host-pyracast-csrftoken",
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
    CSRF_TRUSTED_ORIGINS=["https://testserver"],
)
def test_production_cookie_attributes_satisfy_the_host_prefix() -> None:
    user = User.objects.create_user(
        username="member", email="member@example.com", password=PASSWORD
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    client = Client(enforce_csrf_checks=True)

    session = client.get("/api/session/", secure=True)
    token = session.json()["csrf_token"]
    login = client.post(
        "/api/auth/browser/v1/auth/login",
        {"email": user.email, "password": PASSWORD},
        content_type="application/json",
        secure=True,
        headers={"X-CSRFToken": token, "Origin": "https://testserver"},
    )

    assert login.status_code == 200
    cookies = [
        session.cookies["__Host-pyracast-csrftoken"],
        login.cookies["__Host-pyracast-sessionid"],
    ]
    for cookie in cookies:
        header = cookie.OutputString()
        assert cookie["secure"] is True
        assert cookie["path"] == "/"
        assert cookie["domain"] == ""
        assert "; Secure" in header
        assert "Path=/" in header
        assert "Domain" not in header
