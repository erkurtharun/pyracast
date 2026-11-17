import os
from typing import cast

# Fixed before the settings import, which loads .env without overriding, so neither the shell nor
# a local .env changes test results. PG*, the RabbitMQ URL and the live-test gates stay untouched.
os.environ.update(
    PYRACAST_DEBUG="0",
    PYRACAST_HOSTS="localhost,127.0.0.1",
    PYRACAST_CSRF_ORIGINS="",
    PYRACAST_PUBLIC_ORIGIN="http://127.0.0.1:3000",
    PYRACAST_CUTOFF_LOCAL="16:00",
    PYRACAST_WEATHER_ENABLED="1",
    PYRACAST_TRUST_PROXY="0",
    PYRACAST_TRUSTED_PROXY_CIDRS="",
    PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS="",
    PYRACAST_SECRET_KEY_FALLBACKS="",
    PYRACAST_GOOGLE_CLIENT_ID="",
    PYRACAST_GOOGLE_CLIENT_SECRET="",
    PYRACAST_MICROSOFT_CLIENT_ID="",
    PYRACAST_MICROSOFT_CLIENT_SECRET="",
    PYRACAST_STATIC_ROOT="staticfiles",
    PYRACAST_EMAIL_BACKEND="smtp",
    PYRACAST_FROM_EMAIL="Pyracast <noreply@localhost>",
    PYRACAST_EMAIL_HOST="localhost",
    PYRACAST_EMAIL_PORT="587",
    PYRACAST_EMAIL_USER="",
    PYRACAST_EMAIL_PASSWORD="",
    PYRACAST_EMAIL_TLS="1",
    PYRACAST_EMAIL_SSL="0",
    PYRACAST_GMAIL_CLIENT_ID="",
    PYRACAST_GMAIL_CLIENT_SECRET="",
    PYRACAST_GMAIL_REFRESH_TOKEN="",
    PYRACAST_LOG_LEVEL="INFO",
)

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
