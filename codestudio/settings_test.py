"""Test settings: isolated data dir + fixed service token (set before import)."""
import os
import tempfile

os.environ.setdefault("CODE_STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="cs-test-data-"))
os.environ.setdefault("CODE_STUDIO_ENGINE_TOKEN", "test-engine-token-0123456789")
os.environ.setdefault("CODE_STUDIO_ENGINE_URL", "http://127.0.0.1:1")  # offline unless a test starts one

from .settings import *  # noqa: E402,F401,F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CODE_STUDIO = dict(CODE_STUDIO)  # noqa: F405
