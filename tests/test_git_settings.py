"""Git: Windows (CRLF) checkouts, saved HTTPS logins, push/pull auth, access check."""
import base64
import json
import threading
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest
from dulwich import porcelain
from dulwich.repo import Repo

from vcs.models import GitCredential
from vcs.secrets import SealError, seal, unseal

from .conftest import init_git_repo


def j(client, method, url, body=None):
    return getattr(client, method.lower())(url, data=json.dumps(body or {}), content_type="application/json")


def _crlf_checkout(path):
    for name in ("README.md", "main.py"):
        f = path / name
        f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))


# ---- line endings -------------------------------------------------------------

@pytest.mark.django_db
def test_windows_checkout_is_not_reported_as_modified(client_admin, git_project, workroot):
    repo = workroot / "repo"
    _crlf_checkout(repo)
    st = client_admin.get(f"/api/projects/{git_project.id}/git/status").json()
    assert st["unstaged"] == [] and st["clean"], st
    assert st["line_endings"]["effective"] == "crlf" and "detected" in st["line_endings"]["source"]

    # forcing "as stored" shows the raw difference again
    assert j(client_admin, "PATCH", f"/api/projects/{git_project.id}/git/settings", {"line_endings": "lf"}).status_code == 200
    st = client_admin.get(f"/api/projects/{git_project.id}/git/status").json()
    assert {u["path"] for u in st["unstaged"]} == {"README.md", "main.py"}
    # the repository's own config is never modified
    assert b"autocrlf" not in (repo / ".git" / "config").read_bytes()


@pytest.mark.django_db
def test_repo_config_wins_in_auto_mode(client_admin, git_project, workroot):
    repo = workroot / "repo"
    _crlf_checkout(repo)
    cfg = Repo(str(repo)).get_config()
    cfg.set((b"core",), b"autocrlf", b"false")
    cfg.write_to_path()
    st = client_admin.get(f"/api/projects/{git_project.id}/git/status").json()
    assert st["line_endings"]["effective"] == "lf" and "repository config" in st["line_endings"]["source"]
    assert len(st["unstaged"]) == 2


@pytest.mark.django_db
def test_real_change_in_crlf_file_diffs_and_commits_as_lf(client_admin, git_project, workroot):
    repo = workroot / "repo"
    _crlf_checkout(repo)
    (repo / "main.py").write_bytes(b"x = 2\r\n")
    st = client_admin.get(f"/api/projects/{git_project.id}/git/status").json()
    assert [u["path"] for u in st["unstaged"]] == ["main.py"]
    d = client_admin.get(f"/api/projects/{git_project.id}/git/diff?path=main.py").json()
    assert "-x = 1\n+x = 2\n" in d["diff"] and "\r" not in d["diff"]
    assert j(client_admin, "POST", f"/api/projects/{git_project.id}/git/stage", {"paths": ["main.py"]}).status_code == 200
    r = j(client_admin, "POST", f"/api/projects/{git_project.id}/git/commit", {"message": "crlf edit"})
    assert r.status_code == 200, r.content
    rr = Repo(str(repo))
    from dulwich.object_store import tree_lookup_path
    _m, sha = tree_lookup_path(rr.__getitem__, rr[rr.head()].tree, b"main.py")
    assert rr[sha].data == b"x = 2\n"           # stored with LF, like Git on Windows would
    assert r.json()["status"]["clean"]


@pytest.mark.django_db
def test_line_ending_setting_is_staff_only(client, plain_user, git_project):
    git_project.members.add(plain_user)
    client.force_login(plain_user)
    assert client.get(f"/api/projects/{git_project.id}/git/settings").status_code == 200
    assert j(client, "PATCH", f"/api/projects/{git_project.id}/git/settings", {"line_endings": "lf"}).status_code == 403


# ---- credentials ----------------------------------------------------------------

def test_seal_roundtrip_and_tamper_detection(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    s = seal("ghp_exampletoken1234567890")
    assert "exampletoken" not in s and unseal(s) == "ghp_exampletoken1234567890"
    raw = bytearray(base64.urlsafe_b64decode(s))
    raw[20] ^= 1
    with pytest.raises(SealError):
        unseal(base64.urlsafe_b64encode(bytes(raw)).decode())


@pytest.mark.django_db
def test_credentials_are_write_only_and_per_user(client, admin_user, plain_user):
    client.force_login(admin_user)
    r = j(client, "POST", "/api/git/credentials", {"host": "https://GitHub.com/", "username": "dion", "token": "github_pat_SECRET1234567890"})
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["host"] == "github.com" and body["token_hint"] == "7890" and "SECRET" not in json.dumps(body)
    row = GitCredential.objects.get()
    assert "SECRET" not in row.token_sealed and unseal(row.token_sealed) == "github_pat_SECRET1234567890"
    assert "SECRET" not in client.get("/api/git/credentials").content.decode()
    # update username without retyping the token keeps the token
    assert j(client, "POST", "/api/git/credentials", {"host": "github.com", "username": "dion2", "token": ""}).status_code == 200
    row.refresh_from_db()
    assert row.username == "dion2" and unseal(row.token_sealed) == "github_pat_SECRET1234567890"
    # other users neither see nor delete it
    client.force_login(plain_user)
    assert client.get("/api/git/credentials").json()["credentials"] == []
    assert client.delete(f"/api/git/credentials/{row.id}").status_code == 404
    client.force_login(admin_user)
    assert client.delete(f"/api/git/credentials/{row.id}").status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("body", [
    {"host": "not a host", "username": "u", "token": "t"},
    {"host": "github.com", "username": "", "token": "t"},
    {"host": "github.com", "username": "u", "token": ""},
    {"host": "github.com", "username": "u", "token": "has space"},
])
def test_credential_validation(client_admin, body):
    assert j(client_admin, "POST", "/api/git/credentials", body).status_code == 400


@pytest.mark.django_db
def test_identity(client_admin, admin_user):
    assert j(client_admin, "PATCH", "/api/git/identity", {"name": "Dion M", "email": "bad"}).status_code == 400
    r = j(client_admin, "PATCH", "/api/git/identity", {"name": "Dion M", "email": "dion@example.com"})
    assert r.json()["effective"] == "Dion M <dion@example.com>"


# ---- push / pull / access check against a real authenticated Git HTTP server ----

class _QuietHandler(WSGIRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
def http_remote(tmp_path):
    """Smart-HTTP Git server (dulwich.web) requiring Basic auth user/s3cret-token."""
    from dulwich.server import DictBackend
    from dulwich.web import make_wsgi_chain

    bare = tmp_path / "remote.git"
    src = tmp_path / "seed"
    init_git_repo(src)
    porcelain.clone(str(src), str(bare), bare=True).close()
    app = make_wsgi_chain(DictBackend({"/": Repo(str(bare))}))
    expected = "Basic " + base64.b64encode(b"user:s3cret-token").decode()

    def auth(environ, start_response):
        if environ.get("HTTP_AUTHORIZATION") != expected:
            start_response("401 Unauthorized", [("WWW-Authenticate", 'Basic realm="git"'), ("Content-Length", "0")])
            return [b""]
        return app(environ, start_response)

    server = make_server("127.0.0.1", 0, auth, handler_class=_QuietHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_port}/", bare, src
    server.shutdown()


@pytest.fixture
def cloned_project(root, admin_user, workroot, http_remote):
    from projects.models import Project

    url, _bare, src = http_remote
    clone = workroot / "clone"
    porcelain.clone(str(src), str(clone)).close()
    r = Repo(str(clone))
    cfg = r.get_config()
    cfg.set((b"remote", b"origin"), b"url", url.encode())
    cfg.write_to_path()
    p = Project.objects.create(name="clone", root=root, relative_path="clone", git_mode="git", owner=admin_user)
    p.members.add(admin_user)
    return p, clone


@pytest.mark.django_db
def test_push_requires_saved_login_and_uses_it(client_admin, cloned_project, http_remote):
    project, clone = cloned_project
    _url, bare, _src = http_remote
    (clone / "new.txt").write_bytes(b"pushed\n")
    j(client_admin, "POST", f"/api/projects/{project.id}/git/stage", {"paths": ["new.txt"]})
    head = j(client_admin, "POST", f"/api/projects/{project.id}/git/commit", {"message": "to push"}).json()["commit"]
    push = lambda: j(client_admin, "POST", f"/api/projects/{project.id}/git/push", {"confirm": True})  # noqa: E731

    r = push()
    assert r.status_code == 400 and r.json()["code"] == "git_auth_missing", r.content
    j(client_admin, "POST", "/api/git/credentials", {"host": "127.0.0.1", "username": "user", "token": "wrong-token"})
    # the host includes the port for non-default ports
    host = f"127.0.0.1:{http_remote[0].split(':')[2].rstrip('/')}"
    j(client_admin, "POST", "/api/git/credentials", {"host": host, "username": "user", "token": "wrong-token"})
    r = push()
    assert r.status_code == 400 and r.json()["code"] == "git_auth_rejected", r.content
    assert "wrong-token" not in r.content.decode()

    j(client_admin, "POST", "/api/git/credentials", {"host": host, "username": "user", "token": "s3cret-token"})
    r = push()
    assert r.status_code == 200, r.content
    assert Repo(str(bare)).refs[b"refs/heads/master"].decode() == head
    assert GitCredential.objects.get(host=host).last_used_at is not None
    # the token never reaches the audit log
    from audit.models import AuditEvent
    assert not any("s3cret" in json.dumps(e.detail) for e in AuditEvent.objects.all())


@pytest.mark.django_db
def test_pull_with_saved_login(client_admin, cloned_project, http_remote):
    project, clone = cloned_project
    url, bare, src = http_remote
    host = url.split("//")[1].rstrip("/")
    j(client_admin, "POST", "/api/git/credentials", {"host": host, "username": "user", "token": "s3cret-token"})
    # someone else pushes to the remote
    (src / "upstream.txt").write_bytes(b"from upstream\n")
    porcelain.add(str(src), paths=[str(src / "upstream.txt")])
    porcelain.commit(str(src), message=b"upstream", author=b"U <u@x>", committer=b"U <u@x>")
    porcelain.push(str(src), str(bare), refspecs=[b"refs/heads/master"])
    r = j(client_admin, "POST", f"/api/projects/{project.id}/git/pull", {"confirm": True})
    assert r.status_code == 200 and r.json()["updated"], r.content
    # CRLF when the machine's global Git config has core.autocrlf=true (Git for Windows default)
    assert (clone / "upstream.txt").read_bytes().replace(b"\r\n", b"\n") == b"from upstream\n"


@pytest.mark.django_db
def test_check_access_reports_read_and_write(client_admin, cloned_project, http_remote):
    project, _clone = cloned_project
    url = http_remote[0]
    host = url.split("//")[1].rstrip("/")
    r = j(client_admin, "POST", f"/api/projects/{project.id}/git/check-access", {"remote": "origin"}).json()
    assert r["checked"] and r["read"] is False and r["write"] is False and r["credential"] is None
    j(client_admin, "POST", "/api/git/credentials", {"host": host, "username": "user", "token": "s3cret-token"})
    r = j(client_admin, "POST", f"/api/projects/{project.id}/git/check-access", {"remote": "origin"}).json()
    assert r["read"] is True and r["write"] is True and r["credential"] == f"user@{host}"
    s = client_admin.get(f"/api/projects/{project.id}/git/settings").json()
    assert s["remotes"][0]["credential"]["username"] == "user"
