from django.core import exceptions as _exceptions

from pyracast import settings as base

# noinspection PyUnusedImports
from pyracast.settings import *  # noqa: F403 -- Django loads the re-exported settings.

if base.DEBUG:
    raise _exceptions.ImproperlyConfigured("Production settings require PYRACAST_DEBUG=0.")

MIDDLEWARE = [
    *base.MIDDLEWARE[:2],
    "whitenoise.middleware.WhiteNoiseMiddleware",
    *base.MIDDLEWARE[2:],
]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
