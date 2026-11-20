"""Agent Engine contract v1 tested directly over HTTP (signed requests)."""
import json
import time

import httpx
import pytest

from cs_common import signing
from tests.conftest import TOKEN, wait_for


class Signed:
    def __init__(self, base, token=TOKEN):
        self.base, self.token = base, token

    def req(self, method, path, payload=None, token=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        r = httpx.Request(method, self.base + path, content=body or None)
        r.headers.update(signing.sign(token or self.token, method, r.url.raw_path.decode(), body))
        with httpx.Client(timeout=30) as c:
            return c.send(r)


@pytest.fixture
def eng(live_engine):
    return Signed(live_engine.url)


@pytest.fixture
def src(tmp_path):
    d = tmp_path / "srcproj"
    d.mkdir()
    (d / "a.txt").write_text("alpha\n", encoding="utf-8", newline="")
    (d / ".env").write_text("API_KEY=supersecretvalue123\n", encoding="utf-8", newline="")
    (d / "node_modules").mkdir()
    (d / "node_modules" / "big.js").write_text("x", encoding="utf-8", newline="")
    return d


def submit(eng, src, objective, adapter="fixture", **policy):
    r = eng.req("POST", "/tasks", {"objective": objective, "project": {"id": 1, "name": "p", "root": str(src)},
                                   "assignments": [{"adapter": adapter, "role": "implementer", "policy": policy}]})
    return r


def wait_state(eng, tid, states, timeout=30):
    return wait_for(lambda: (lambda t: t if t["state"] in states else None)(eng.req("GET", f"/tasks/{tid}").json()),
                    timeout=timeout)


def test_auth_required(live_engine):
    with httpx.Client() as c:
        assert c.get(live_engine.url + "/health").status_code == 401
    bad = Signed(live_engine.url, token="wrong-token")
    assert bad.req("GET", "/health").status_code == 401
    # replayed/old timestamp rejected
    r = httpx.Request("GET", live_engine.url + "/health")
    r.headers.update(signing.sign(TOKEN, "GET", "/health", b"", ts=int(time.time()) - 3600))
    with httpx.Client() as c:
        assert c.send(r).status_code == 401


def test_health_and_agents(eng):
    h = eng.req("GET", "/health").json()
    assert h["status"] == "ok" and h["api_version"] == "1"
    agents = {a["identifier"]: a for a in eng.req("GET", "/agents").json()["agents"]}
    assert {"claude-code", "codex", "antigravity", "fixture"} <= set(agents)
    assert agents["antigravity"]["available"] is False  # honest: no verified headless interface
    assert agents["fixture"]["capabilities"]["test_only"] is True


def test_rejects_bad_source_paths(eng, tmp_path):
    for root in ("relative/path", str(tmp_path / "missing"), "C:\\"):
        r = eng.req("POST", "/tasks", {"objective": "x", "project": {"root": root},
                                       "assignments": [{"adapter": "fixture"}]})
        assert r.status_code == 400, root


def test_task_lifecycle_changes_and_isolation(eng, src):
    r = submit(eng, src, "SAY starting\nWRITE a.txt <<< alpha\\nbeta\\n\nWRITE new/b.txt <<< bee\nDELETE gone.txt")
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    t = wait_state(eng, tid, {"awaiting_review", "failed"})
    assert t["state"] == "awaiting_review", t
    # live source untouched
    assert (src / "a.txt").read_text() == "alpha\n" and not (src / "new").exists()
    changes = {c["path"]: c for c in eng.req("GET", f"/tasks/{tid}/changes").json()["changes"]}
    assert changes["a.txt"]["action"] == "modified" and "+beta" in changes["a.txt"]["diff"]
    assert changes["new/b.txt"]["action"] == "added"
    assert ".env" not in changes and not any(p.startswith("node_modules") for p in changes)
    # events are ordered with timestamps; the secret never appears
    ev = eng.req("GET", f"/tasks/{tid}/events?after=0&follow=0").json()["events"]
    seqs = [e["seq"] for e in ev]
    assert seqs == sorted(seqs) and all("ts" in e for e in ev)
    assert "supersecretvalue123" not in json.dumps(ev)
    types = [e["type"] for e in ev]
    assert "workspace.ready" in types and "agent.tool_use" in types and "changes.ready" in types
    # approval is recorded (engine never writes live files)
    r = eng.req("POST", f"/tasks/{tid}/approve", {"decision": "applied", "files": ["a.txt"], "actor": "t"})
    assert r.status_code == 200 and r.json()["state"] == "applied"
    assert (src / "a.txt").read_text() == "alpha\n"
    assert eng.req("POST", f"/tasks/{tid}/approve", {"decision": "applied"}).status_code == 409


def test_sse_stream(eng, src):
    tid = submit(eng, src, "SAY one\nSAY two").json()["id"]
    path = f"/tasks/{tid}/events?after=0&follow=1"
    r = httpx.Request("GET", eng.base + path)
    r.headers.update(signing.sign(TOKEN, "GET", path, b""))
    events = []
    with httpx.Client(timeout=30) as c:
        resp = c.send(r, stream=True)
        try:
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    events.append(line[6:].strip())
                if events and events[-1] == "end":
                    break
        finally:
            resp.close()
    assert "task.submitted" in events and "agent.message" in events and events[-1] == "end"


def test_cancel_terminates_process(eng, src):
    tid = submit(eng, src, "SLEEP 60").json()["id"]
    wait_for(lambda: any(e["type"] == "agent.started" for e in
                         eng.req("GET", f"/tasks/{tid}/events?after=0&follow=0").json()["events"]), timeout=20)
    t0 = time.time()
    assert eng.req("POST", f"/tasks/{tid}/cancel", {}).json()["cancelled"] is True
    t = wait_state(eng, tid, {"cancelled"}, timeout=20)
    assert time.time() - t0 < 15
    assert t["assignments"][0]["status"] == "cancelled"


def test_timeout(eng, src):
    tid = submit(eng, src, "SLEEP 30", timeout_seconds=5).json()["id"]
    t = wait_state(eng, tid, {"failed"}, timeout=30)
    assert t["assignments"][0]["status"] == "timed_out"


def test_failure_reported(eng, src):
    tid = submit(eng, src, "FAIL boom").json()["id"]
    t = wait_state(eng, tid, {"failed"})
    assert "exited with code 3" in t["error"]


def test_unavailable_adapter_fails_cleanly(eng, src):
    tid = submit(eng, src, "do something", adapter="antigravity").json()["id"]
    t = wait_state(eng, tid, {"failed"})
    assert "not available" in t["error"]


def test_restart_marks_inflight_interrupted(tmp_path):
    from agent_engine.config import EngineConfig
    from agent_engine.tasks import TaskManager

    cfg = EngineConfig(data_dir=tmp_path, token=TOKEN)
    tdir = cfg.tasks_dir / "abc"
    tdir.mkdir(parents=True)
    (tdir / "task.json").write_text(json.dumps({"id": "abc", "state": "running", "created_at": 0}), encoding="utf-8", newline="")
    mgr = TaskManager(cfg, {})
    assert mgr.tasks["abc"].state == "interrupted"


def test_antigravity_switch_changes_without_restart(eng, live_engine):
    def antigravity():
        return {a["identifier"]: a for a in eng.req("GET", "/agents").json()["agents"]}["antigravity"]

    assert antigravity()["user_enabled"] is False
    r = eng.req("PUT", "/agents/antigravity", {"enabled": True})
    assert r.status_code == 200 and r.json()["agent"]["user_enabled"] is True
    assert antigravity()["user_enabled"] is True  # probe cache was refreshed, same engine process
    saved = json.loads((live_engine.config.engine_dir / "settings.json").read_text())
    assert saved["adapters"]["antigravity"]["enabled"] is True

    assert eng.req("PUT", "/agents/antigravity", {"enabled": False}).json()["agent"]["user_enabled"] is False
    assert eng.req("PUT", "/agents/antigravity", {"enabled": "yes"}).status_code == 400
    assert eng.req("PUT", "/agents/claude-code", {"enabled": False}).status_code == 400  # no switch
    assert eng.req("PUT", "/agents/nope", {"enabled": True}).status_code == 404
    assert {a["identifier"]: a for a in eng.req("GET", "/agents").json()["agents"]}["claude-code"]["user_enabled"] is None


def test_settings_store_seeds_once_then_persists(tmp_path):
    from agent_engine.settings_store import SettingsStore

    path = tmp_path / "engine" / "settings.json"
    store = SettingsStore(path, seed={"antigravity": {"enabled": True}})  # legacy env switch was on
    assert store.adapter("antigravity")["enabled"] is True
    store.update_adapter("antigravity", enabled=False)
    # a restart with the old env switch still set must not override the saved choice
    again = SettingsStore(path, seed={"antigravity": {"enabled": True}})
    assert again.adapter("antigravity")["enabled"] is False


def test_antigravity_adapter_obeys_switch(tmp_path, monkeypatch):
    from agent_engine.adapters.antigravity import AntigravityAdapter
    from agent_engine.settings_store import SettingsStore

    adapter = AntigravityAdapter(SettingsStore(tmp_path / "s.json"))
    monkeypatch.setattr(adapter, "find_executable", lambda: "agy")
    monkeypatch.setattr(adapter, "_run_quick", lambda argv, timeout=20: (0, "agy 1.5.10\n"))
    monkeypatch.setattr(adapter, "keyring_login", lambda: True)
    off = adapter.probe()
    assert off.available is False and off.user_enabled is False and "Turned off" in off.notes[0]
    adapter.set_enabled(True)
    on = adapter.probe()
    assert on.available is True and on.user_enabled is True


# ---- follow-ups (resume a step's conversation in the kept staging copy) ----

def events_of(eng, tid):
    return eng.req("GET", f"/tasks/{tid}/events?after=0&follow=0").json()["events"]


def test_followup_resumes_session_and_accumulates_changes(eng, src, live_engine):
    tid = submit(eng, src, "WRITE a.txt <<< alpha\nbeta\n").json()["id"]
    t = wait_state(eng, tid, {"awaiting_review", "failed"})
    assert t["state"] == "awaiting_review" and t["can_follow_up"] is True
    sid = t["assignments"][0]["session_id"]
    assert sid
    assert (live_engine.config.engine_dir / "workspaces" / tid / "work").is_dir()

    r = eng.req("POST", f"/tasks/{tid}/followup", {"message": "WRITE extra.txt <<< more\nREPLY fixed it"})
    assert r.status_code == 200, r.text
    t = wait_state(eng, tid, {"awaiting_review", "failed"})
    assert t["state"] == "awaiting_review", t
    second = t["assignments"][1]
    assert second["followup_of"] == 0 and second["resume_session"] == sid and second["result"] == "fixed it"
    # the CLI was resumed with the first step's session id, and got only the follow-up (not the objective again)
    texts = [e["payload"].get("text", "") for e in events_of(eng, tid) if e["payload"].get("assignment") == 1]
    assert f"resumed session {sid}" in texts
    # edits from both turns are in one change set
    changes = {c["path"] for c in eng.req("GET", f"/tasks/{tid}/changes").json()["changes"]}
    assert changes == {"a.txt", "extra.txt"}
    assert (src / "a.txt").read_text() == "alpha\n"  # live project still untouched


def test_followup_after_cancel_and_access_override(eng, src):
    tid = submit(eng, src, "SLEEP 60").json()["id"]
    wait_for(lambda: wait_state(eng, tid, {"running"}) and eng.req("GET", f"/tasks/{tid}").json()["assignments"][0]
             .get("session_id"), timeout=20)
    eng.req("POST", f"/tasks/{tid}/cancel", {})
    t = wait_state(eng, tid, {"cancelled"}, timeout=20)
    assert t["can_follow_up"] is True  # the session id was saved while it ran
    r = eng.req("POST", f"/tasks/{tid}/followup", {"message": "SAY carry on", "read_only": True})
    assert r.status_code == 200, r.text
    t = wait_state(eng, tid, {"completed", "failed"})
    assert t["state"] == "completed" and t["error"] is None
    assert t["assignments"][1]["policy"]["read_only"] is True and t["assignments"][1]["status"] == "succeeded"


def test_followup_refused_when_not_possible(eng, src):
    tid = submit(eng, src, "WRITE a.txt <<< changed").json()["id"]
    wait_state(eng, tid, {"awaiting_review"})
    assert eng.req("POST", f"/tasks/{tid}/followup", {"message": ""}).status_code == 400
    assert eng.req("POST", f"/tasks/{tid}/followup", {"message": "x", "resume_from": 5}).status_code == 409
    eng.req("POST", f"/tasks/{tid}/approve", {"decision": "rejected"})
    t = eng.req("GET", f"/tasks/{tid}").json()
    assert t["can_follow_up"] is False
    r = eng.req("POST", f"/tasks/{tid}/followup", {"message": "more"})
    assert r.status_code == 409 and "cannot be followed up" in r.text
    # a running task cannot be followed up either
    busy = submit(eng, src, "SLEEP 30").json()["id"]
    wait_state(eng, busy, {"running"})
    assert eng.req("POST", f"/tasks/{busy}/followup", {"message": "x"}).status_code == 409
    eng.req("POST", f"/tasks/{busy}/cancel", {})


def test_staging_copy_expires(tmp_path):
    from agent_engine.config import EngineConfig
    from agent_engine.tasks import TaskManager

    cfg = EngineConfig(data_dir=tmp_path, token=TOKEN, workspace_retention_days=1)
    for tid, age in (("old", 3 * 86400), ("new", 60)):
        (cfg.tasks_dir / tid).mkdir(parents=True)
        (cfg.workspaces_dir / tid / "work").mkdir(parents=True)
        (cfg.tasks_dir / tid / "task.json").write_text(json.dumps(
            {"id": tid, "state": "completed", "created_at": 0, "updated_at": time.time() - age,
             "manifest": {"a.txt": "x"}, "assignments": []}), encoding="utf-8")
    mgr = TaskManager(cfg, {})
    assert mgr.tasks["old"].public()["can_follow_up"] is False and not (cfg.workspaces_dir / "old").exists()
    assert mgr.tasks["new"].public()["can_follow_up"] is True and (cfg.workspaces_dir / "new").exists()


def test_adapters_build_resume_commands(tmp_path, monkeypatch):
    from agent_engine.adapters.antigravity import AntigravityAdapter, conversation_id
    from agent_engine.adapters.claude_code import ClaudeCodeAdapter
    from agent_engine.adapters.codex import CodexAdapter

    pol = {"resume_session": "S1", "read_only": True, "model": "m"}
    claude = ClaudeCodeAdapter()
    monkeypatch.setattr(claude, "find_executable", lambda: "claude")
    argv, _ = claude.build_command("p", tmp_path, pol)
    assert argv[argv.index("--resume") + 1] == "S1" and "plan" in argv
    codex = CodexAdapter()
    monkeypatch.setattr(codex, "_argv0", lambda: ["codex"])
    argv, stdin = codex.build_command("p", tmp_path, pol)
    assert argv[:3] == ["codex", "exec", "resume"] and argv[-2:] == ["S1", "-"] and stdin == "p"
    assert 'sandbox_mode="read-only"' in argv and "-C" not in argv
    argv, _ = codex.build_command("p", tmp_path, {"read_only": False})
    assert "resume" not in argv and "workspace-write" in argv
    agy = AntigravityAdapter()
    monkeypatch.setattr(agy, "find_executable", lambda: "agy")
    argv, _ = agy.build_command("p", tmp_path, pol)
    assert "--conversation=S1" in argv
    assert conversation_id({"event": "result", "result": {"conversationId": "C9"}}) == "C9"
    evs = agy.parse_line(json.dumps({"event": "start", "conversation_id": "C1"}))
    assert evs[0]["payload"]["conversation_id"] == "C1"


def test_antigravity_login_check_and_read_only_flags(tmp_path, monkeypatch):
    from agent_engine.adapters import antigravity as ag
    from agent_engine.settings_store import SettingsStore

    adapter = ag.AntigravityAdapter(SettingsStore(tmp_path / "s.json"))
    adapter.set_enabled(True)
    monkeypatch.setattr(adapter, "find_executable", lambda: "agy")
    monkeypatch.setattr(ag.os, "name", "nt")
    monkeypatch.setattr(ag.shutil, "which", lambda name: "cmdkey" if name == "cmdkey" else None)
    stored = {"out": ""}

    def fake_run(argv, timeout=20):
        if argv[0] == "cmdkey":
            assert argv[1] == "/list:gemini:antigravity"
            return 0, stored["out"]
        return 0, "1.5.12\n"

    monkeypatch.setattr(adapter, "_run_quick", fake_run)
    stored["out"] = "Currently stored credentials for gemini:antigravity:\n\n* NONE *\n"
    off = adapter.probe()
    assert off.available is False and off.auth_state == "logged_out" and "not logged in" in off.notes[0]
    adapter.invalidate_probe()
    stored["out"] = ("Currently stored credentials for gemini:antigravity:\n\n    Target: gemini:antigravity\n"
                     "    Type: Generic \n    User: antigravity\n")
    on = adapter.probe()
    assert on.available is True and on.auth_state == "logged_in"
    # where the keyring cannot be checked, login is assumed (previous behaviour)
    adapter.invalidate_probe()
    monkeypatch.setattr(ag.os, "name", "posix")
    assert adapter.probe().auth_state == "assumed_logged_in"

    # agy ignores --mode plan when slash commands are disabled: read-only runs must not disable them
    ro, _ = adapter.build_command("p", tmp_path, {"read_only": True})
    assert "plan" in ro and "--disable-slash-commands" not in ro
    rw, _ = adapter.build_command("p", tmp_path, {"read_only": False})
    assert "accept-edits" in rw and "--disable-slash-commands" in rw
    # the verified record shapes carry the conversation id
    init = adapter.parse_line('{"event":"init","conversation_id":"851f0e5e-eb01-4ef0-a9bd-10a351a6a9db","init":{}}')
    assert init[0]["payload"]["conversation_id"] == "851f0e5e-eb01-4ef0-a9bd-10a351a6a9db"
    res = adapter.parse_line('{"event":"result","result":{"conversation_id":"c1","status":"SUCCESS","response":"ok"}}')
    assert res[-1]["type"] == "agent.result" and res[-1]["payload"]["text"] == "ok"


def test_earlier_replies_reach_later_steps_by_role():
    from agent_engine.tasks import build_prompt, fit_replies, reply_limit, step_summary

    plan = "PLAN " + "x" * 15_000 + " FILES: a.py, b.py"   # the end of a plan is where the file list is
    review = "R" * 7_000 + " MUST-FIX: rename"
    status = "S" * 5_000
    status = "S" * 5_000 + " CAVEAT: the login page is unsafe"   # implementers put caveats at the end
    arch = step_summary({"index": 0, "role": "architect", "adapter": "claude-code", "result": plan}, "succeeded")
    rev = step_summary({"index": 2, "role": "Senior Reviewer", "adapter": "antigravity", "result": review}, "succeeded")
    imp = step_summary({"index": 1, "role": "implementer", "adapter": "codex", "result": status}, "succeeded")
    assert (arch["limit"], rev["limit"], imp["limit"]) == (20_000, 8_000, 4_000)
    assert imp["file"] == "step-02-implementer.md" and rev["file"] == "step-03-senior-reviewer.md"
    assert reply_limit({"role": "verify", "adapter": "verify"}) == 12_000
    prompt = build_prompt("implementer", "do it", [arch, imp, rev], False)
    assert "FILES: a.py, b.py" in prompt and "MUST-FIX: rename" in prompt   # whole plan and findings
    # a long status reply keeps its start and its end, and says where the whole reply is
    assert "characters not shown here; the full reply is in .codestudio-task/step-02-implementer.md" in prompt
    assert "CAVEAT: the login page is unsafe" in prompt
    # over the shared budget: short replies stay whole, long ones share the rest
    texts = fit_replies([dict(arch), dict(rev, summary="short")], budget=10_000)
    head, _, tail = texts[0].partition("\n[...")
    assert texts[1] == "short" and len(head) + len(tail.split("...]\n", 1)[1]) == 10_000 - len("short")
    assert texts[0].endswith("FILES: a.py, b.py")


def test_prompt_points_at_handoff_and_review_findings():
    from agent_engine.tasks import build_followup_prompt, build_prompt, step_summary

    rev = step_summary({"index": 1, "role": "reviewer", "adapter": "codex", "result": "1. bug"}, "succeeded")
    fixer = build_prompt("implementer", "do it", [rev], False)
    assert ".codestudio-task/" in fixer and "do not edit it" in fixer
    assert "full findings: .codestudio-task/step-02-reviewer.md" in fixer and "fixed or not fixed" in fixer
    # read-only steps and other reviewers are not told to fix findings; the first step has no handoff yet
    assert "fixed or not fixed" not in build_prompt("implementer", "do it", [rev], True)
    assert "fixed or not fixed" not in build_prompt("second reviewer", "do it", [rev], True)
    first = build_prompt("architect", "do it", [], True)
    assert ".codestudio-task" not in first and "concise" not in first and "file:line" in first
    assert ".codestudio-task/" in build_followup_prompt("again", False, False, [])


def test_handoff_folder_is_rewritten_from_the_record(tmp_path):
    from agent_engine.tasks import write_handoff

    work = tmp_path / "work"
    work.mkdir()
    data = {"objective": "obj", "assignments": [
        {"index": 0, "role": "architect", "adapter": "claude-code", "profile": "Claude", "status": "succeeded",
         "result": "THE PLAN", "error": None, "policy": {"read_only": True}},
        {"index": 1, "role": "implementer", "adapter": "codex", "profile": "codex", "status": "pending",
         "result": None, "error": None, "policy": {}}]}
    changes = [{"path": "a.py", "action": "modified", "diff": "--- a/a.py\n+++ b/a.py\n+x\n"},
               {"path": ".env", "action": "added", "diff": "+SECRET=1\n", "sensitive": True}]
    write_handoff(work, data, changes, current=1)
    root = work / ".codestudio-task"
    (root / "stray.md").write_text("agent scribble", encoding="utf-8")   # an agent wrote here
    (root / "step-01-architect.md").write_text("tampered", encoding="utf-8")
    write_handoff(work, data, changes, current=1)
    assert not (root / "stray.md").exists()
    md = (root / "step-01-architect.md").read_text(encoding="utf-8")
    assert "THE PLAN" in md and "- Agent: claude-code (Claude)" in md and "Read-only: yes" in md
    index = json.loads((root / "task.json").read_text(encoding="utf-8"))
    assert [s["file"] for s in index["steps"]] == ["step-01-architect.md", None]
    assert index["steps"][1]["current"] is True and index["changes_file"] == "changes.diff"
    diff = (root / "changes.diff").read_text(encoding="utf-8")
    assert "+x" in diff and "SECRET" not in diff and ".env: sensitive file" in diff


def test_every_step_gets_full_earlier_replies_in_handoff(eng, src, live_engine):
    plan = "P" * 15_000 + " END-OF-NOTES"   # an "analyst" reply: prompts carry 4 000 characters of it
    r = eng.req("POST", "/tasks", {
        "objective": f"WRITE a.txt <<< changed\\n\nREPLY {plan}", "project": {"id": 1, "name": "p", "root": str(src)},
        "assignments": [{"adapter": "fixture", "role": "analyst"}, {"adapter": "fixture", "role": "implementer"}]})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    t = wait_state(eng, tid, {"awaiting_review", "failed"})
    assert t["state"] == "awaiting_review", t
    root = live_engine.config.engine_dir / "workspaces" / tid / "work" / ".codestudio-task"
    assert plan in (root / "step-01-analyst.md").read_text(encoding="utf-8")   # whole
    index = json.loads((root / "task.json").read_text(encoding="utf-8"))
    assert [s["file"] for s in index["steps"]] == ["step-01-analyst.md", "step-02-implementer.md"]
    assert "+changed" in (root / "changes.diff").read_text(encoding="utf-8")
    # the handoff folder never reaches the change set
    paths = {c["path"] for c in eng.req("GET", f"/tasks/{tid}/changes").json()["changes"]}
    assert paths == {"a.txt"}


def test_prompt_shrinks_to_fit_antigravity_on_windows(tmp_path, monkeypatch):
    from agent_engine.adapters.antigravity import AntigravityAdapter
    from agent_engine.config import EngineConfig
    from agent_engine.tasks import EngineTask, TaskManager

    agy = AntigravityAdapter()
    monkeypatch.setattr(agy, "max_prompt_chars", lambda: 26_000)
    mgr = TaskManager(EngineConfig(data_dir=tmp_path, token=TOKEN), {"antigravity": agy})
    task = EngineTask({"id": "t", "state": "running", "objective": "build it", "assignments": []}, tmp_path)
    mgr.emit = lambda *a, **k: None
    base, work = tmp_path / "base", tmp_path / "work"
    base.mkdir(), work.mkdir()
    (work / "big.py").write_text("line\n" * 8000, encoding="utf-8")   # a 40 KB added file -> a large diff
    plans = [{"role": "architect", "adapter": "claude-code", "status": "succeeded", "summary": "P" * 18_000,
              "limit": 20_000} for _ in range(3)]
    a = {"index": 3, "role": "reviewer", "adapter": "antigravity", "policy": {"read_only": True}}
    prompt = mgr._prompt_for(task, a, plans, {"base": str(base), "work": str(work), "manifest": {}}, work)
    assert len(prompt) <= 26_000 and "Objective:\nbuild it" in prompt and "big.py" in prompt


def test_generate_one_shot_text(eng, live_engine):
    r = eng.req("POST", "/generate", {"adapter": "fixture", "prompt": "SAY hi\nREPLY ```text\\nAdd thing\\n```"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "Add thing" and body["adapter"] == "fixture"  # code fence stripped
    scratch = live_engine.config.engine_dir / "scratch"
    assert not any(scratch.iterdir())  # the empty run folder is removed afterwards
    assert eng.req("POST", "/generate", {"adapter": "fixture", "prompt": "FAIL nope"}).status_code == 502
    assert eng.req("POST", "/generate", {"adapter": "fixture", "prompt": " "}).status_code == 400
    assert eng.req("POST", "/generate", {"adapter": "nope", "prompt": "x"}).status_code == 404
    assert eng.req("POST", "/generate", {"adapter": "antigravity", "prompt": "x"}).status_code == 409
