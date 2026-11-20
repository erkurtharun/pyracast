from functools import lru_cache
from threading import Lock

import httpx
from django.conf import settings

from pyracast.epias import CREDENTIALS_MISSING, EpiasClient, EpiasCredentialsError
from pyracast.outbound import TIMEOUT

_client_lock = Lock()


def get_epias_client() -> EpiasClient:
    with _client_lock:
        return _cached_client()


@lru_cache(maxsize=1)
def _cached_client() -> EpiasClient:
    username = settings.PYRACAST_EPIAS_USERNAME
    password = settings.PYRACAST_EPIAS_PASSWORD
    if not username or not password:
        raise EpiasCredentialsError(CREDENTIALS_MISSING)
    return EpiasClient(httpx.Client(timeout=TIMEOUT), username=username, password=password)
