from pyracast.settings import *  # noqa: F403 -- Django settings overlay.

SECRET_KEY = "test-only-not-a-deployment-secret"  # noqa: S105
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
SECURE_SSL_REDIRECT = False
PYRACAST_PUBLIC_ORIGIN = "https://testserver"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
SESSION_COOKIE_NAME = "sessionid"
CSRF_COOKIE_NAME = "csrftoken"
