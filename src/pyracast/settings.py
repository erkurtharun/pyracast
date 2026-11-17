import os
import re
from datetime import time
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

from pyracast.environment import load_environment

load_environment()

_cutoff = os.environ.get("PYRACAST_CUTOFF_LOCAL", "16:00")
if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", _cutoff):
    raise ImproperlyConfigured("PYRACAST_CUTOFF_LOCAL must be a valid HH:MM time.")
PYRACAST_CUTOFF_LOCAL = time(int(_cutoff[:2]), int(_cutoff[3:]))
PYRACAST_WEATHER_ENABLED = os.environ.get("PYRACAST_WEATHER_ENABLED", "1") == "1"

SECRET_KEY = os.environ.get("PYRACAST_SECRET_KEY", "")
SECRET_KEY_FALLBACKS = os.environ.get("PYRACAST_SECRET_KEY_FALLBACKS", "").split()
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
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.microsoft",
    "allauth.mfa",
    "allauth.headless",
    "allauth.usersessions",
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
    "pyracast.change_requests",
]
MIDDLEWARE = [
    "pyracast.accounts.middleware.TrustedProxyHeaders",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "pyracast.api.middleware.PrivateAPIResponses",
    "pyracast.accounts.session_lock.SessionRequestSerialization",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "pyracast.accounts.middleware.SessionLifetime",
    "pyracast.accounts.language.RequestLanguage",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "pyracast.accounts.middleware.PrivilegedAuthenticatorEnrollment",
    "allauth.account.middleware.AccountMiddleware",
    "allauth.usersessions.middleware.UserSessionsMiddleware",
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
        "OPTIONS": {
            "connect_timeout": 5,
            "options": "-c lock_timeout=5s -c statement_timeout=60s",
        },
    }
}
DATABASES["auth_cache"] = {
    **DATABASES["default"],
    "ATOMIC_REQUESTS": False,
    "TEST": {"MIRROR": "default"},
}
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [Path(__file__).resolve().parent / "accounts" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTHENTICATION_BACKENDS = ["pyracast.accounts.authentication.AuthenticationBackend"]
ACCOUNT_ADAPTER = "pyracast.accounts.adapters.AccountAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_SIGNUP_FORM_CLASS = "pyracast.accounts.forms.CompanySignupForm"
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_EMAIL_VERIFICATION_SUPPORTS_RESEND = 3
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED = True
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_TIMEOUT = 600
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_MAX_ATTEMPTS = 3
ACCOUNT_PASSWORD_RESET_BY_CODE_ENABLED = True
ACCOUNT_PASSWORD_RESET_BY_CODE_TIMEOUT = 600
ACCOUNT_PASSWORD_RESET_BY_CODE_MAX_ATTEMPTS = 3
ACCOUNT_LOGIN_BY_CODE_TIMEOUT = 180
ACCOUNT_EMAIL_SUBJECT_PREFIX = ""
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_CHANGE_EMAIL = True
ACCOUNT_PREVENT_ENUMERATION = True
ACCOUNT_EMAIL_NOTIFICATIONS = True
ACCOUNT_REAUTHENTICATION_REQUIRED = True
ACCOUNT_REAUTHENTICATION_TIMEOUT = 300
ACCOUNT_REAUTHENTICATION_METHODS = {"password", "code", "mfa"}
SOCIALACCOUNT_ADAPTER = "pyracast.accounts.social.SocialAccountAdapter"
SOCIALACCOUNT_AUTO_SIGNUP = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = False
SOCIALACCOUNT_REAUTHENTICATION_REQUIRED = True
SOCIALACCOUNT_STORE_TOKENS = False
SOCIALACCOUNT_PROVIDERS: dict[str, dict[str, object]] = {}
for _provider in ("google", "microsoft"):
    _client_id = os.environ.get(f"PYRACAST_{_provider.upper()}_CLIENT_ID", "").strip()
    _client_secret = os.environ.get(f"PYRACAST_{_provider.upper()}_CLIENT_SECRET", "").strip()
    if bool(_client_id) != bool(_client_secret):
        raise ImproperlyConfigured(f"Both {_provider} client ID and secret are required.")
    if _client_id:
        SOCIALACCOUNT_PROVIDERS[_provider] = {
            "APP": {"client_id": _client_id, "secret": _client_secret},
            "SCOPE": ["openid", "email"] if _provider == "google" else ["User.Read"],
            "OAUTH_PKCE_ENABLED": True,
            "VERIFIED_EMAIL": False,
        }
ACCOUNT_SESSION_REMEMBER = False
ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE = True
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = False
ACCOUNT_LOGIN_ON_PASSWORD_RESET = False
PASSWORD_RESET_TIMEOUT = 3600
HEADLESS_ONLY = True
HEADLESS_CLIENTS = ("browser",)
PYRACAST_PUBLIC_ORIGIN = os.environ.get("PYRACAST_PUBLIC_ORIGIN", "http://127.0.0.1:3000").rstrip(
    "/"
)
HEADLESS_FRONTEND_URLS = {
    "account_login": f"{PYRACAST_PUBLIC_ORIGIN}/app/",
    "account_confirm_email": f"{PYRACAST_PUBLIC_ORIGIN}/app/",
    "account_reset_password": f"{PYRACAST_PUBLIC_ORIGIN}/app/",
    "account_reset_password_from_key": f"{PYRACAST_PUBLIC_ORIGIN}/app/",
    "account_signup": f"{PYRACAST_PUBLIC_ORIGIN}/app/?signup=1",
    "socialaccount_login_error": f"{PYRACAST_PUBLIC_ORIGIN}/app/?social_callback=1",
}
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_ADAPTER = "pyracast.accounts.mfa.MFAAdapter"
MFA_REAUTHENTICATION_REQUIRED = True
MFA_RECOVERY_CODES_SHOW_ONCE = True
MFA_TOTP_ISSUER = "Pyracast"
USERSESSIONS_TRACK_ACTIVITY = True
ACCOUNT_RATE_LIMITS = {
    "login": "20/m/ip",
    "login_failed": "10/10m/ip,5/10m/key",
    "signup": "5/h/ip",
    "reset_password": "5/h/ip,3/h/key",
    "reset_password_from_key": "10/h/ip",
    "confirm_email": "1/1m/key",
}
CACHES = {
    "default": {
        "BACKEND": "pyracast.accounts.cache.AuthenticationCache",
        "LOCATION": "pyracast_authentication_cache",
        "KEY_PREFIX": "pyracast-auth",
    }
}
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "pyracast.api.permissions.WorkspaceWritePermission",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_RENDERER_CLASSES": ["pyracast.api.renderers.CustomerJSONRenderer"],
    "EXCEPTION_HANDLER": "pyracast.api.errors.exception_handler",
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Pyracast API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "PREPROCESSING_HOOKS": ["pyracast.api.schema.customer_endpoints"],
    "ENUM_NAME_OVERRIDES": {
        "PowerBasisEnum": [("ac", "ac"), ("dc", "dc")],
        "CalibrationStatusEnum": "pyracast.plants.serializers.calibration_status_choices",
        "EpiasSourceKindEnum": "pyracast.metering.history.PRODUCTION_SOURCES",
        "ForecastSourceKindEnum": ["forecast", "estimated_actuals", "mixed"],
        "ProductionQualityEnum": "pyracast.plants.models.OBSERVATION_QUALITIES",
        "ChangeRequestStatusEnum": ["pending", "approved", "rejected", "withdrawn"],
        "ChangeRequestKindEnum": ["production", "incident"],
        "IncidentKindEnum": ["inverter", "panels", "grid", "other", "multiple"],
        "IncidentChangeActionEnum": ["create", "update", "resolve", "cancel"],
        "ProductionChangeActionEnum": ["set", "revert"],
    },
}
LANGUAGE_CODE = "en"
LANGUAGES = (("en", "English"), ("tr", "Türkçe"))
USE_I18N = True
TIME_ZONE = "UTC"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
STATIC_ROOT = os.environ.get("PYRACAST_STATIC_ROOT", "staticfiles")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_NAME = "sessionid" if DEBUG else "__Host-pyracast-sessionid"
CSRF_COOKIE_NAME = "csrftoken" if DEBUG else "__Host-pyracast-csrftoken"
SESSION_COOKIE_AGE = 12 * 60 * 60
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
PYRACAST_SESSION_IDLE_SECONDS = 30 * 60
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.NONCE],
    "style-src": [CSP.SELF, CSP.NONCE],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
    "form-action": [CSP.SELF],
}
SECURE_SSL_REDIRECT = not DEBUG
SECURE_REDIRECT_EXEMPT = [r"^health/(live|ready)/$"]
PYRACAST_TRUST_PROXY = os.environ.get("PYRACAST_TRUST_PROXY") == "1"
PYRACAST_TRUSTED_PROXY_CIDRS = os.environ.get("PYRACAST_TRUSTED_PROXY_CIDRS", "").split()
PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS = (
    os.environ.get("PYRACAST_TRUSTED_UPSTREAM_PROXY_CIDRS", "").replace(",", " ").split()
)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if PYRACAST_TRUST_PROXY else None
ALLAUTH_TRUSTED_CLIENT_IP_HEADER = "X-Real-IP" if PYRACAST_TRUST_PROXY else None
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
LOGIN_URL = "/app/"
LOGIN_REDIRECT_URL = "/app/"
_email_backend = os.environ.get("PYRACAST_EMAIL_BACKEND", "smtp")
if _email_backend not in {"smtp", "console", "gmail"}:
    raise ImproperlyConfigured("PYRACAST_EMAIL_BACKEND must be smtp, console or gmail.")
if _email_backend == "console" and not DEBUG:
    raise ImproperlyConfigured("PYRACAST_EMAIL_BACKEND=console requires PYRACAST_DEBUG=1.")
DEFAULT_FROM_EMAIL = os.environ.get("PYRACAST_FROM_EMAIL", "Pyracast <noreply@localhost>")
if _email_backend == "smtp":
    try:
        _email_port = int(os.environ.get("PYRACAST_EMAIL_PORT", "587"))
    except ValueError:
        raise ImproperlyConfigured("PYRACAST_EMAIL_PORT must be an integer.") from None
    _email_ssl = os.environ.get("PYRACAST_EMAIL_SSL", "0") == "1"
    _mailer: dict[str, object] = {
        "BACKEND": "pyracast.mail.smtp.EmailBackend",
        "OPTIONS": {
            "host": os.environ.get("PYRACAST_EMAIL_HOST", ""),
            "port": _email_port,
            "username": os.environ.get("PYRACAST_EMAIL_USER", ""),
            "password": os.environ.get("PYRACAST_EMAIL_PASSWORD", ""),
            "use_tls": os.environ.get("PYRACAST_EMAIL_TLS", "0" if _email_ssl else "1") == "1",
            "use_ssl": _email_ssl,
            "timeout": 15,
        },
    }
elif _email_backend == "gmail":
    _mailer = {
        "BACKEND": "pyracast.mail.gmail.EmailBackend",
        "OPTIONS": {
            "client_id": os.environ.get("PYRACAST_GMAIL_CLIENT_ID", ""),
            "client_secret": os.environ.get("PYRACAST_GMAIL_CLIENT_SECRET", ""),
            "refresh_token": os.environ.get("PYRACAST_GMAIL_REFRESH_TOKEN", ""),
            "timeout": 15,
        },
    }
else:
    _mailer = {"BACKEND": "django.core.mail.backends.console.EmailBackend"}
MAILERS = {"default": _mailer}
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"standard": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "stream": "ext://sys.stdout",
        },
        "discard": {"class": "logging.NullHandler"},
    },
    "loggers": {
        "pyracast": {
            "handlers": ["console"],
            "level": os.environ.get("PYRACAST_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.security.DisallowedHost": {"handlers": ["discard"], "propagate": False},
    },
}
