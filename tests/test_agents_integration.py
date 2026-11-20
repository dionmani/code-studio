"""IDE <-> Agent Engine integration: submit, stream, review, gated apply, rollback, offline."""
import json

import pytest
from asgiref.sync import async_to_sync
from django.test import AsyncClient

from agents_client.models import AgentAdapter, ChangeSet, Task, TaskEvent
from tests.conftest import wait_for


def submit(client, project, profile, objective):
    return client.post(f"/api/projects/{project.id}/tasks",
                       {"objective": objective, "assignments": [{"profile_id": profile.id, "role": "implementer"}]},
                       content_type="application/json")


def wait_task(client, tid, states, timeout=40):
    return wait_for(lambda: (lambda t: t if t["state"] in states else None)(client.get(f"/api/tasks/{tid}").json()),
                    timeout=timeout)


@pytest.mark.django_db
def test_engine_offline_ide_still_works(client_admin, plain_project, fixture_profile, settings):
    settings.CODE_STUDIO["ENGINE_URL"] = "http://127.0.0.1:1"
    from agents_client import engine_client

    engine_client._health_cache.update(at=0.0, value=None)
    st = client_admin.get("/api/engine/status").json()
    assert st["online"] is False
    agents = client_admin.get("/api/agents").json()
    assert agents["engine_online"] is False and agents["profiles"]
    r = submit(client_admin, plain_project, fixture_profile, "WRITE x.txt <<< x")
    assert r.status_code == 503
    assert Task.objects.get().state == "engine_offline"
    # Core IDE features are unaffected.
    assert client_admin.get(f"/api/projects/{plain_project.id}/tree").status_code == 200
    v = client_admin.get(f"/api/projects/{plain_project.id}/file", {"path": "hello.txt"}).json()["version"]["token"]
    assert client_admin.put(f"/api/projects/{plain_project.id}/file",
                            {"path": "hello.txt", "content": "offline edit\n", "base_version": v},
                            content_type="application/json").status_code == 200
    assert client_admin.get(f"/api/projects/{plain_project.id}/git/status").status_code == 200


@pytest.mark.django_db
def test_agents_discovery_syncs_adapters(client_admin, live_engine):
    data = client_admin.get("/api/agents").json()
    assert data["engine_online"]
    ids = {a["identifier"] for a in data["adapters"]}
    assert {"claude-code", "codex", "antigravity", "fixture"} <= ids
    assert AgentAdapter.objects.get(identifier="antigravity").available is False


@pytest.mark.django_db
def test_submit_review_approve_apply(client_admin, plain_project, fixture_profile, live_engine, workroot):
    base = workroot / "plain"
    r = submit(client_admin, plain_project, fixture_profile,
               "WRITE hello.txt <<< hello from agent\\n\nWRITE docs/new.md <<< # New\nDELETE src/app.py")
    assert r.status_code == 200, r.content
    tid = r.json()["id"]
    t = wait_task(client_admin, tid, {"awaiting_review", "failed"})
    assert t["state"] == "awaiting_review", t
    # Nothing touched the live project yet.
    assert (base / "hello.txt").read_text() == "hello\n" and (base / "src" / "app.py").exists()
    assert TaskEvent.objects.filter(task_id=tid).count() > 3

    changes = {c["path"]: c for c in client_admin.get(f"/api/tasks/{tid}/changes").json()["changes"]}
    assert changes["hello.txt"]["action"] == "modified" and changes["hello.txt"]["applicable"]
    assert changes["docs/new.md"]["action"] == "added"
    assert changes["src/app.py"]["action"] == "deleted"
    assert changes["hello.txt"]["base_text"] == "hello\n"
    cs = ChangeSet.objects.get(task_id=tid)
    assert cs.artifact_path and json.loads(open(cs.artifact_path, encoding="utf-8").read())

    # explicit confirmation required
    r = client_admin.post(f"/api/tasks/{tid}/approve", {"files": list(changes)}, content_type="application/json")
    assert r.status_code == 428
    r = client_admin.post(f"/api/tasks/{tid}/approve", {"files": list(changes), "confirm": True},
                          content_type="application/json")
    assert r.status_code == 200, r.content
    res = r.json()
    assert res["engine_acknowledged"] and res["snapshot"]
    assert (base / "hello.txt").read_text() == "hello from agent\n"
    assert (base / "docs" / "new.md").read_text() == "# New"
    assert not (base / "src" / "app.py").exists()
    assert Task.objects.get(pk=tid).state == "applied"
    assert ChangeSet.objects.get(task_id=tid).approval_status == "applied"

    # Rollback via the recovery snapshot restores the pre-apply state.
    r = client_admin.post(f"/api/projects/{plain_project.id}/snapshots/{res['snapshot']}/restore",
                          {"confirm": True}, content_type="application/json")
    assert r.status_code == 200
    assert (base / "hello.txt").read_text() == "hello\n"
    assert (base / "src" / "app.py").exists() and not (base / "docs" / "new.md").exists()


@pytest.mark.django_db
def test_apply_conflict_applies_nothing(client_admin, plain_project, fixture_profile, live_engine, workroot):
    base = workroot / "plain"
    tid = submit(client_admin, plain_project, fixture_profile,
                 "WRITE hello.txt <<< agent\nWRITE other.txt <<< other").json()["id"]
    wait_task(client_admin, tid, {"awaiting_review"})
    (base / "hello.txt").write_text("user edited meanwhile\n", encoding="utf-8", newline="")
    changes = client_admin.get(f"/api/tasks/{tid}/changes").json()["changes"]
    hello = next(c for c in changes if c["path"] == "hello.txt")
    assert hello["applicable"] is False and "changed" in hello["conflict_reason"]
    r = client_admin.post(f"/api/tasks/{tid}/approve", {"files": ["hello.txt", "other.txt"], "confirm": True},
                          content_type="application/json")
    assert r.status_code == 409
    assert r.json()["conflicts"][0]["path"] == "hello.txt"
    assert (base / "hello.txt").read_text() == "user edited meanwhile\n"
    assert not (base / "other.txt").exists()  # all-or-nothing
    # Partial approval of the non-conflicting file works.
    r = client_admin.post(f"/api/tasks/{tid}/approve", {"files": ["other.txt"], "confirm": True},
                          content_type="application/json")
    assert r.status_code == 200 and r.json()["decision"] == "partially_applied"
    assert (base / "other.txt").read_text() == "other"


@pytest.mark.django_db
def test_apply_failure_rolls_back(client_admin, plain_project, fixture_profile, live_engine, workroot, monkeypatch):
    base = workroot / "plain"
    tid = submit(client_admin, plain_project, fixture_profile,
                 "WRITE hello.txt <<< changed\nWRITE zzz.txt <<< second").json()["id"]
    wait_task(client_admin, tid, {"awaiting_review"})
    from agents_client import service

    real = service.atomic_write
    calls = {"n": 0}

    def flaky(path, data):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full (simulated)")
        return real(path, data)

    monkeypatch.setattr(service, "atomic_write", flaky)
    r = client_admin.post(f"/api/tasks/{tid}/approve", {"files": ["hello.txt", "zzz.txt"], "confirm": True},
                          content_type="application/json")
    assert r.status_code == 500 and "rolled back" in r.json()["error"]
    assert (base / "hello.txt").read_text() == "hello\n"
    assert not (base / "zzz.txt").exists()


@pytest.mark.django_db
def test_sensitive_files_never_applied(client_admin, plain_project, fixture_profile, live_engine, workroot):
    tid = submit(client_admin, plain_project, fixture_profile, "WRITE .env <<< TOKEN=evil").json()["id"]
    wait_task(client_admin, tid, {"awaiting_review"})
    changes = client_admin.get(f"/api/tasks/{tid}/changes").json()["changes"]
    assert changes[0]["sensitive"] and not changes[0]["applicable"]
    r = client_admin.post(f"/api/tasks/{tid}/approve", {"files": [".env"], "confirm": True},
                          content_type="application/json")
    assert r.status_code == 409
    assert not (workroot / "plain" / ".env").exists()


@pytest.mark.django_db
def test_reject_and_cancel(client_admin, plain_project, fixture_profile, live_engine, workroot):
    tid = submit(client_admin, plain_project, fixture_profile, "WRITE hello.txt <<< nope").json()["id"]
    wait_task(client_admin, tid, {"awaiting_review"})
    r = client_admin.post(f"/api/tasks/{tid}/reject", {}, content_type="application/json")
    assert r.status_code == 200 and r.json()["state"] == "rejected"
    assert (workroot / "plain" / "hello.txt").read_text() == "hello\n"

    tid2 = submit(client_admin, plain_project, fixture_profile, "SLEEP 60").json()["id"]
    wait_task(client_admin, tid2, {"running"})
    assert client_admin.post(f"/api/tasks/{tid2}/cancel", {}, content_type="application/json").status_code == 200
    assert wait_task(client_admin, tid2, {"cancelled"}, timeout=20)["state"] == "cancelled"


@pytest.mark.django_db(transaction=True)
def test_sse_proxy_streams_and_persists(admin_user, plain_project, fixture_profile, live_engine):
    from django.test import Client

    c = Client()
    c.force_login(admin_user)
    tid = submit(c, plain_project, fixture_profile, "SAY hello stream\nWRITE s.txt <<< s").json()["id"]

    async def consume():
        ac = AsyncClient()
        await ac.aforce_login(admin_user)
        resp = await ac.get(f"/api/tasks/{tid}/events")
        assert resp.status_code == 200 and resp["Content-Type"] == "text/event-stream"
        chunks = []
        async for chunk in resp.streaming_content:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
            if '"type": "end"' in chunks[-1]:
                break
        return "".join(chunks)

    body = async_to_sync(consume)()
    assert "hello stream" in body and '"type": "end"' in body
    assert TaskEvent.objects.filter(task_id=tid, type="agent.message").exists()


@pytest.mark.django_db
def test_non_member_cannot_touch_tasks(client, plain_user, admin_user, plain_project, fixture_profile, live_engine):
    from django.test import Client

    c = Client()
    c.force_login(admin_user)
    tid = submit(c, plain_project, fixture_profile, "SAY hi").json()["id"]
    client.force_login(plain_user)
    assert client.get(f"/api/tasks/{tid}").status_code == 404
    assert client.post(f"/api/tasks/{tid}/approve", {"files": [], "confirm": True},
                       content_type="application/json").status_code == 404
    assert submit(client, plain_project, fixture_profile, "x").status_code == 404


# ---- post-task summary ------------------------------------------------------------

@pytest.mark.django_db
def test_research_task_reply_is_in_the_summary(client_admin, plain_project, fixture_profile, live_engine, workroot):
    """A task that changes no files still returns the agent's reply (e.g. research)."""
    reply = "# Findings\n\n- Option **A** is faster\n- Option B is simpler\n\nRecommendation: A."
    r = submit(client_admin, plain_project, fixture_profile, "REPLY " + reply.replace("\n", "\\n"))
    tid = r.json()["id"]
    t = wait_task(client_admin, tid, {"completed", "failed"})
    assert t["state"] == "completed", t
    a = t["assignments"][0]
    assert a["result"] == "# Findings\n\n- Option **A** is faster\n- Option B is simpler\n\nRecommendation: A."
    assert a["status"] == "succeeded" and a["exit_code"] == 0 and a["error"] == ""
    assert a["stats"]["num_turns"] == 1 and a["started_at"] and a["finished_at"]
    assert t["changes_summary"] == [] and t["started_at"] and t["finished_at"]
    # the list endpoint stays light (no reply bodies)
    listed = client_admin.get(f"/api/projects/{plain_project.id}/tasks").json()["tasks"]
    assert "result" not in listed[0]["assignments"][0]


@pytest.mark.django_db
def test_summary_lists_each_step_and_changed_files(client_admin, plain_project, fixture_profile, live_engine,
                                                   workroot):
    from agents_client.models import AgentProfile

    reviewer = AgentProfile.objects.create(name="Fixture reviewer", adapter=fixture_profile.adapter, role="reviewer",
                                           execution_policy={"timeout_seconds": 60})
    r = client_admin.post(f"/api/projects/{plain_project.id}/tasks", {
        "objective": "WRITE notes.txt <<< hi\nREPLY did the thing",
        "assignments": [{"profile_id": fixture_profile.id, "role": "implementer"},
                        {"profile_id": reviewer.id, "role": "reviewer"}]}, content_type="application/json")
    t = wait_task(client_admin, r.json()["id"], {"awaiting_review", "failed"})
    assert t["state"] == "awaiting_review", t
    assert [(a["order"], a["role"], a["status"], a["result"]) for a in t["assignments"]] == [
        (0, "implementer", "succeeded", "did the thing"), (1, "reviewer", "succeeded", "did the thing")]
    assert t["changes_summary"] == [{"path": "notes.txt", "action": "added", "binary": False, "sensitive": False,
                                     "size": 2}]


@pytest.mark.django_db
def test_failed_step_error_and_skipped_steps_in_summary(client_admin, plain_project, fixture_profile, live_engine,
                                                        workroot):
    from agents_client.models import AgentProfile

    second = AgentProfile.objects.create(name="Fixture 2", adapter=fixture_profile.adapter, role="reviewer",
                                         execution_policy={"timeout_seconds": 60})
    r = client_admin.post(f"/api/projects/{plain_project.id}/tasks", {
        "objective": "FAIL could not reach the docs site",
        "assignments": [{"profile_id": fixture_profile.id, "role": "implementer"},
                        {"profile_id": second.id, "role": "reviewer"}]}, content_type="application/json")
    t = wait_task(client_admin, r.json()["id"], {"failed"})
    first, rest = t["assignments"]
    assert first["status"] == "failed" and "exited with code 3" in first["error"] and first["exit_code"] == 3
    assert rest["status"] == "skipped" and rest["result"] == ""


@pytest.mark.django_db
def test_reply_secrets_are_redacted(client_admin, plain_project, fixture_profile, live_engine, workroot):
    r = submit(client_admin, plain_project, fixture_profile,
               "REPLY found key ghp_abcdefghijklmnopqrstuvwxyz0123456789AB in config")
    t = wait_task(client_admin, r.json()["id"], {"completed", "failed"})
    reply = t["assignments"][0]["result"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB" not in reply and "found key" in reply
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB" not in Task.objects.get(pk=t["id"]).assignments.get().result


def test_agent_switch_via_ide_api(client, admin_user, plain_user, live_engine):
    client.force_login(plain_user)
    r = client.post("/api/agents/antigravity/enabled", {"enabled": True}, content_type="application/json")
    assert r.status_code == 403  # admins only

    client.force_login(admin_user)
    r = client.post("/api/agents/antigravity/enabled", {"enabled": True}, content_type="application/json")
    assert r.status_code == 200 and r.json()["agent"]["user_enabled"] is True
    agents = {a["identifier"]: a for a in client.get("/api/agents").json()["adapters"]}
    assert agents["antigravity"]["user_enabled"] is True
    assert client.post("/api/agents/antigravity/enabled", {"enabled": "on"},
                       content_type="application/json").status_code == 400
    assert client.post("/api/agents/claude-code/enabled", {"enabled": False},
                       content_type="application/json").status_code == 400

    from audit.models import AuditEvent
    assert AuditEvent.objects.filter(action="agent.enabled", target="antigravity").exists()


@pytest.mark.django_db
def test_follow_up_continues_the_conversation(client_admin, plain_project, fixture_profile, live_engine, workroot):
    tid = submit(client_admin, plain_project, fixture_profile, "WRITE hello.txt <<< first\n\nREPLY checked").json()["id"]
    t = wait_task(client_admin, tid, {"awaiting_review", "failed"})
    assert t["state"] == "awaiting_review" and t["follow_up"] == {"available": True, "reason": None}
    assert t["assignments"][0]["can_resume"] is True

    post = lambda body: client_admin.post(f"/api/tasks/{tid}/followup", body, content_type="application/json")
    assert post({"message": "  "}).status_code == 400
    assert post({"message": "x", "step": 7}).status_code == 404
    r = post({"message": "WRITE docs/more.md <<< more\nREPLY done", "read_only": False})
    assert r.status_code == 200, r.content
    assert r.json()["state"] in ("queued", "preparing", "running") and r.json()["finished_at"] is None
    t = wait_task(client_admin, tid, {"awaiting_review", "failed"})
    assert t["state"] == "awaiting_review", t
    step = t["assignments"][1]
    assert step["followup_of"] == 0 and step["message"].startswith("WRITE docs/more.md") and step["result"] == "done"
    assert step["profile"] == fixture_profile.name and t["finished_at"]
    paths = {c["path"] for c in client_admin.get(f"/api/tasks/{tid}/changes").json()["changes"]}
    assert paths == {"hello.txt", "docs/more.md"}

    # rejecting drops the working copy: no more follow-ups
    assert client_admin.post(f"/api/tasks/{tid}/reject", {}, content_type="application/json").status_code == 200
    t = client_admin.get(f"/api/tasks/{tid}").json()
    assert t["follow_up"]["available"] is False and "rejected" in t["follow_up"]["reason"]
    assert post({"message": "again"}).status_code == 409


@pytest.mark.django_db
def test_follow_up_respects_project_access(client, plain_user, admin_user, plain_project, fixture_profile,
                                           live_engine):
    from django.test import Client

    admin = Client()
    admin.force_login(admin_user)
    tid = submit(admin, plain_project, fixture_profile, "SAY hi").json()["id"]
    wait_task(admin, tid, {"completed", "failed"})
    client.force_login(plain_user)
    r = client.post(f"/api/tasks/{tid}/followup", {"message": "x"}, content_type="application/json")
    assert r.status_code == 404


# ---- project checks + Verify step through the IDE ----------------------------------

@pytest.mark.django_db
def test_project_checks_settings_and_verify_step(client_admin, plain_user, plain_project, fixture_profile,
                                                 live_engine, workroot, monkeypatch):
    from agents_client.models import AgentProfile

    base = workroot / "plain"
    (base / "check_ok.py").write_text('import os\nprint("settings=" + os.environ.get("DJANGO_SETTINGS_MODULE", ""))\n',
                                      encoding="utf-8")
    (base / "prod_dry.csv").write_text("secret,data\n", encoding="utf-8")
    url = f"/api/projects/{plain_project.id}/checks"
    put = lambda body, c=client_admin: c.put(url, body, content_type="application/json")
    assert client_admin.get(url).json()["verify_commands"] == []
    assert put({"env": {"DATABASE_URL": "postgres://u:p@h/d"}}).status_code == 400
    assert put({"verify_commands": ["python check_ok.py | tee x"]}).status_code == 400
    assert put({"python_env": "../elsewhere"}).status_code == 400
    assert put({"python_env": "src"}).status_code == 400  # not a virtualenv

    adapter = client_admin.get("/api/agents").json()  # syncs the engine's adapters, including "verify"
    assert any(a["identifier"] == "verify" and a["available"] for a in adapter["adapters"])
    verify = AgentProfile.objects.create(name="Verify", adapter_id=AgentAdapter.objects.get(identifier="verify").pk,
                                         role="verify", execution_policy={"read_only": True, "timeout_seconds": 120})
    lineup = {"objective": "WRITE out.txt <<< hi", "assignments": [{"profile_id": fixture_profile.id},
                                                                    {"profile_id": verify.id}]}
    r = client_admin.post(f"/api/projects/{plain_project.id}/tasks", lineup, content_type="application/json")
    assert r.status_code == 400 and "no check commands" in r.json()["error"]

    r = put({"verify_commands": ["python check_ok.py"], "env": "DJANGO_SETTINGS_MODULE=plain.settings_test",
             "exclude": ["*.csv"], "python_env": ""})
    assert r.status_code == 200, r.content
    assert r.json()["env"] == {"DJANGO_SETTINGS_MODULE": "plain.settings_test"} and r.json()["can_edit"] is True
    from django.test import Client

    other = Client()
    other.force_login(plain_user)
    assert put({"verify_commands": []}, c=other).status_code in (403, 404)  # not staff (or not a member)

    tid = client_admin.post(f"/api/projects/{plain_project.id}/tasks", lineup,
                            content_type="application/json").json()["id"]
    t = wait_task(client_admin, tid, {"awaiting_review", "failed", "completed"}, timeout=60)
    assert t["state"] == "awaiting_review", t
    step = t["assignments"][1]
    assert step["adapter"] == "verify" and step["stats"] == {"checks_total": 1, "checks_failed": 0}
    assert "PASS `python check_ok.py`" in step["result"]
    events = json.dumps(list(TaskEvent.objects.filter(task_id=tid).values_list("payload", flat=True)))
    assert "settings=plain.settings_test" in events
    assert {c["path"] for c in client_admin.get(f"/api/tasks/{tid}/changes").json()["changes"]} == {"out.txt"}
