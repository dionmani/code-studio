"""Unmanaged folders vs Dulwich Git workflows, plus repo-content hardening."""
import pytest
from dulwich import porcelain

from tests.conftest import init_git_repo


def url(p, suffix):
    return f"/api/projects/{p.id}/{suffix}"


def post(client, u, body):
    return client.post(u, body, content_type="application/json")


@pytest.mark.django_db
def test_unmanaged_folder_has_no_git(client_admin, plain_project, workroot):
    r = client_admin.get(url(plain_project, "git/status"))
    assert r.status_code == 200 and r.json() == {"enabled": False, "mode": "unmanaged"}
    assert not (workroot / "plain" / ".git").exists()  # never initialised
    assert post(client_admin, url(plain_project, "git/commit"), {"message": "x"}).status_code == 400
    assert not (workroot / "plain" / ".git").exists()


@pytest.mark.django_db
def test_register_detects_mode(client_admin, root, workroot):
    (workroot / "folder").mkdir()
    init_git_repo(workroot / "gitrepo")
    r1 = post(client_admin, "/api/projects", {"root_id": root.id, "relative_path": "folder"})
    r2 = post(client_admin, "/api/projects", {"root_id": root.id, "relative_path": "gitrepo"})
    assert r1.json()["git_mode"] == "unmanaged" and r2.json()["git_mode"] == "git"
    assert post(client_admin, "/api/projects", {"root_id": root.id, "relative_path": "folder"}).status_code == 409
    assert post(client_admin, "/api/projects", {"root_id": root.id, "relative_path": "../x"}).status_code == 400


@pytest.mark.django_db
def test_status_diff_stage_commit(client_admin, git_project, workroot, admin_user):
    repo_dir = workroot / "repo"
    st = client_admin.get(url(git_project, "git/status")).json()
    assert st["enabled"] and st["clean"] and st["branch"] == "master"
    (repo_dir / "main.py").write_text("x = 2\n", encoding="utf-8", newline="")
    (repo_dir / "new.txt").write_text("new\n", encoding="utf-8", newline="")
    st = client_admin.get(url(git_project, "git/status")).json()
    assert {"path": "main.py", "status": "modified"} in st["unstaged"]
    assert {"path": "new.txt", "status": "untracked"} in st["untracked"]

    d = client_admin.get(url(git_project, "git/diff"), {"path": "main.py"}).json()
    assert d["original"] == "x = 1\n" and d["modified"] == "x = 2\n"
    assert "-x = 1" in d["diff"] and "+x = 2" in d["diff"]

    st = post(client_admin, url(git_project, "git/stage"), {"paths": ["main.py", "new.txt"]}).json()
    assert {e["path"] for e in st["staged"]} == {"main.py", "new.txt"}
    staged_diff = client_admin.get(url(git_project, "git/diff"), {"path": "main.py", "staged": "1"}).json()
    assert staged_diff["modified"] == "x = 2\n"

    st = post(client_admin, url(git_project, "git/unstage"), {"paths": ["new.txt"]}).json()
    assert {e["path"] for e in st["staged"]} == {"main.py"}
    assert any(e["path"] == "new.txt" for e in st["untracked"])

    r = post(client_admin, url(git_project, "git/commit"), {"message": "change x"})
    assert r.status_code == 200
    sha = r.json()["commit"]
    with porcelain.open_repo_closing(str(repo_dir)) as repo:
        c = repo[sha.encode()]
        assert c.message == b"change x"
        assert b"admin" in c.author
    assert post(client_admin, url(git_project, "git/commit"), {"message": "nothing"}).status_code == 400


@pytest.mark.django_db
def test_stage_deleted_file(client_admin, git_project, workroot):
    (workroot / "repo" / "README.md").unlink()
    st = client_admin.get(url(git_project, "git/status")).json()
    assert {"path": "README.md", "status": "deleted"} in st["unstaged"]
    st = post(client_admin, url(git_project, "git/stage"), {"paths": ["README.md"]}).json()
    assert {"path": "README.md", "status": "deleted"} in st["staged"]


@pytest.mark.django_db
def test_hooks_are_never_executed(client_admin, git_project, workroot):
    repo_dir = workroot / "repo"
    marker = workroot / "HOOK_RAN"
    hooks = repo_dir / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-commit", "commit-msg", "post-commit"):
        (hooks / name).write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n", encoding="utf-8", newline="")
        (hooks / (name + ".bat")).write_text(f"echo ran > \"{marker}\"\n", encoding="utf-8", newline="")
    (repo_dir / "main.py").write_text("x = 3\n", encoding="utf-8", newline="")
    post(client_admin, url(git_project, "git/stage"), {"paths": ["main.py"]})
    assert post(client_admin, url(git_project, "git/commit"), {"message": "m"}).status_code == 200
    assert not marker.exists()


@pytest.mark.django_db
def test_filter_drivers_are_never_executed(client_admin, git_project, workroot):
    repo_dir = workroot / "repo"
    marker = workroot / "FILTER_RAN"
    (repo_dir / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8", newline="")
    cfg = repo_dir / ".git" / "config"
    cmd = f'cmd /c echo pwned > "{marker}"'
    cfg.write_text(cfg.read_text() + f'[filter "evil"]\n\tclean = {cmd}\n\tsmudge = {cmd}\n\tprocess = {cmd}\n',
                   encoding="utf-8", newline="")
    (repo_dir / "data.txt").write_text("payload\n", encoding="utf-8", newline="")
    client_admin.get(url(git_project, "git/status"))
    post(client_admin, url(git_project, "git/stage"), {"paths": ["data.txt", ".gitattributes"]})
    post(client_admin, url(git_project, "git/commit"), {"message": "attrs"})
    client_admin.get(url(git_project, "git/diff"), {"path": "data.txt"})
    assert not marker.exists()


@pytest.mark.django_db
def test_push_pull_local_remote_with_confirmation(client_admin, git_project, workroot, tmp_path):
    repo_dir = workroot / "repo"
    bare = tmp_path / "remote.git"
    porcelain.clone(str(repo_dir), str(bare), bare=True).close()
    with porcelain.open_repo_closing(str(repo_dir)) as r:
        cfg = r.get_config()
        cfg.set((b"remote", b"origin"), b"url", str(bare).encode())
        cfg.write_to_path()

    (repo_dir / "main.py").write_text("x = 10\n", encoding="utf-8", newline="")
    post(client_admin, url(git_project, "git/stage"), {"paths": ["main.py"]})
    head = post(client_admin, url(git_project, "git/commit"), {"message": "for push"}).json()["commit"]

    assert post(client_admin, url(git_project, "git/push"), {"remote": "origin"}).status_code == 428
    r = post(client_admin, url(git_project, "git/push"), {"remote": "origin", "confirm": True})
    assert r.status_code == 200, r.content
    with porcelain.open_repo_closing(str(bare)) as b:
        assert b.refs[b"refs/heads/master"].decode() == head

    # Another clone pushes a commit; pull fast-forwards our repo.
    other = tmp_path / "other"
    porcelain.clone(str(bare), str(other)).close()
    (other / "main.py").write_text("x = 11\n", encoding="utf-8", newline="")
    porcelain.add(str(other), paths=[str(other / "main.py")])
    porcelain.commit(str(other), message=b"remote change", author=b"O <o@x>", committer=b"O <o@x>")
    porcelain.push(str(other), str(bare), b"refs/heads/master")
    r = post(client_admin, url(git_project, "git/pull"), {"remote": "origin", "confirm": True})
    assert r.status_code == 200, r.content
    assert r.json()["updated"] is True
    assert (repo_dir / "main.py").read_text() == "x = 11\n"

    # Pull refuses with a dirty tree.
    (repo_dir / "main.py").write_text("dirty\n", encoding="utf-8", newline="")
    assert post(client_admin, url(git_project, "git/pull"), {"remote": "origin", "confirm": True}).status_code == 409


@pytest.mark.django_db
def test_ssh_remote_refused(client_admin, git_project, workroot):
    with porcelain.open_repo_closing(str(workroot / "repo")) as r:
        cfg = r.get_config()
        cfg.set((b"remote", b"origin"), b"url", b"ssh://git@example.com/x.git")
        cfg.set((b"core",), b"sshCommand", b"calc.exe")
        cfg.write_to_path()
    r = post(client_admin, url(git_project, "git/push"), {"remote": "origin", "confirm": True})
    assert r.status_code == 400 and "ssh" in r.json()["error"]


@pytest.mark.django_db
def test_ai_commit_message(client_admin, git_project, workroot, live_engine, plain_project):
    u = url(git_project, "git/commit-message")
    # nothing changed -> nothing to describe
    assert post(client_admin, u, {"adapter": "fixture"}).status_code == 400
    (workroot / "repo" / "main.py").write_text("x = 2\n", encoding="utf-8", newline="")
    r = post(client_admin, u, {"adapter": "fixture"})
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["adapter"] == "fixture" and body["staged"] is False
    assert body["message"] == "fixture run complete"
    post(client_admin, url(git_project, "git/stage"), {"paths": ["main.py"]})
    assert post(client_admin, u, {"adapter": "fixture"}).json()["staged"] is True
    # the prompt carries the diff and the repo's recent subjects
    from vcs import service
    ch = service.changes_for_message(git_project)
    assert ch["staged"] and "+x = 2" in ch["diff"]
    assert service.recent_subjects(git_project) == ["initial"]
    # unmanaged folders are refused; nothing was committed
    assert post(client_admin, url(plain_project, "git/commit-message"), {}).status_code == 400
    assert client_admin.get(url(git_project, "git/status")).json()["staged"]


def patch(client, u, body):
    return client.patch(u, body, content_type="application/json")


@pytest.mark.django_db
def test_commit_message_preference(client_admin, git_project, workroot, live_engine):
    pu = "/api/git/commit-message-preference"
    client_admin.get("/api/agents")  # mirrors the engine's adapters into the database
    p = client_admin.get(pu).json()
    assert p["adapter"] == "" and p["model"] == ""
    ids = {a["identifier"] for a in p["agents"]}
    assert {"claude-code", "codex", "fixture"} <= ids and "verify" not in ids
    claude = next(a for a in p["agents"] if a["identifier"] == "claude-code")
    assert claude["default_model"] == "haiku" and "sonnet" in claude["suggested_models"]

    assert patch(client_admin, pu, {"adapter": "nope"}).status_code == 400
    assert patch(client_admin, pu, {"adapter": "verify"}).status_code == 400
    assert patch(client_admin, pu, {"adapter": "codex", "model": "bad model; rm"}).status_code == 400
    r = patch(client_admin, pu, {"adapter": "fixture", "model": "sonnet"})
    assert r.status_code == 200 and r.json()["adapter"] == "fixture" and r.json()["model"] == "sonnet"

    # the saved choice is used without the request naming an agent
    (workroot / "repo" / "main.py").write_text("x = 3\n", encoding="utf-8", newline="")
    body = post(client_admin, url(git_project, "git/commit-message"), {}).json()
    assert body["adapter"] == "fixture" and body["model"] == "sonnet"

    # back to automatic
    assert patch(client_admin, pu, {"adapter": "", "model": ""}).json() ["adapter"] == ""
