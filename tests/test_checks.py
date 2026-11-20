"""Project checks: the Verify step (runs commands, not an AI) and the environment guardrail."""
import json
import os
import stat

import pytest

from cs_common.agent_policy import PolicyError, clean_check_env, clean_excludes, is_excluded
from cs_common.redact import agent_env, is_database_env
from tests.test_engine import eng, events_of, src, wait_state  # noqa: F401  (fixtures)

CHECK_OK = '''import os, pathlib
pathlib.Path("written_by_check.txt").write_text("x")
print("settings=" + os.environ.get("DJANGO_SETTINGS_MODULE", ""))
print("db_vars=" + ",".join(sorted(k for k in os.environ if k.upper() in ("DATABASE_URL", "DB_HOST", "PGPASSWORD"))))
print("cred_url=" + str(any("@" in v and "://" in v for v in os.environ.values())))
'''
CHECK_FAIL = 'import sys\nprint("boom: 2 tests failed")\nsys.exit(4)\n'


def make_venv_with_tool(project):
    """A stand-in virtualenv whose bin folder has a 'projtool' command (proves PATH order)."""
    venv = project / ".venv"
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    if os.name == "nt":
        (bin_dir / "projtool.cmd").write_text("@echo projtool from the project venv %*\r\n", encoding="utf-8")
    else:
        tool = bin_dir / "projtool"
        tool.write_text("#!/bin/sh\necho projtool from the project venv \"$@\"\n", encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    return venv


def submit_with_checks(eng, src, objective, checks, lineup=("fixture", "verify")):
    return eng.req("POST", "/tasks", {
        "objective": objective, "project": {"id": 1, "name": "p", "root": str(src), "checks": checks},
        "assignments": [{"adapter": a, "role": "verify" if a == "verify" else "implementer",
                         "policy": {"timeout_seconds": 120}} for a in lineup]})


def test_verify_step_reports_real_results_without_touching_the_change_set(eng, src, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://app:hunter2pass@db.internal/statz")
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("SOME_ENDPOINT", "mssql://sa:Sup3rSecret@10.0.0.5/prod")
    (src / "check_ok.py").write_text(CHECK_OK, encoding="utf-8")
    (src / "check_fail.py").write_text(CHECK_FAIL, encoding="utf-8")
    (src / "prod_dry.csv").write_text("customer,amount\n", encoding="utf-8")
    venv = make_venv_with_tool(src)
    checks = {"verify_commands": ["python check_ok.py", "python check_fail.py", "projtool run"],
              "env": {"DJANGO_SETTINGS_MODULE": "proj.settings_test"}, "exclude": ["*.csv"],
              "python_env": str(venv)}
    r = submit_with_checks(eng, src, "WRITE feature.txt <<< new", checks)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    t = wait_state(eng, tid, {"awaiting_review", "failed", "completed"}, timeout=60)
    assert t["state"] == "awaiting_review", t
    verify = t["assignments"][1]
    assert verify["status"] == "succeeded", verify  # the commands ran; whether checks passed is in the report
    assert verify["stats"]["checks_total"] == 3 and verify["stats"]["checks_failed"] == 1
    report = verify["result"]
    assert "1 of 3 failed" in report and "PASS `python check_ok.py`" in report
    assert "FAIL `python check_fail.py` (exit 4" in report and "boom: 2 tests failed" in report
    out = "\n".join(e["payload"].get("text", "") for e in events_of(eng, tid) if e["type"] == "agent.output")
    assert "settings=proj.settings_test" in out          # forced test settings
    assert "db_vars=\n" in out + "\n" and "cred_url=False" in out  # database settings never reach checks
    assert "projtool from the project venv run" in out   # the project's venv is first on PATH
    # files written by checks stay out of the change set; excluded files never reached the copy
    changes = {c["path"] for c in eng.req("GET", f"/tasks/{tid}/changes").json()["changes"]}
    assert changes == {"feature.txt"}
    assert t["workspace"]["excluded_by_project"] == 1
    assert "hunter2pass" not in json.dumps(events_of(eng, tid))


def test_later_steps_receive_the_check_report(eng, src):
    (src / "check_fail.py").write_text(CHECK_FAIL, encoding="utf-8")
    checks = {"verify_commands": ["python check_fail.py"]}
    # fixture -> verify -> fixture: the last step's prompt holds the failure output
    tid = submit_with_checks(eng, src, "SAY go", checks, lineup=("fixture", "verify", "fixture")).json()["id"]
    t = wait_state(eng, tid, {"awaiting_review", "failed", "completed"}, timeout=60)
    assert [a["status"] for a in t["assignments"]] == ["succeeded"] * 3
    received = [e["payload"]["text"] for e in events_of(eng, tid)
                if e["type"] == "agent.message" and e["payload"].get("assignment") == 2
                and "received" in e["payload"].get("text", "")]
    first = [e["payload"]["text"] for e in events_of(eng, tid)
             if e["type"] == "agent.message" and e["payload"].get("assignment") == 0
             and "received" in e["payload"].get("text", "")]
    # the third step's prompt is longer by (at least) the report with the failure output
    size = lambda s: int(s[0].split()[3])
    assert size(received) > size(first) + len("boom: 2 tests failed")


def test_verify_needs_commands_and_validates_settings(eng, src):
    r = submit_with_checks(eng, src, "x", {})
    assert r.status_code == 400 and "no check commands" in r.text
    r = submit_with_checks(eng, src, "x", {"verify_commands": ["rm -rf /"]})
    assert r.status_code == 400
    r = submit_with_checks(eng, src, "x", {"verify_commands": ["python -m pytest"], "env": {"PATH": "/evil"}})
    assert r.status_code == 400 and "PATH" in r.text
    r = submit_with_checks(eng, src, "x", {"verify_commands": ["python -m pytest"],
                                           "python_env": str(src.parent)})  # outside the project
    assert r.status_code == 400 and "inside the project" in r.text


def test_guardrail_rules():
    env = {"DATABASE_URL": "postgres://u:p@h/d", "DB_NAME": "statz", "PGPASSWORD": "x", "MYSQL_HOST": "h",
           "REDIS_URL": "redis://h", "LEGACY_DATABASE_URL": "x", "HTTP_PROXY": "http://u:secret@proxy:8080",
           "ANTHROPIC_BASE_URL": "https://api.example.com", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run",
           "PATH": "/usr/bin", "OPENAI_API_KEY": "sk-x"}
    kept = agent_env(env, keep=("OPENAI_API_KEY",))
    assert set(kept) == {"ANTHROPIC_BASE_URL", "PATH", "OPENAI_API_KEY"}
    assert not is_database_env("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run")  # DB_ needs the underscore
    assert is_database_env("X", "sqlserver://sa:pw@host") and not is_database_env("X", "https://host/path")
    assert clean_check_env("DJANGO_SETTINGS_MODULE=app.settings_test\n# note\n") == {
        "DJANGO_SETTINGS_MODULE": "app.settings_test"}
    for bad in ("PATH=x", "DATABASE_URL=sqlite:///x", "URL=postgres://u:p@h/d", "LD_PRELOAD=x", "nope"):
        with pytest.raises(PolicyError):
            clean_check_env(bad)
    assert clean_excludes("*.csv\n data/prod_* \n# c\n") == ["*.csv", "data/prod_*"]
    with pytest.raises(PolicyError):
        clean_excludes("../secrets")
    assert is_excluded("exports/prod_dry.csv", ["*.csv"]) and is_excluded("data/prod/a.txt", ["data/prod"])
    assert is_excluded("Data/Prod_1.txt", ["data/prod_*"]) and not is_excluded("src/app.py", ["*.csv", "data/*"])
