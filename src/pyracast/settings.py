import os
import re
from datetime import time

from django.core.exceptions import ImproperlyConfigured

from pyracast.environment import load_environment

load_environment()

_cutoff = os.environ.get("PYRACAST_CUTOFF_LOCAL", "16:00")
if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", _cutoff):
    raise ImproperlyConfigured("PYRACAST_CUTOFF_LOCAL must be a valid HH:MM time.")
PYRACAST_CUTOFF_LOCAL = time(int(_cutoff[:2]), int(_cutoff[3:]))

SECRET_KEY = os.environ.get("PYRACAST_SECRET_KEY", "")
DEBUG = os.environ.get("PYRACAST_DEBUG") == "1"
ALLOWED_HOSTS = os.environ.get("PYRACAST_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = os.environ.get("PYRACAST_CSRF_ORIGINS", "").split()
ROOT_URLCONF = "pyracast.urls"
WSGI_APPLICATION = "pyracast.wsgi.application"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",
    "django.contrib.postgres",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "rest_framework",
    "drf_spectacular",
    "pyracast.accounts",
    "pyracast.regions",
    "pyracast.connections",
    "pyracast.forecasts",
    "pyracast.plants",
    "pyracast.incidents",
    "pyracast.notifications",
    "pyracast.metering",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "pyracast.api.middleware.PrivateAPIResponses",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGDATABASE", "pyracast"),
        "USER": os.environ.get("PGUSER", "pyracast"),
        "PASSWORD": os.environ.get("PGPASSWORD", ""),
        "HOST": os.environ.get("PGHOST", "127.0.0.1"),
        "PORT": os.environ.get("PGPORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "EXCEPTION_HANDLER": "pyracast.api.errors.exception_handler",
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Pyracast API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "ENUM_NAME_OVERRIDES": {
        "PowerBasisEnum": [("ac", "ac"), ("dc", "dc")],
        "CalibrationStatusEnum": "pyracast.plants.serializers.calibration_status_choices",
        "EpiasSourceKindEnum": "pyracast.plants.epias_api.SOURCES",
        "ForecastSourceKindEnum": ["forecast", "estimated_actuals"],
    },
}
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
STATIC_ROOT = os.environ.get("PYRACAST_STATIC_ROOT", "staticfiles")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = not DEBUG
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https") if os.environ.get("PYRACAST_TRUST_PROXY") == "1" else None
)
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
LOGIN_REDIRECT_URL = "/api/tenants/"
MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "OPTIONS": {
            "host": os.environ.get("PYRACAST_EMAIL_HOST", "localhost"),
            "port": int(os.environ.get("PYRACAST_EMAIL_PORT", "587")),
            "username": os.environ.get("PYRACAST_EMAIL_USER", ""),
            "password": os.environ.get("PYRACAST_EMAIL_PASSWORD", ""),
            "use_tls": os.environ.get("PYRACAST_EMAIL_TLS", "1") == "1",
            "use_ssl": os.environ.get("PYRACAST_EMAIL_SSL", "0") == "1",
            "timeout": 15,
        },
    },
}
DEFAULT_FROM_EMAIL = os.environ.get("PYRACAST_FROM_EMAIL", "Pyracast <noreply@localhost>")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"standard": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "standard"}},
    "loggers": {"pyracast": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}
