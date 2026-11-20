"""File API: read/save versions, external-edit conflicts, atomic writes, snapshots, ops, search."""
import os
import time

import pytest

from audit.models import AuditEvent


def url(p, suffix):
    return f"/api/projects/{p.id}/{suffix}"


def read(client, p, path):
    r = client.get(url(p, "file"), {"path": path})
    assert r.status_code == 200, r.content
    return r.json()


def save(client, p, path, content, base, force=False):
    return client.put(url(p, "file"), {"path": path, "content": content, "base_version": base, "force": force},
                      content_type="application/json")


@pytest.mark.django_db
def test_read_returns_content_and_version(client_admin, plain_project):
    data = read(client_admin, plain_project, "hello.txt")
    assert data["content"] == "hello\n"
    assert data["editable"] and data["language"] == "plaintext"
    assert len(data["version"]["sha256"]) == 64


@pytest.mark.django_db
def test_save_roundtrip_and_audit(client_admin, plain_project, workroot):
    v = read(client_admin, plain_project, "hello.txt")["version"]["token"]
    r = save(client_admin, plain_project, "hello.txt", "updated\n", v)
    assert r.status_code == 200
    assert (workroot / "plain" / "hello.txt").read_text() == "updated\n"
    assert AuditEvent.objects.filter(action="file.save", outcome="success").exists()
    # no temp files left behind (atomic write)
    assert not [f for f in os.listdir(workroot / "plain") if ".cs-tmp-" in f]


@pytest.mark.django_db
def test_external_modification_conflict(client_admin, plain_project, workroot):
    v = read(client_admin, plain_project, "hello.txt")["version"]["token"]
    time.sleep(0.01)
    (workroot / "plain" / "hello.txt").write_text("changed by someone else\n", encoding="utf-8", newline="")
    r = save(client_admin, plain_project, "hello.txt", "my edit\n", v)
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "conflict"
    assert body["current_content"] == "changed by someone else\n"
    assert (workroot / "plain" / "hello.txt").read_text() == "changed by someone else\n"  # not overwritten
    # Resolving with the disk version as the new base succeeds without force.
    r2 = save(client_admin, plain_project, "hello.txt", "merged\n", body["current_version"]["token"])
    assert r2.status_code == 200


@pytest.mark.django_db
def test_touch_without_content_change_is_not_a_conflict(client_admin, plain_project, workroot):
    v = read(client_admin, plain_project, "hello.txt")["version"]["token"]
    f = workroot / "plain" / "hello.txt"
    os.utime(f, (time.time() + 100, time.time() + 100))
    assert save(client_admin, plain_project, "hello.txt", "edit\n", v).status_code == 200


@pytest.mark.django_db
def test_force_overwrite_snapshots_and_restore(client_admin, plain_project, workroot):
    v = read(client_admin, plain_project, "hello.txt")["version"]["token"]
    (workroot / "plain" / "hello.txt").write_text("their version\n", encoding="utf-8", newline="")
    r = save(client_admin, plain_project, "hello.txt", "mine\n", v, force=True)
    assert r.status_code == 200
    snaps = client_admin.get(url(plain_project, "snapshots")).json()["snapshots"]
    assert snaps and snaps[0]["op"] == "force-save"
    # Restore needs confirmation
    r = client_admin.post(url(plain_project, f"snapshots/{snaps[0]['id']}/restore"), {}, content_type="application/json")
    assert r.status_code == 428
    r = client_admin.post(url(plain_project, f"snapshots/{snaps[0]['id']}/restore"), {"confirm": True},
                          content_type="application/json")
    assert r.status_code == 200
    assert (workroot / "plain" / "hello.txt").read_text() == "their version\n"
    # the restore itself is undoable
    assert r.json()["pre_restore_snapshot"]


@pytest.mark.django_db
def test_save_deleted_file_conflict(client_admin, plain_project, workroot):
    v = read(client_admin, plain_project, "hello.txt")["version"]["token"]
    (workroot / "plain" / "hello.txt").unlink()
    r = save(client_admin, plain_project, "hello.txt", "x", v)
    assert r.status_code == 409 and r.json()["code"] == "deleted"


@pytest.mark.django_db
def test_create_rename_delete(client_admin, plain_project, workroot):
    base = workroot / "plain"
    r = client_admin.post(url(plain_project, "file/create"), {"path": "docs", "kind": "dir"}, content_type="application/json")
    assert r.status_code == 200 and (base / "docs").is_dir()
    r = client_admin.post(url(plain_project, "file/create"), {"path": "docs/a.md", "content": "# A"},
                          content_type="application/json")
    assert r.status_code == 200
    assert client_admin.post(url(plain_project, "file/create"), {"path": "docs/a.md"},
                             content_type="application/json").status_code == 409
    r = client_admin.post(url(plain_project, "file/rename"), {"path": "docs/a.md", "new_path": "docs/b.md"},
                          content_type="application/json")
    assert r.status_code == 200 and (base / "docs" / "b.md").exists()
    assert client_admin.post(url(plain_project, "file/rename"), {"path": "docs/b.md", "new_path": "../escape.md"},
                             content_type="application/json").status_code == 403
    # delete requires confirm and snapshots first
    assert client_admin.post(url(plain_project, "file/delete"), {"path": "docs"},
                             content_type="application/json").status_code == 428
    r = client_admin.post(url(plain_project, "file/delete"), {"path": "docs", "confirm": True},
                          content_type="application/json")
    assert r.status_code == 200 and not (base / "docs").exists()
    sid = r.json()["snapshot"]
    client_admin.post(url(plain_project, f"snapshots/{sid}/restore"), {"confirm": True}, content_type="application/json")
    assert (base / "docs" / "b.md").read_text() == "# A"


@pytest.mark.django_db
def test_cannot_delete_project_root_or_git(client_admin, plain_project, git_project):
    assert client_admin.post(url(plain_project, "file/delete"), {"path": "", "confirm": True},
                             content_type="application/json").status_code == 403
    assert client_admin.post(url(git_project, "file/delete"), {"path": ".git", "confirm": True},
                             content_type="application/json").status_code == 403


@pytest.mark.django_db
def test_binary_file_is_read_only(client_admin, plain_project, workroot):
    (workroot / "plain" / "img.bin").write_bytes(b"\x00\x01\x02binary")
    data = read(client_admin, plain_project, "img.bin")
    assert data["binary"] and not data["editable"] and data["content"] is None


@pytest.mark.django_db
def test_search(client_admin, plain_project):
    r = client_admin.get(url(plain_project, "search"), {"q": "print"})
    hits = r.json()["results"]
    assert any(h["path"] == "src/app.py" and h["line"] == 1 for h in hits)
    r = client_admin.get(url(plain_project, "search"), {"q": "pr[i]nt", "regex": "1"})
    assert r.json()["results"]
    assert client_admin.get(url(plain_project, "search"), {"q": "(", "regex": "1"}).status_code == 400


@pytest.mark.django_db
def test_tree_hides_git_dir(client_admin, git_project):
    names = [e["name"] for e in client_admin.get(url(git_project, "tree")).json()["entries"]]
    assert ".git" not in names and "main.py" in names


@pytest.mark.django_db
def test_snapshot_retention(settings, plain_project):
    from files import snapshots

    settings.CODE_STUDIO["SNAPSHOT_RETENTION_COUNT"] = 3
    for _ in range(5):
        snapshots.create_snapshot(plain_project, "test", ["hello.txt"])
    assert len(snapshots.list_snapshots(plain_project)) == 3


@pytest.mark.django_db
def test_snapshots_live_outside_project(plain_project, workroot, settings):
    from files import snapshots

    sid = snapshots.create_snapshot(plain_project, "test", ["hello.txt"])
    assert (settings.DATA_DIR / "snapshots" / str(plain_project.id) / sid).is_dir()
    assert not any("snapshot" in p.name for p in (workroot / "plain").rglob("*"))
