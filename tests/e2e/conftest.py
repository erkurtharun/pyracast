from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.support.browser import Gateway, capture_codes, live_settings, serving


@pytest.fixture(scope="session")
def web_gateway(live_server: Any) -> Iterator[Gateway]:
    with serving(live_server.url) as gateway:
        yield gateway


@pytest.fixture(autouse=True)
def browser_settings(web_gateway: Gateway) -> Iterator[None]:
    with live_settings(web_gateway.origin):
        yield


@pytest.fixture
def auth_mailbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return capture_codes(tmp_path / "auth-mailbox.json", monkeypatch)
