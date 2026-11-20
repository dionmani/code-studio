"""Images attached to agent tasks and follow-ups: validation, engine handoff, IDE API."""
import base64
import json

import pytest

from cs_common.images import MAX_IMAGE_BYTES, MAX_IMAGES, ImageError, clean_images, sniff
from tests.conftest import wait_for

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ---- shared validation ---------------------------------------------------------------

def test_sniff_uses_the_bytes_not_the_name():
    assert sniff(PNG) == "image/png" and sniff(JPEG) == "image/jpeg"
    assert sniff(b"GIF89a....") == "image/gif" and sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff(SVG) is None and sniff(b"") is None


def test_clean_images_accepts_data_urls_and_cleans_names():
    out = clean_images([{"name": "C:\\Users\\me\\shot\x07.png", "data": "data:image/png;base64," + b64(PNG)},
                        {"name": "", "data": b64(JPEG)}])
    assert [(o["name"], o["media_type"]) for o in out] == [("shot.png", "image/png"), ("image-2", "image/jpeg")]
    assert out[0]["data"] == PNG
    assert clean_images(None) == []


@pytest.mark.parametrize("raw, msg", [
    ([{"name": "x.svg", "data": b64(SVG)}], "not a PNG"),
    ([{"name": "x.png", "data": "!!notbase64!!"}], "base64"),
    ([{"name": "x.png", "data": b64(PNG + b"\x00" * MAX_IMAGE_BYTES)}], "larger than"),
    ([{"name": f"{i}.png", "data": b64(PNG)} for i in range(MAX_IMAGES + 1)], "at most"),
    ("nope", "must be a list"),
])
def test_clean_images_refuses_bad_input(raw, msg):
    with pytest.raises(ImageError, match=msg):
        clean_images(raw)


# ---- engine --------------------------------------------------------------------------

def test_prompts_point_at_the_images():
    from agent_engine.tasks import build_followup_prompt, build_prompt, image_note

    imgs = [{"name": "login page.png", "file": "task-01.png"}]
    note = image_note(imgs, attached=False)
    assert '.codestudio-task/attachments/task-01.png ("login page.png")' in note and "file-reading tool" in note
    assert "also attached" in image_note(imgs, attached=True)
    assert image_note([], attached=False) == ""
    p = build_prompt("implementer", "Fix the layout", [], False, images=note)
    assert p.index("Fix the layout") < p.index("task-01.png")
    assert "task-01.png" in build_followup_prompt("Now this", False, False, [], images=note)
    assert "attachments" not in build_prompt("implementer", "obj", [], False)


def test_codex_attaches_images_natively(tmp_path, monkeypatch):
    from agent_engine.adapters.claude_code import ClaudeCodeAdapter
    from agent_engine.adapters.codex import CodexAdapter

    codex = CodexAdapter()
    monkeypatch.setattr(codex, "_argv0", lambda: ["codex"])
    for pol in ({"images": ["/w/a.png", "/w/b.jpg"]}, {"images": ["/w/a.png", "/w/b.jpg"], "resume_session": "S1"}):
        argv, _ = codex.build_command("p", tmp_path, pol)
        # "=" form, before the other flags: the multi-value --image never swallows the stdin "-"
        assert argv.index("--image=/w/a.png") < argv.index("--image=/w/b.jpg") < argv.index("--json")
        assert argv[-1] == "-"
    assert codex.attaches_images is True
    claude = ClaudeCodeAdapter()
    monkeypatch.setattr(claude, "find_executable", lambda: "claude")
    assert claude.attaches_images is False
    assert not any("image" in a for a in claude.build_command("p", tmp_path, {"images": ["/w/a.png"]})[0])


def test_handoff_copies_attachments(tmp_path):
    from agent_engine.tasks import write_handoff

    work, src = tmp_path / "work", tmp_path / "att"
    work.mkdir()
    src.mkdir()
    (src / "task-01.png").write_bytes(PNG)
    data = {"objective": "o", "attachments": [{"name": "s.png", "file": "task-01.png"}], "assignments": [
        {"index": 0, "role": "implementer", "adapter": "codex", "status": "pending", "policy": {},
         "attachments": []}]}
    write_handoff(work, data, [], current=0, attachments_src=src)
    root = work / ".codestudio-task"
    (root / "attachments" / "task-01.png").write_bytes(b"tampered")
    write_handoff(work, data, [], current=0, attachments_src=src)  # rewritten from the engine's copy
    assert (root / "attachments" / "task-01.png").read_bytes() == PNG
    index = json.loads((root / "task.json").read_text(encoding="utf-8"))
    assert index["attachments"] == [{"file": "attachments/task-01.png", "name": "s.png"}]


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "srcproj"
    d.mkdir()
    (d / "a.txt").write_text("alpha\n", encoding="utf-8")
    return d


def test_engine_stores_images_for_task_and_follow_up(live_engine, src):
    from tests.test_engine import Signed, events_of, wait_state

    eng = Signed(live_engine.url)
    body = {"objective": "WRITE a.txt <<< beta\n", "project": {"id": 1, "name": "p", "root": str(src)},
            "assignments": [{"adapter": "fixture", "role": "implementer", "policy": {}}]}
    bad = eng.req("POST", "/tasks", {**body, "images": [{"name": "x.svg", "data": b64(SVG)}]})
    assert bad.status_code == 400 and "not a PNG" in bad.text
    r = eng.req("POST", "/tasks", {**body, "images": [{"name": "shot.png", "data": b64(PNG)}]})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert r.json()["attachments"] == [{"name": "shot.png", "file": "task-01.png", "media_type": "image/png",
                                        "size": len(PNG)}]
    t = wait_state(eng, tid, {"awaiting_review", "failed"})
    assert t["state"] == "awaiting_review", t
    att = live_engine.config.engine_dir / "workspaces" / tid / "work" / ".codestudio-task" / "attachments"
    assert (att / "task-01.png").read_bytes() == PNG
    # the images are never part of the change set
    assert {c["path"] for c in eng.req("GET", f"/tasks/{tid}/changes").json()["changes"]} == {"a.txt"}

    r = eng.req("POST", f"/tasks/{tid}/followup", {"message": "SAY ok", "images": [{"name": "b.jpg", "data": b64(JPEG)}]})
    assert r.status_code == 200, r.text
    t = wait_state(eng, tid, {"awaiting_review", "failed"})
    assert t["assignments"][1]["attachments"][0]["file"] == "step02-01.jpg"
    assert (att / "step02-01.jpg").read_bytes() == JPEG and (att / "task-01.png").is_file()
    followup_ev = next(e for e in events_of(eng, tid) if e["type"] == "task.followup")
    assert followup_ev["payload"]["images"] == 1


# ---- IDE -----------------------------------------------------------------------------

def _submit(client, project, profile, **extra):
    return client.post(f"/api/projects/{project.id}/tasks",
                       {"objective": "WRITE hello.txt <<< hi\n", **extra,
                        "assignments": [{"profile_id": profile.id, "role": "implementer"}]},
                       content_type="application/json")


def _wait(client, tid):
    return wait_for(lambda: (lambda t: t if t["state"] in {"awaiting_review", "failed"} else None)(
        client.get(f"/api/tasks/{tid}").json()), timeout=40)


@pytest.mark.django_db
def test_ide_task_and_follow_up_images(client_admin, client, plain_user, plain_project, fixture_profile,
                                       live_engine, workroot):
    from agents_client.models import Task

    r = _submit(client_admin, plain_project, fixture_profile, images=[{"name": "x.svg", "data": b64(SVG)}])
    assert r.status_code == 400 and "not a PNG" in r.json()["error"]
    r = _submit(client_admin, plain_project, fixture_profile,
                images=[{"name": "shot.png", "data": "data:image/png;base64," + b64(PNG)}])
    assert r.status_code == 200, r.content
    tid = r.json()["id"]
    img = r.json()["attachments"][0]
    assert img["name"] == "shot.png" and img["url"] == f"/api/tasks/{tid}/images/task-01.png"
    t = _wait(client_admin, tid)
    assert t["state"] == "awaiting_review", t

    resp = client_admin.get(img["url"])
    assert resp.status_code == 200 and resp["Content-Type"] == "image/png"
    assert b"".join(resp.streaming_content) == PNG
    resp.close()  # Windows: an open handle would keep the file (and its folder) from being deleted
    assert client_admin.get(f"/api/tasks/{tid}/images/task-02.png").status_code == 404
    assert client_admin.get(f"/api/tasks/{tid}/images/..%2Fdb.sqlite3").status_code == 404

    r = client_admin.post(f"/api/tasks/{tid}/followup", {"message": "SAY again",
                                                          "images": [{"name": "b.jpg", "data": b64(JPEG)}]},
                          content_type="application/json")
    assert r.status_code == 200, r.content
    t = _wait(client_admin, tid)
    step = t["assignments"][1]
    assert step["attachments"][0]["url"] == f"/api/tasks/{tid}/images/step02-01.jpg"
    resp = client_admin.get(step["attachments"][0]["url"])
    assert resp["Content-Type"] == "image/jpeg"
    resp.close()

    client.force_login(plain_user)  # no access to the project: no access to its images
    assert client.get(img["url"]).status_code == 404

    task = Task.objects.get(pk=tid)
    folder = task.attachments_dir
    assert (folder / "task-01.png").is_file()
    task.delete()
    assert not folder.exists()
