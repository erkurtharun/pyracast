import os
from functools import lru_cache
from threading import Lock

import httpx

from pyracast.epias import CREDENTIALS_MISSING, EpiasClient, EpiasCredentialsError

_client_lock = Lock()


def get_epias_client() -> EpiasClient:
    with _client_lock:
        return _cached_client()


@lru_cache(maxsize=1)
def _cached_client() -> EpiasClient:
    username = os.environ.get("EPIAS_USERNAME", "")
    password = os.environ.get("EPIAS_PASSWORD", "")
    if not username or not password:
        raise EpiasCredentialsError(CREDENTIALS_MISSING)
    return EpiasClient(httpx.Client(timeout=30), username=username, password=password)
