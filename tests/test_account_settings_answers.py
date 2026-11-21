from collections.abc import Iterator

import pytest
from allauth.socialaccount.models import SocialAccount
from django.test import Client, override_settings

from tests.support.account_sessions import account, passwordless, signed_in
from tests.support.auth_lifecycle import AUTH, submit
from tests.support.social_accounts import PROVIDERS

pytestmark = pytest.mark.django_db(databases=["default", "auth_cache"], transaction=True)


@pytest.fixture(autouse=True)
def only_google() -> Iterator[None]:
    with override_settings(
        SOCIALACCOUNT_PROVIDERS={"google": PROVIDERS["google"]},
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}},
    ):
        yield


def connections(client: Client) -> dict[str, dict[str, object]]:
    response = client.get(AUTH + "account/providers")
    assert response.status_code == 200
    return {entry["provider"]["id"]: entry for entry in response.json()["data"]}


def test_a_common_password_for_an_account_without_one_is_refused_before_a_confirmation() -> None:
    user = passwordless("owner")
    client = signed_in(user)

    response = submit(client, "account/password/change", {"new_password": "password"})

    assert response.status_code == 400
    assert {error["param"] for error in response.json()["errors"]} == {"new_password"}


def test_each_connected_account_says_whether_it_can_be_disconnected_and_used_to_sign_in() -> None:
    user = account("owner")
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")
    SocialAccount.objects.create(user=user, provider="microsoft", uid="microsoft-identity")

    listed = connections(signed_in(user))

    assert {name: entry["can_disconnect"] for name, entry in listed.items()} == {
        "google": True,
        "microsoft": True,
    }
    assert {name: entry["sign_in_available"] for name, entry in listed.items()} == {
        "google": True,
        "microsoft": False,
    }


def test_the_last_way_to_sign_in_cannot_be_disconnected() -> None:
    user = passwordless("owner")
    SocialAccount.objects.create(user=user, provider="google", uid="google-identity")

    listed = connections(signed_in(user))

    assert listed["google"]["can_disconnect"] is False
