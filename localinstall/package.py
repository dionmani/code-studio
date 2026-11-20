"""The Windows "Run Locally" package, built from the running server's own source.

Zip layout::

    Install Code Studio.cmd   double-click after "Extract All"
    README.txt
    install-info.json         where updates come from + this copy's download token
    app/                      the source (no secrets, data or environments)
    app/BUILD_INFO.json       the build this package contains
"""
from __future__ import annotations

import json
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from django.core import signing

from .version import BUILD_INFO_NAME, base_dir, build_info, iter_source_files

TOKEN_SALT = "codestudio.localinstall.download"
WINDOWS_DIR = Path(__file__).resolve().parent / "windows"
CRLF_SUFFIXES = (".cmd", ".bat")  # cmd.exe misparses LF-only batch files


def make_download_token(user) -> str:
    """A token that lets a local copy download future packages as ``user``.

    It only authorises the package download, and stops working when the user
    is deactivated or deleted (or the server's secret key changes).
    """
    return signing.dumps({"u": user.pk}, salt=TOKEN_SALT, compress=True)


def user_for_token(token: str):
    from django.contrib.auth import get_user_model

    try:
        data = signing.loads(token, salt=TOKEN_SALT)
    except signing.BadSignature:
        return None
    user = get_user_model().objects.filter(pk=data.get("u"), is_active=True).first()
    return user


def _bytes_for(name: str, path: Path) -> bytes:
    data = path.read_bytes()
    if name.lower().endswith(CRLF_SUFFIXES):
        data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    return data


def package_name(info: dict | None = None) -> str:
    info = info or build_info()
    return f"CodeStudio-{info['version']}-{info['build']}-windows.zip"


def build_package(server_url: str, token: str) -> tempfile.SpooledTemporaryFile:
    """Return a rewound file object holding the zip."""
    info = dict(build_info())
    info["built_at"] = info.get("built_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    install_info = {"server": server_url.rstrip("/"), "token": token,
                    "version": info["version"], "build": info["build"]}

    out = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("Install Code Studio.cmd", _bytes_for(".cmd", WINDOWS_DIR / "Install Code Studio.cmd"))
        zf.writestr("README.txt", (WINDOWS_DIR / "README.txt").read_bytes().replace(b"\r\n", b"\n")
                    .replace(b"\n", b"\r\n"))
        zf.writestr("install-info.json", json.dumps(install_info, indent=2))
        for rel, path in iter_source_files(base_dir()):
            zf.writestr(f"app/{rel}", _bytes_for(rel, path))
        zf.writestr(f"app/{BUILD_INFO_NAME}", json.dumps(info, indent=2))
    out.seek(0)
    return out
