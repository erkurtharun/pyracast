import hashlib
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from allauth.headless.socialaccount import internal as headless_social
from allauth.socialaccount.internal import statekit
from allauth.socialaccount.internal.flows import login as social_login
from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView, OAuth2View

# pyracast.accounts.social_views.ProviderCallbackView re-implements the first of these and rests
# on the others: the state is taken once and saved at once, the lookup runs before the adapter's
# pre_social_login, and the headless completion answers a refusal with error and error_process.
# Digests of allauth 65.22.1; a changed source fails here until the view is read against it.
SEQUENCE: dict[str, tuple[Callable[..., Any], str]] = {
    "OAuth2CallbackView.dispatch": (
        OAuth2CallbackView.dispatch,
        "313de4b5f9730fff3a5f113d79cbe2c9553d64fbf015232812cd527ee2048465",
    ),
    "OAuth2CallbackView._get_state": (
        OAuth2CallbackView._get_state,
        "46c33b1187bfc1bcc4836f64d804fceaa60108bd020c8fb5b7abbf8a2ceaa024",
    ),
    "OAuth2View.adapter_view": (
        OAuth2View.adapter_view,
        "884f2c09a0a4d56a6a9d3a26b4f438f90b3dd48604d61f4fd6224bc256ce28fb",
    ),
    "statekit.unstash_state": (
        statekit.unstash_state,
        "fc1cd6143295be0df3bc739361fe6476cc867be678d17d5edfb94bfff3965b1f",
    ),
    "flows.login.pre_social_login": (
        social_login.pre_social_login,
        "fabbd06cbdcb9d0c78588e00b9b60c2efd19e287de612e1425d8b9fe0c73dd5f",
    ),
    "flows.login.complete_login": (
        social_login.complete_login,
        "3825daff45dc7f8ecbadd8619653e87d99354a1c2f4bc4bfd4a35bdf63c65deb",
    ),
    "headless.socialaccount.internal.complete_login": (
        headless_social.complete_login,
        "48aca22a930270ffb3cb7eb8797ef906a908ff35a309f8f3581345a648237b0e",
    ),
}


def digest(function: Callable[..., Any]) -> str:
    source = inspect.getsource(function).replace("\r\n", "\n")
    return hashlib.sha256(source.encode()).hexdigest()


@pytest.mark.parametrize("name", SEQUENCE)
def test_the_provider_callback_rests_on_the_allauth_sequence_it_was_written_against(
    name: str,
) -> None:
    function, recorded = SEQUENCE[name]

    assert digest(function) == recorded, f"{name} changed: read ProviderCallbackView against it."
