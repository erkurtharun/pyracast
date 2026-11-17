from typing import cast

from pyracast.settings import *  # noqa: F403 -- Django settings overlay.
from pyracast.settings import DATABASES

SECRET_KEY = "test-only-not-a-deployment-secret"  # noqa: S105
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
SECURE_SSL_REDIRECT = False
PYRACAST_PUBLIC_ORIGIN = "https://testserver"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
SESSION_COOKIE_NAME = "sessionid"
CSRF_COOKIE_NAME = "csrftoken"
# auth_cache shares this OPTIONS dict; test sessions skip the WAL flush wait on commit.
_session = cast("dict[str, object]", DATABASES["default"]["OPTIONS"])
_session["options"] = f"{_session['options']} -c synchronous_commit=off"
