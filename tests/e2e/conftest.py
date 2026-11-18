from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.support.browser import capture_codes, live_settings, production_gateway


@pytest.fixture(scope="session")
def web_origin(live_server: Any) -> Iterator[str]:
    with production_gateway(live_server.url) as origin:
        yield origin


@pytest.fixture(autouse=True)
def browser_settings(web_origin: str) -> Iterator[None]:
    with live_settings(web_origin):
        yield


@pytest.fixture
def auth_mailbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return capture_codes(tmp_path / "auth-mailbox.json", monkeypatch)
