import hashlib
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from allauth.socialaccount.internal import statekit
from allauth.socialaccount.providers.oauth2.views import OAuth2CallbackView

# pyracast.accounts.social_views.ProviderCallbackView re-implements the first of these up to the
# adapter's complete_login and takes its state through the others: the state is taken once and
# saved at once, before the session's serialization ends, and the store swapped in after the save
# is left unsaved. Digests of allauth 65.23.0; a changed source fails here until the view is read
# against it.
SEQUENCE: dict[str, tuple[Callable[..., Any], str]] = {
    "OAuth2CallbackView.dispatch": (
        OAuth2CallbackView.dispatch,
        "890d8c3ba73d4716ae9bfa68ab0be6a60b1c5b53b5615cbe4f76a4e82829bcb8",
    ),
    "OAuth2CallbackView._get_state": (
        OAuth2CallbackView._get_state,
        "46c33b1187bfc1bcc4836f64d804fceaa60108bd020c8fb5b7abbf8a2ceaa024",
    ),
    "statekit.unstash_state": (
        statekit.unstash_state,
        "fc1cd6143295be0df3bc739361fe6476cc867be678d17d5edfb94bfff3965b1f",
    ),
    "statekit._save_session": (
        statekit._save_session,
        "7d370458c72f85044195852de1d93097c13b6a9a668d3d79e304a58c9c1cb84d",
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
