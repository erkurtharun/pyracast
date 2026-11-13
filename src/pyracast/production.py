from django.core.exceptions import ImproperlyConfigured

from pyracast import settings as base
from pyracast.settings import *  # noqa: F403 -- Deployment settings overlay.

if base.DEBUG:
    raise ImproperlyConfigured("Production settings require PYRACAST_DEBUG=0.")

MIDDLEWARE = [
    *base.MIDDLEWARE[:2],
    "whitenoise.middleware.WhiteNoiseMiddleware",
    *base.MIDDLEWARE[2:],
]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
