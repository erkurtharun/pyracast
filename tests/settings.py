from pyracast.settings import *  # noqa: F403 -- Django settings overlay.

SECRET_KEY = "test-only-not-a-deployment-secret"  # noqa: S105
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
SECURE_SSL_REDIRECT = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
