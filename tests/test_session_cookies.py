import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.test import Client, override_settings

from pyracast import settings as production

PASSWORD = "test-only-" + "harbor-lantern-42!"


# tests/settings.py pins PYRACAST_DEBUG=0 before importing pyracast.settings, so this module holds
# the production cookie configuration that the test overlay replaces.
@pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])
@override_settings(
    SESSION_COOKIE_NAME=production.SESSION_COOKIE_NAME,
    CSRF_COOKIE_NAME=production.CSRF_COOKIE_NAME,
    SESSION_COOKIE_SECURE=production.SESSION_COOKIE_SECURE,
    CSRF_COOKIE_SECURE=production.CSRF_COOKIE_SECURE,
    CSRF_TRUSTED_ORIGINS=["https://testserver"],
)
def test_production_cookie_attributes_satisfy_the_host_prefix() -> None:
    assert production.DEBUG is False
    assert production.SESSION_COOKIE_NAME == "__Host-pyracast-sessionid"
    assert production.CSRF_COOKIE_NAME == "__Host-pyracast-csrftoken"
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
        session.cookies[production.CSRF_COOKIE_NAME],
        login.cookies[production.SESSION_COOKIE_NAME],
    ]
    for cookie in cookies:
        header = cookie.OutputString()
        assert cookie["secure"] is True
        assert cookie["path"] == "/"
        assert cookie["domain"] == ""
        assert "; Secure" in header
        assert "Path=/" in header
        assert "Domain" not in header
