import json
from urllib.parse import parse_qs, urlsplit

import pytest
from allauth.mfa.models import Authenticator
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse

from tests.support.admin_setup import admin_system, setup_input
from tests.support.api import verified_admin

pytestmark = pytest.mark.django_db
DEEP_LINK = "/admin/plants/7/?tab=review"
HOSTILE = [
    "https://elsewhere.example/admin/",
    "//elsewhere.example/admin/",
    "/\\elsewhere.example/admin/",
    "\\\\elsewhere.example/admin/",
    "/%5celsewhere.example/admin/",
    "http://testserver@elsewhere.example/admin/",
    "http://elsewhere.example@testserver/admin/",
    "javascript:alert(1)//admin/",
    "/admin/../app/",
    "/admin/%2e%2e/app/",
    "/admin/%2E%2E%2Fapp/",
    "/admin/.%2e/app/",
    "/admin//elsewhere.example/",
    "/admin/%2F%2Felsewhere.example/",
    "/admin/\r\nSet-Cookie: x=1",
    "/admin/%0d%0aLocation:%20https://elsewhere.example/",
    "/admin/\x00",
    " /admin/",
    "/app/w/",
    "",
]


def login(client: Client, target: str) -> str:
    response = client.get(reverse("admin:login"), {"next": target})
    assert response.status_code == 302
    return str(response["Location"])


def handoff_target(location: str) -> str:
    parts = urlsplit(location)
    assert parts.path == "/app/"
    (target,) = parse_qs(parts.query)["next"]
    return target


def test_a_deep_link_into_the_console_survives_sign_in() -> None:
    anonymous = Client()

    redirected = anonymous.get(DEEP_LINK)
    assert redirected.status_code == 302
    handoff = anonymous.get(redirected["Location"])

    assert handoff_target(handoff["Location"]) == DEEP_LINK
    signed_in, _ = verified_admin()
    assert login(signed_in, DEEP_LINK) == DEEP_LINK


@pytest.mark.parametrize("target", HOSTILE)
def test_a_return_address_outside_the_console_is_replaced_by_the_overview(target: str) -> None:
    signed_in, _ = verified_admin()

    assert handoff_target(login(Client(), target)) == "/admin/"
    assert login(signed_in, target) == "/admin/"
    language = signed_in.post(
        reverse("admin:workspace_language"), {"language": "tr", "next": target}
    )
    assert language["Location"] == "/admin/"


def test_the_console_sign_in_is_never_cached() -> None:
    response = Client().get(reverse("admin:login"), {"next": DEEP_LINK})

    assert "no-store" in response["Cache-Control"]


def test_an_account_without_console_access_is_recorded_and_sent_to_the_application(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = Client()
    client.force_login(User.objects.create_user(username="customer", email="c@example.com"))

    with caplog.at_level("INFO", logger="pyracast.security"):
        response = client.get(reverse("admin:login"), {"next": DEEP_LINK})

    assert response["Location"] == "/app/"
    (refusal,) = [
        json.loads(record.getMessage())
        for record in caplog.records
        if '"event": "authorization_denied"' in record.getMessage()
    ]
    assert (refusal["reason"], refusal["view"]) == ("platform_admin_required", "admin/login/")


@pytest.mark.parametrize("identity", ["demoted", "factor_removed", "customer", "anonymous"])
def test_signing_out_ends_any_session(identity: str) -> None:
    if identity == "anonymous":
        client = Client()
    elif identity == "customer":
        client = Client()
        client.force_login(User.objects.create_user(username="customer"))
    else:
        client, user = verified_admin()
        if identity == "demoted":
            User.objects.filter(pk=user.pk).update(is_superuser=False)
        else:
            Authenticator.objects.filter(user=user).delete()

    response = client.post(reverse("admin:logout"))

    assert (response.status_code, response["Location"]) == (302, "/app/")
    assert client.get("/api/session/").json()["user"] is None


def test_signing_out_needs_a_post_with_its_form_token() -> None:
    client, _ = verified_admin(enforce_csrf_checks=True)

    assert client.get(reverse("admin:logout")).status_code == 405
    assert client.post(reverse("admin:logout")).status_code == 403
    assert client.get("/api/session/").json()["user"] is not None


@pytest.mark.parametrize(
    "name", ["admin:index", "admin:workspace_plants", "admin:workspace_operations"]
)
def test_read_pages_refuse_writes(name: str) -> None:
    client, _ = verified_admin()

    assert client.post(reverse(name)).status_code == 405


def test_a_successful_plant_setup_reports_success() -> None:
    system = admin_system()
    client, _ = verified_admin()
    url = reverse("admin:workspace_plant", args=(system.region.pk,))

    saved = client.post(url, setup_input(system, site_id="moved-site"))

    assert saved.status_code == 302
    assert 'class="notice notice--success"' in client.get(url).content.decode()
