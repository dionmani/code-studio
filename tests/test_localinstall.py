import io
import json
import zipfile

import httpx
import pytest

from localinstall import package, version, views


@pytest.fixture(autouse=True)
def fresh_build_info():
    version.build_info.cache_clear()
    yield
    version.build_info.cache_clear()


def _tree(tmp_path):
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_bytes(b"print('hi')\n")
    (root / "VERSION").write_text("2.3.4\n")
    for junk in (".venv/lib/x.py", ".venv-linux/bin/x", "pkg/__pycache__/mod.pyc", "staticfiles/a.css",
                 "node_modules/m/index.js", ".git/HEAD"):
        p = root / junk
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("junk")
    (root / ".env").write_text("SECRET=1")
    (root / "id_rsa").write_text("key")
    (root / "db.sqlite3").write_text("db")
    return root


def test_source_files_skip_environments_caches_and_secrets(tmp_path):
    root = _tree(tmp_path)
    assert [rel for rel, _ in version.iter_source_files(root)] == ["VERSION", "pkg/mod.py"]


def test_fingerprint_tracks_content_but_not_line_endings(tmp_path):
    root = _tree(tmp_path)
    first = version.fingerprint(root)
    (root / "pkg" / "mod.py").write_bytes(b"print('hi')\r\n")
    assert version.fingerprint(root) == first
    (root / ".env").write_text("SECRET=2")  # excluded files don't count
    assert version.fingerprint(root) == first
    (root / "pkg" / "mod.py").write_bytes(b"print('changed')\n")
    assert version.fingerprint(root) != first


def test_build_info_prefers_the_stamped_file(tmp_path, settings):
    root = _tree(tmp_path)
    settings.BASE_DIR = root
    computed = version.build_info()
    assert computed["version"] == "2.3.4" and computed["build"] == version.fingerprint(root)
    version.build_info.cache_clear()
    (root / "BUILD_INFO.json").write_text(json.dumps({"version": "2.3.4", "build": "abc123def456"}))
    assert version.build_info()["build"] == "abc123def456"


def test_version_endpoint_is_public(client):
    data = client.get("/api/version").json()
    assert data["build"] == version.build_info()["build"] and data["local_install"] is False


def test_local_page_requires_sign_in_and_offers_download(client, admin_user):
    assert client.get("/local/").status_code == 302
    client.force_login(admin_user)
    html = client.get("/local/").content.decode()
    assert 'id="download-btn"' in html and version.build_info()["build"] in html


def _download(client, **headers):
    resp = client.get("/local/download", **headers)
    body = b"".join(resp.streaming_content) if resp.status_code == 200 else resp.content
    return resp, body


@pytest.mark.django_db
def test_download_needs_a_session_or_valid_token(client, admin_user):
    resp, _ = _download(client)
    assert resp.status_code == 401
    resp, _ = _download(client, HTTP_AUTHORIZATION="Bearer not-a-token")
    assert resp.status_code == 401


def test_download_package_contents(client, admin_user):
    client.force_login(admin_user)
    resp, body = _download(client)
    assert resp.status_code == 200
    assert resp["Content-Disposition"].startswith('attachment; filename="CodeStudio-')
    zf = zipfile.ZipFile(io.BytesIO(body))
    names = set(zf.namelist())
    assert {"Install Code Studio.cmd", "README.txt", "install-info.json", "app/manage.py", "app/BUILD_INFO.json",
            "app/localinstall/windows/install.ps1", "app/localinstall/windows/common.ps1"} <= names
    assert not [n for n in names if "/.venv" in n or "__pycache__" in n or n.endswith((".env", ".sqlite3"))]
    assert b"\r\n" in zf.read("Install Code Studio.cmd") and b"\n" not in zf.read("Install Code Studio.cmd").replace(b"\r\n", b"")

    info = json.loads(zf.read("install-info.json"))
    assert info["server"] == "http://testserver" and info["build"] == version.build_info()["build"]
    assert json.loads(zf.read("app/BUILD_INFO.json"))["build"] == info["build"]
    assert package.user_for_token(info["token"]) == admin_user

    # the token alone (no session) is enough for an update download
    client.logout()
    resp, body = _download(client, HTTP_AUTHORIZATION=f"Bearer {info['token']}")
    assert resp.status_code == 200 and zipfile.ZipFile(io.BytesIO(body)).testzip() is None

    # ... until the account is deactivated
    admin_user.is_active = False
    admin_user.save()
    resp, _ = _download(client, HTTP_AUTHORIZATION=f"Bearer {info['token']}")
    assert resp.status_code == 401


def test_local_copy_does_not_serve_packages(client_admin, settings):
    settings.CODE_STUDIO["LOCAL_INSTALL"] = True
    resp, _ = _download(client_admin)
    assert resp.status_code == 404


def test_update_status_on_server_copy_is_disabled(client_admin):
    assert client_admin.get("/api/local/update-status").json()["enabled"] is False


def test_update_status_compares_builds(client_admin, settings, monkeypatch):
    settings.CODE_STUDIO.update(LOCAL_INSTALL=True, UPDATE_SERVER="http://server:8197")
    seen = {}

    def fake_get(url, timeout):
        seen["url"] = url
        return httpx.Response(200, json={"version": "9.9.9", "build": "fffffffffff0"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(views.httpx, "get", fake_get)
    data = client_admin.get("/api/local/update-status").json()
    assert seen["url"] == "http://server:8197/api/version"
    assert data["update_available"] is True and data["server"]["build"] == "fffffffffff0"

    monkeypatch.setattr(views.httpx, "get", lambda url, timeout: httpx.Response(
        200, json=version.build_info(), request=httpx.Request("GET", url)))
    assert client_admin.get("/api/local/update-status").json()["update_available"] is False

    def offline(url, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(views.httpx, "get", offline)
    data = client_admin.get("/api/local/update-status").json()
    assert data["update_available"] is False and "Could not reach" in data["error"]
    html = client_admin.get("/local/").content.decode()
    assert "This is your local copy" in html and 'id="download-btn"' not in html


def test_update_status_requires_sign_in(client):
    assert client.get("/api/local/update-status").status_code == 401


def test_ide_header_links(client_admin, settings):
    settings.CODE_STUDIO.update(OFFER_LOCAL_INSTALL=True, LOCAL_INSTALL=False)
    html = client_admin.get("/").content.decode()
    assert 'id="run-locally"' in html and 'id="update-badge"' not in html
    settings.CODE_STUDIO.update(OFFER_LOCAL_INSTALL=False, LOCAL_INSTALL=True)
    html = client_admin.get("/").content.decode()
    assert 'id="run-locally"' not in html and 'id="update-badge"' in html and 'data-local-install="1"' in html


@pytest.mark.django_db
def test_bootstrap_reads_password_from_environment(monkeypatch):
    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    monkeypatch.setenv("CODE_STUDIO_BOOTSTRAP_PASSWORD", "from-env-pw-123")
    call_command("bootstrap", "--first-run-only", "--username", "local")
    assert get_user_model().objects.get(username="local").check_password("from-env-pw-123")
