"""Django settings for the Code Studio IDE service.

Configuration is via environment variables (all optional for local dev):

  CODE_STUDIO_DATA_DIR      internal data dir (default %LOCALAPPDATA%\\CodeStudio)
  CODE_STUDIO_DEBUG         "1" (default) / "0"
  CODE_STUDIO_SECRET_KEY    Django secret key (default: generated file in data dir)
  CODE_STUDIO_ALLOWED_HOSTS comma separated (default 127.0.0.1,localhost)
  CODE_STUDIO_ENGINE_URL    Agent Engine base URL (default http://127.0.0.1:8765)
  CODE_STUDIO_ENGINE_TOKEN  shared service token (default: token file in data dir)
  CODE_STUDIO_CSRF_TRUSTED_ORIGINS  e.g. http://192.168.1.12:8197 (needed behind Docker port maps)
  CODE_STUDIO_SECURE_COOKIES        "1" (default when DEBUG=0) / "0" for plain-HTTP LAN access
  CODE_STUDIO_STATIC_ROOT           collectstatic target (Docker image: /app/staticfiles)
  CODE_STUDIO_LOCAL_INSTALL         "1" when started by the Windows "Run Locally" launcher
  CODE_STUDIO_UPDATE_SERVER         a local copy's server, checked for updates (set by the launcher)
  CODE_STUDIO_OFFER_LOCAL_INSTALL   show "Run Locally" (default: on when DEBUG=0 and not a local copy)
  CODE_STUDIO_SYSTEM_TERMINAL       "1" enables the unrestricted System terminal for loopback clients
                                    (default: on for a local copy, off otherwise)
"""
from pathlib import Path
import os

from cs_common.datadir import default_data_dir, ensure_dir, read_or_create_secret

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ensure_dir(default_data_dir())

DEBUG = os.environ.get("CODE_STUDIO_DEBUG", "1") == "1"
SECRET_KEY = os.environ.get("CODE_STUDIO_SECRET_KEY") or read_or_create_secret(DATA_DIR / "django_secret", 48)
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("CODE_STUDIO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("CODE_STUDIO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

INSTALLED_APPS = [
    "daphne",  # ASGI runserver (WebSockets) for `manage.py runserver`
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "core",
    "audit",
    "projects",
    "files",
    "vcs",
    "terminal",
    "agents_client",
    "localinstall",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "codestudio.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "codestudio.wsgi.application"
ASGI_APPLICATION = "codestudio.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("CODE_STUDIO_DB_PATH", str(DATA_DIR / "db.sqlite3")),
        "OPTIONS": {"timeout": 20},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = Path(os.environ.get("CODE_STUDIO_STATIC_ROOT") or DATA_DIR / "staticfiles")

# With DEBUG off (Docker), static files are served by WhiteNoise from STATIC_ROOT
# (populated by `collectstatic` at image build). Optional for local dev.
if not DEBUG:
    try:
        import whitenoise  # noqa: F401
    except ImportError:  # pragma: no cover
        pass
    else:
        MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# Session / CSRF hardening. The CSRF token is embedded in the page, so the
# cookie can be HttpOnly.
# Browsers scope cookies by host, not port: other Django apps on the same
# server (e.g. 192.168.1.12:18080) would overwrite the default "csrftoken" /
# "sessionid" cookies and cause CSRF failures or random sign-outs.
SESSION_COOKIE_NAME = os.environ.get("CODE_STUDIO_SESSION_COOKIE", "codestudio_sessionid")
CSRF_COOKIE_NAME = os.environ.get("CODE_STUDIO_CSRF_COOKIE", "codestudio_csrftoken")
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 12
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
if not DEBUG:
    SESSION_COOKIE_SECURE = os.environ.get("CODE_STUDIO_SECURE_COOKIES", "1") == "1"
    CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE

CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# JSON bodies up to 25 MB (core.api.json_body enforces the same cap): task images travel as base64.
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024

# ---- Code Studio specific -------------------------------------------------
CODE_STUDIO = {
    "ENGINE_URL": os.environ.get("CODE_STUDIO_ENGINE_URL", "http://127.0.0.1:8765").rstrip("/"),
    "ENGINE_TIMEOUT": float(os.environ.get("CODE_STUDIO_ENGINE_TIMEOUT", "10")),
    "MAX_EDIT_BYTES": 5 * 1024 * 1024,
    "SNAPSHOT_RETENTION_COUNT": 100,
    "SNAPSHOT_RETENTION_DAYS": 30,
    "SNAPSHOT_MAX_BYTES": 50 * 1024 * 1024,
    "TERMINAL_COMMAND_TIMEOUT": 600,
    "TERMINAL_IDLE_TIMEOUT": 30 * 60,
    "TERMINAL_MAX_OUTPUT": 5 * 1024 * 1024,
    "SEARCH_MAX_RESULTS": 500,
    # Run Locally (localinstall/): a server offers the Windows package, a local copy checks it for updates.
    "LOCAL_INSTALL": os.environ.get("CODE_STUDIO_LOCAL_INSTALL", "0") == "1",
    "UPDATE_SERVER": os.environ.get("CODE_STUDIO_UPDATE_SERVER", "").rstrip("/"),
}
CODE_STUDIO["OFFER_LOCAL_INSTALL"] = os.environ.get(
    "CODE_STUDIO_OFFER_LOCAL_INSTALL", "1" if not DEBUG and not CODE_STUDIO["LOCAL_INSTALL"] else "0") == "1"
# Unrestricted interactive shell (terminal/system.py). Full user-level access, so it is
# on by default only for a local copy and always refuses non-loopback clients.
CODE_STUDIO["SYSTEM_TERMINAL"] = os.environ.get(
    "CODE_STUDIO_SYSTEM_TERMINAL", "1" if CODE_STUDIO["LOCAL_INSTALL"] else "0") == "1"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}
