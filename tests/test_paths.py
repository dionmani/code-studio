"""Path containment: traversal, absolute paths, Windows quirks, symlink/junction escapes."""
import os
import sys

import pytest

from projects.paths import PathSecurityError, resolve, split_relative, validate_root_path


@pytest.mark.parametrize("bad", [
    "../x", "a/../../x", "..\\x", "a\\..\\..\\b", "/etc/passwd", "C:\\Windows\\win.ini", "C:/x", "c:x",
    "\\\\server\\share\\x", "//server/share", "file.txt:stream", "a/CON", "nul.txt", "COM1", "trail. ",
    "dots...", "bad\x00name", "x?y", "a|b",
])
def test_split_relative_rejects(bad):
    with pytest.raises(PathSecurityError):
        split_relative(bad)


@pytest.mark.parametrize("good,expected", [("", []), (".", []), ("a/b.txt", ["a", "b.txt"]),
                                           ("a\\b", ["a", "b"]), ("./a//b/", ["a", "b"])])
def test_split_relative_accepts(good, expected):
    assert split_relative(good) == expected


@pytest.mark.django_db
def test_resolve_contains(plain_project, workroot):
    assert resolve(plain_project, "src/app.py") == (workroot / "plain" / "src" / "app.py").resolve()
    with pytest.raises(PathSecurityError):
        resolve(plain_project, "../outside.txt")


@pytest.mark.django_db
def test_git_dir_protected(git_project):
    with pytest.raises(PathSecurityError):
        resolve(git_project, ".git/config")
    with pytest.raises(PathSecurityError):
        resolve(git_project, ".GIT/hooks/pre-commit")


def _make_junction(link, target):
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows-only")
@pytest.mark.django_db
def test_junction_escape_rejected(plain_project, workroot, tmp_path, client_admin):
    outside = tmp_path / "outside_secret"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret", encoding="utf-8", newline="")
    _make_junction(workroot / "plain" / "escape", outside)
    with pytest.raises(PathSecurityError):
        resolve(plain_project, "escape/secret.txt")
    # Through the API: read, write and tree listing are all blocked.
    r = client_admin.get(f"/api/projects/{plain_project.id}/file", {"path": "escape/secret.txt"})
    assert r.status_code == 403
    r = client_admin.put(f"/api/projects/{plain_project.id}/file",
                         data={"path": "escape/new.txt", "content": "x"}, content_type="application/json")
    assert r.status_code == 403
    assert not (outside / "new.txt").exists()
    r = client_admin.get(f"/api/projects/{plain_project.id}/tree", {"path": "escape"})
    assert r.status_code == 403
    entries = client_admin.get(f"/api/projects/{plain_project.id}/tree").json()["entries"]
    esc = next(e for e in entries if e["name"] == "escape")
    assert esc["escapes_root"] is True


@pytest.mark.django_db
def test_symlink_escape_rejected(plain_project, workroot, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("nope", encoding="utf-8", newline="")
    link = workroot / "plain" / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine (needs Developer Mode/admin)")
    with pytest.raises(PathSecurityError):
        resolve(plain_project, "link.txt")


@pytest.mark.django_db
def test_validate_root_rejects_dangerous(tmp_path, settings):
    with pytest.raises(PathSecurityError):
        validate_root_path("relative/path")
    with pytest.raises(PathSecurityError):
        validate_root_path("C:\\")
    with pytest.raises(PathSecurityError):
        validate_root_path(os.environ.get("SystemRoot", "C:\\Windows"))
    with pytest.raises(PathSecurityError):
        validate_root_path(str(settings.DATA_DIR))
    with pytest.raises(PathSecurityError):
        validate_root_path(str(tmp_path / "missing"))


@pytest.mark.django_db
def test_root_swapped_for_junction_is_rejected(plain_project, workroot, tmp_path):
    """If the allowlisted root is replaced by a junction after approval, all access stops."""
    if sys.platform != "win32":
        pytest.skip("windows only")
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "plain").mkdir(parents=True)
    moved = tmp_path / "moved-root"
    os.rename(workroot, moved)
    _make_junction(workroot, elsewhere)
    with pytest.raises(PathSecurityError):
        resolve(plain_project, "hello.txt")
