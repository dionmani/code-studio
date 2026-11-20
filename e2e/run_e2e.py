"""Live end-to-end verification against real servers in a real browser.

    .venv\\Scripts\\python e2e\\run_e2e.py [--real-agents] [--headed]

Starts the Agent Engine and the Django IDE (daphne via `manage.py runserver`)
with an isolated data dir, then drives Chromium through every workflow:
login, root + project registration (Git and non-Git), file create/edit/save/
rename/delete, external-edit conflict resolution, search, scoped terminal,
Git status/diff/stage/commit, agent task submit/stream/review/approve,
recovery snapshots, and IDE behaviour with the engine stopped.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
IDE_PORT, ENGINE_PORT = 8001, 8766
IDE = f"http://127.0.0.1:{IDE_PORT}"
USER, PASSWORD = "e2e-admin", "E2e-" + secrets.token_hex(6)

results: list[tuple[str, bool, str]] = []


def step(name):
    def deco(fn):
        def run(*a, **kw):
            t0 = time.time()
            try:
                fn(*a, **kw)
                results.append((name, True, f"{time.time() - t0:.1f}s"))
                print(f"  PASS  {name} ({time.time() - t0:.1f}s)", flush=True)
            except Exception as exc:
                results.append((name, False, f"{type(exc).__name__}: {str(exc)[:300]}"))
                print(f"  FAIL  {name}: {exc}", flush=True)
                traceback.print_exc()
                if SHOT_DIR:
                    try:
                        a[0].screenshot(path=str(SHOT_DIR / f"FAIL-{name.replace(' ', '_')[:40]}.png"))
                    except Exception:
                        pass
        return run
    return deco


SHOT_DIR: Path | None = None


def wait_port(port, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise RuntimeError(f"port {port} did not open")


def make_workspace(base: Path):
    from dulwich import porcelain

    if base.exists():
        # git.exe writes some files read-only (e.g. commit-graphs); clear that so Windows can delete them
        def _force(func, path, _exc):
            os.chmod(path, 0o666)
            func(path)
        shutil.rmtree(base, onexc=_force)
    plain = base / "plain-folder"
    (plain / "src").mkdir(parents=True)
    (plain / "hello.txt").write_bytes(b"hello world\n")
    (plain / "src" / "util.py").write_bytes(b"def add(a, b):\n    return a + b\n")
    (plain / ".env").write_bytes(b"SECRET_TOKEN=do-not-leak-1234567890\n")
    repo = base / "git-repo"
    repo.mkdir()
    r = porcelain.init(str(repo))
    (repo / "main.py").write_bytes(b"print('v1')\n")
    (repo / "README.md").write_bytes(b"# Demo repo\n")
    porcelain.add(r, paths=[str(repo / "main.py"), str(repo / "README.md")])
    porcelain.commit(r, message=b"initial", author=b"E2E <e2e@example.com>", committer=b"E2E <e2e@example.com>")
    r.close()
    return plain, repo


class Servers:
    def __init__(self, env, logdir: Path):
        self.env, self.logdir = env, logdir
        self.engine = self.ide = None

    def _spawn(self, args, name):
        log = open(self.logdir / f"{name}.log", "ab")
        return subprocess.Popen(args, cwd=REPO, env=self.env, stdout=log, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    def start_engine(self):
        self.engine = self._spawn([PY, "-m", "agent_engine", "--port", str(ENGINE_PORT)], "engine")
        wait_port(ENGINE_PORT)

    def stop_engine(self):
        if self.engine:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.engine.pid)], capture_output=True)
            self.engine.wait(10)
            self.engine = None

    def start_ide(self):
        self.ide = self._spawn([PY, "manage.py", "runserver", f"127.0.0.1:{IDE_PORT}", "--noreload"], "ide")
        wait_port(IDE_PORT)

    def stop_all(self):
        self.stop_engine()
        if self.ide:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.ide.pid)], capture_output=True)


def dialog_ok(page, value=None):
    modal = page.locator("#dialog-modal")
    expect(modal).to_be_visible()
    if value is not None:
        page.fill("#dialog-input", value)
    page.click("#dialog-ok")
    expect(modal).to_be_hidden()


def toast_contains(page, text, timeout=15000):
    expect(page.locator("#toasts .toast-body", has_text=text).first).to_be_visible(timeout=timeout)


def editor_type(page, text):
    page.click("#editor .view-lines")
    page.keyboard.press("Control+End")
    page.keyboard.type(text)


def select_project(page, name):
    page.click("#tabbtn-explorer")
    page.select_option("#project-select", label=name)
    expect(page.locator("#file-tree .tree-node").first).to_be_visible(timeout=15000)


def main():
    global SHOT_DIR
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--real-agents", action="store_true", help="also run a real Claude Code task")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "code-studio-e2e"))
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    SHOT_DIR = out / "screenshots"
    SHOT_DIR.mkdir(parents=True)
    data_dir = out / "data"
    work = REPO / ".e2e" / "workroot"  # roots may not live under AppData, so keep this beside the repo
    plain, repo = make_workspace(work)

    env = dict(os.environ)
    env.update({"CODE_STUDIO_DATA_DIR": str(data_dir), "CODE_STUDIO_ENGINE_TOKEN": secrets.token_urlsafe(32),
                "CODE_STUDIO_ENGINE_URL": f"http://127.0.0.1:{ENGINE_PORT}", "CODE_STUDIO_ENGINE_TEST_ADAPTER": "1",
                "CODE_STUDIO_SYSTEM_TERMINAL": "1", "PYTHONUNBUFFERED": "1"})
    subprocess.run([PY, "manage.py", "migrate", "--noinput"], cwd=REPO, env=env, check=True, capture_output=True)
    subprocess.run([PY, "manage.py", "bootstrap", "--username", USER, "--password", PASSWORD, "--test-adapter"],
                   cwd=REPO, env=env, check=True, capture_output=True)

    servers = Servers(env, out)
    servers.start_engine()
    servers.start_ide()
    console_errors: list[str] = []
    print(f"IDE {IDE}  engine :{ENGINE_PORT}  data {data_dir}  artifacts {out}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            ctx = browser.new_context(viewport={"width": 1600, "height": 950})
            page = ctx.new_page()
            page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
            run_flows(page, servers, plain, repo, work, args.real_agents)
            # mobile layout smoke
            mobile = browser.new_context(viewport={"width": 390, "height": 844})
            mp = mobile.new_page()
            mobile_check(mp)
            browser.close()
    finally:
        servers.stop_all()

    ok = all(r[1] for r in results)
    unexpected = [e for e in console_errors if "409" not in e and "428" not in e and "503" not in e
                  and "Failed to load resource" not in e]
    report = {"passed": sum(r[1] for r in results), "failed": sum(not r[1] for r in results),
              "steps": [{"name": n, "ok": o, "detail": d} for n, o, d in results],
              "console_errors": console_errors, "unexpected_console_errors": unexpected}
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n{report['passed']} passed, {report['failed']} failed; unexpected console errors: {len(unexpected)}")
    for e in unexpected:
        print("   console:", e)
    print(f"report: {out / 'report.json'}")
    sys.exit(0 if ok and not unexpected else 1)


def run_flows(page, servers, plain, repo, work, real_agents):
    @step("login")
    def login(page):
        page.goto(IDE + "/")
        expect(page).to_have_url(f"{IDE}/login/?next=/")
        page.fill("#id_username", USER)
        page.fill("#id_password", "wrong")
        page.click("button[type=submit]")
        expect(page.locator(".alert-danger")).to_be_visible()
        page.fill("#id_username", USER)
        page.fill("#id_password", PASSWORD)
        page.click("button[type=submit]")
        expect(page.locator("#project-select")).to_be_visible()
        expect(page.locator("body[data-editor-ready='1']")).to_be_attached(timeout=20000)
        expect(page.locator("#engine-badge")).to_have_text("engine v1 online", timeout=15000)

    @step("configure allowlisted root")
    def add_root(page):
        page.click("#tabbtn-projects")
        page.fill("#root-alias", "work")
        page.fill("#root-path", "C:\\Windows")
        page.click("#root-form button[type=submit]")
        toast_contains(page, "System directories cannot be allowlisted")
        page.fill("#root-path", str(work))
        page.click("#root-form button[type=submit]")
        toast_contains(page, "Root added")
        expect(page.locator("#root-list")).to_contain_text(str(work))

    @step("register non-Git folder (unmanaged)")
    def reg_plain(page):
        page.click("#reg-browse")
        page.locator("#reg-browser .item[data-dir='plain-folder']").click()
        expect(page.locator("#reg-path")).to_have_value("plain-folder")
        page.click("#register-form button[type=submit]")
        toast_contains(page, "unmanaged folder")
        expect(page.locator("#git-badge")).to_have_text("unmanaged folder")
        assert not (plain / ".git").exists(), "git must never be initialised"

    @step("register Git repository")
    def reg_git(page):
        page.click("#tabbtn-projects")
        page.fill("#reg-path", "git-repo")
        page.fill("#reg-name", "Demo Repo")
        page.click("#register-form button[type=submit]")
        toast_contains(page, "Git repository")
        expect(page.locator("#git-badge")).to_contain_text("master", timeout=10000)

    @step("open, edit and save a file")
    def edit_save(page):
        select_project(page, "plain-folder")
        page.click("#tabbtn-explorer")
        page.locator(".tree-node[data-path='hello.txt']").click()
        expect(page.locator(".editor-tab.active")).to_contain_text("hello.txt")
        editor_type(page, "edited in browser")
        expect(page.locator(".editor-tab.active .dirty")).to_be_visible()
        page.keyboard.press("Control+s")
        toast_contains(page, "Saved hello.txt")
        expect(page.locator(".editor-tab.active .dirty")).to_have_count(0)
        assert (plain / "hello.txt").read_bytes() == b"hello world\nedited in browser", (plain / "hello.txt").read_bytes()
        page.screenshot(path=str(SHOT_DIR / "01-editor.png"))

    @step("create, rename and delete files")
    def file_ops(page):
        page.locator(".tree-node[data-path='src']").click()
        page.click("#btn-new-file")
        dialog_ok(page, "notes.md")
        toast_contains(page, "Created src/notes.md")
        assert (plain / "src" / "notes.md").exists()
        expect(page.locator(".editor-tab.active")).to_contain_text("notes.md")
        page.locator(".tree-node[data-path='src/notes.md']").click()
        page.click("#btn-rename")
        dialog_ok(page, "src/renamed.md")
        toast_contains(page, "Renamed to src/renamed.md")
        assert (plain / "src" / "renamed.md").exists() and not (plain / "src" / "notes.md").exists()
        page.locator(".tree-node[data-path='src/renamed.md']").click()
        page.click("#btn-delete")
        expect(page.locator("#dialog-body")).to_contain_text("recovery snapshot")
        page.click("#dialog-cancel")
        assert (plain / "src" / "renamed.md").exists(), "cancel must not delete"
        page.click("#btn-delete")
        dialog_ok(page)
        toast_contains(page, "Deleted src/renamed.md")
        assert not (plain / "src" / "renamed.md").exists()

    @step("external modification conflict -> resolve")
    def conflict(page):
        page.locator(".tree-node[data-path='src/util.py']").click()
        expect(page.locator(".editor-tab.active")).to_contain_text("util.py")
        editor_type(page, "\n# my browser edit")
        time.sleep(0.2)
        (plain / "src" / "util.py").write_bytes(b"def add(a, b):\n    return a + b  # external change\n")
        page.keyboard.press("Control+s")
        expect(page.locator("#conflict-modal")).to_be_visible()
        expect(page.locator("#conflict-path")).to_have_text("src/util.py")
        page.wait_for_timeout(800)
        page.screenshot(path=str(SHOT_DIR / "02-conflict.png"))
        expect(page.locator("#problem-count")).to_have_text("1")
        page.click("#conflict-overwrite")
        toast_contains(page, "Saved src/util.py")
        assert b"# my browser edit" in (plain / "src" / "util.py").read_bytes()
        page.click("#tabbtn-recovery")
        page.click("#btn-refresh-snapshots")
        expect(page.locator("#snapshot-list .item", has_text="force-save").first).to_be_visible()
        expect(page.locator("#snapshot-list .item", has_text="delete").first).to_be_visible()

    @step("restore recovery snapshot")
    def restore(page):
        page.locator("#snapshot-list .item", has_text="force-save").first.locator("button").click()
        dialog_ok(page)
        toast_contains(page, "Snapshot restored")
        assert b"# external change" in (plain / "src" / "util.py").read_bytes()

    @step("project search")
    def search(page):
        page.click("#tabbtn-search")
        page.fill("#search-input", "return a")
        page.click("#search-form button[type=submit]")
        expect(page.locator("#search-results .search-hit").first).to_contain_text("return a + b")
        page.locator("#search-results .search-hit").first.click()
        expect(page.locator(".editor-tab.active")).to_contain_text("util.py")

    @step("scoped terminal")
    def terminal(page):
        page.click("#tabbtn-terminal")
        expect(page.locator("#term-input")).to_be_enabled(timeout=15000)
        page.fill("#term-input", "echo e2e-terminal-ok")
        page.press("#term-input", "Enter")
        expect(page.locator("#term-output")).to_contain_text("e2e-terminal-ok", timeout=20000)
        expect(page.locator("#term-input")).to_be_enabled(timeout=20000)
        page.fill("#term-input", "cd src")
        page.press("#term-input", "Enter")
        expect(page.locator("#term-prompt")).to_have_text("~/src>", timeout=10000)
        page.fill("#term-input", "cd ..\\..")
        page.press("#term-input", "Enter")
        expect(page.locator("#term-output .t-denied").first).to_contain_text("outside the project root")
        page.fill("#term-input", "type C:\\Windows\\win.ini")
        page.press("#term-input", "Enter")
        expect(page.locator("#term-output .t-denied").nth(1)).to_be_visible(timeout=10000)
        expect(page.locator("#term-input")).to_be_enabled(timeout=10000)
        page.fill("#term-input", "Get-ChildItem -Name")
        page.press("#term-input", "Enter")
        expect(page.locator("#term-output")).to_contain_text("util.py", timeout=20000)
        page.screenshot(path=str(SHOT_DIR / "03-terminal.png"))

    @step("system terminal: real shell, persistent state, real paths")
    def system_terminal(page):
        page.click("#tabbtn-systerm")
        expect(page.locator("#systerm-status")).to_contain_text(str(plain), timeout=20000)
        rows = page.locator("#systerm-host .xterm-rows")
        expect(rows).to_contain_text("PS ", timeout=30000)  # the profile-loaded prompt with the real path
        page.locator("#systerm-host").click()
        for line in ("$env:E2E_SYS = 'kept'", "cd src", "Write-Output \"sys-$($env:E2E_SYS)-$(Split-Path -Leaf $PWD)\""):
            page.keyboard.type(line)
            page.keyboard.press("Enter")
        expect(rows).to_contain_text("sys-kept-src", timeout=20000)
        page.screenshot(path=str(SHOT_DIR / "03b-system-terminal.png"))
        page.keyboard.type("exit")
        page.keyboard.press("Enter")
        expect(rows).to_contain_text("press any key to restart", timeout=20000)
        page.keyboard.press("Enter")  # restarts in the project root
        expect(page.locator("#systerm-status")).to_contain_text(str(plain), timeout=20000)

    @step("git status, diff, stage, commit")
    def git(page):
        select_project(page, "Demo Repo")
        (repo / "main.py").write_bytes(b"print('v2')\n")
        (repo / "new_file.txt").write_bytes(b"brand new\n")
        page.click("#tabbtn-git")
        page.locator("#git-panel button[title='Refresh']").click()
        changes = page.locator("#git-panel [data-section='changes'] .git-item[data-path='main.py']")
        expect(changes).to_be_visible(timeout=10000)
        expect(page.locator("#git-panel [data-section='untracked'] .git-item[data-path='new_file.txt']")).to_be_visible()
        expect(page.locator("#git-badge")).to_contain_text("*")
        changes.locator(".grow").click()
        expect(page.locator("#diff-editor")).to_be_visible()
        expect(page.locator("#editor-status")).to_contain_text("Diff: main.py")
        page.wait_for_timeout(600)
        page.screenshot(path=str(SHOT_DIR / "04-git-diff.png"))
        page.click("#btn-diff-close")
        changes.locator("button").click()
        expect(page.locator("#git-panel [data-section='staged'] .git-item[data-path='main.py']")).to_be_visible()
        page.fill("#commit-message", "E2E commit from browser")
        page.click("#btn-commit")
        toast_contains(page, "Committed")
        expect(page.locator("#git-panel [data-section='staged'] .git-item")).to_have_count(0)
        from dulwich import porcelain

        with porcelain.open_repo_closing(str(repo)) as r:
            assert r[r.head()].message == b"E2E commit from browser"

    @step("git settings: login, commit author, line endings (modal)")
    def git_settings(page):
        modal = page.locator("#git-settings-modal")
        page.click("#tabbtn-git")
        page.click("#btn-git-settings")
        expect(modal).to_be_visible()
        expect(page.locator("#gs-remotes")).to_contain_text("no remote")
        expect(page.locator("#gs-line-endings")).to_be_enabled()
        # save a login; the token is write-only
        page.fill("#gs-host", "github.com")
        page.fill("#gs-username", "e2e-user")
        page.fill("#gs-token", "github_pat_E2ESECRET0000000001234")
        page.click("#gs-cred-save")
        toast_contains(page, "Saved login for github.com")
        row = modal.locator("#gs-cred-table tbody tr[data-host='github.com']")
        expect(row).to_contain_text("e2e-user")
        expect(row).to_contain_text("••••1234")
        assert "E2ESECRET" not in page.content()
        # edit username without retyping the token
        row.locator("button", has_text="Edit").click()
        expect(page.locator("#gs-host")).to_be_disabled()
        page.fill("#gs-username", "e2e-user2")
        page.click("#gs-cred-save")
        expect(row).to_contain_text("e2e-user2")
        # commit author
        page.fill("#gs-name", "E2E Person")
        page.fill("#gs-email", "e2e@example.com")
        modal.locator("#gs-id-form button[type=submit]").click()
        toast_contains(page, "Commit author saved")
        page.screenshot(path=str(SHOT_DIR / "04b-git-settings.png"))
        row.locator("button", has_text="Remove").click()
        expect(modal.locator("#gs-cred-table tbody")).to_contain_text("No logins saved yet.")
        modal.locator(".btn-close").click()
        expect(modal).to_be_hidden()

    def open_settings(page, section="agents"):
        page.click("#user-menu")
        page.click("#settings-item")
        modal = page.locator("#settings-modal")
        expect(modal).to_be_visible()
        if section != "agents":
            page.click(f"#settings-tab-{section}")
        return modal

    def close_settings(page):
        modal = page.locator("#settings-modal")
        modal.locator(".btn-close").click()
        expect(modal).to_be_hidden()

    def pick_for_task(page, names):
        """New task > Change for this task: run exactly these profiles, in this order."""
        if not page.locator("#task-change").evaluate("d => d.open"):
            page.click("#task-change summary")
        for cb in page.locator("#task-assignments .assign-check:checked").all():
            cb.uncheck()
        for n in names:
            page.locator("#task-assignments .item", has_text=n).locator(".assign-check").check()

    @step("settings: agent team discovery (real CLI probes)")
    def agents(page):
        # agents, profiles and the lineup live in Settings now, not in the side panel
        assert page.locator("#right-panel #adapter-list").count() == 0
        open_settings(page)
        page.click("#btn-refresh-agents")
        al = page.locator("#adapter-list")
        expect(al.locator("[data-adapter='fixture']")).to_contain_text("ready", timeout=60000)
        expect(al.locator("[data-adapter='antigravity']")).to_contain_text("unavailable")
        page.screenshot(path=str(SHOT_DIR / "05-agents.png"))
        close_settings(page)

    @step("settings: agent profiles add, edit, disable, delete; default lineup")
    def profiles_modal(page):
        modal = open_settings(page, "profiles")
        expect(modal.locator("#pm-table tbody tr", has_text="Fixture implementer")).to_be_visible()
        # add: picking the reviewer role defaults to read-only and suggests a name
        page.click("#pm-new")
        page.select_option("#pm-adapter", "fixture")
        modal.locator("#pm-role-chips button[data-role='reviewer']").click()
        expect(page.locator("#pm-access-ro")).to_be_checked()
        expect(page.locator("#pm-name")).to_have_value(re.compile(r"reviewer$"))
        page.fill("#pm-name", "Fixture implementer")          # duplicate -> inline error, stays open
        page.click("#pm-save")
        expect(page.locator("#pm-error")).to_contain_text("already exists")
        page.fill("#pm-name", "E2E reviewer")
        page.screenshot(path=str(SHOT_DIR / "05b-profile-form.png"))
        page.click("#pm-save")
        toast_contains(page, 'Profile "E2E reviewer" created')
        row = modal.locator("#pm-table tbody tr", has_text="E2E reviewer")
        expect(row).to_contain_text("Read-only")
        expect(page.locator("#task-assignments .item", has_text="E2E reviewer")).to_have_count(1)
        # edit: rename, give edit access, longer time limit
        row.locator("button", has_text="Edit").click()
        expect(page.locator("#pm-name")).to_have_value("E2E reviewer")
        page.fill("#pm-name", "E2E tester")
        modal.locator("#pm-role-chips button[data-role='tester']").click()
        page.check("#pm-access-edit")
        if not page.locator("#pm-advanced").evaluate("d => d.open"):
            page.click("#pm-advanced summary")
        page.fill("#pm-timeout", "30")
        page.click("#pm-save")
        toast_contains(page, 'Profile "E2E tester" saved')
        row = modal.locator("#pm-table tbody tr", has_text="E2E tester")
        expect(row).to_contain_text("Can edit")
        expect(row).to_contain_text("tester")
        page.screenshot(path=str(SHOT_DIR / "05c-profiles.png"))
        # disable -> hidden from New task
        row.locator("input[role='switch']").uncheck()
        expect(page.locator("#task-assignments .item", has_text="E2E tester")).to_have_count(0, timeout=10000)
        # delete needs a second click
        row.locator("button", has_text="Edit").click()
        page.click("#pm-delete")
        expect(page.locator("#pm-delete")).to_have_text("Click again to delete")
        page.click("#pm-delete")
        toast_contains(page, 'Profile "E2E tester" deleted')
        expect(modal.locator("#pm-table tbody tr", has_text="E2E tester")).to_have_count(0)
        # default lineup: saved for the account, shown in New task, kept across reloads
        page.click("#settings-tab-lineup")
        expect(page.locator("#lineup-save")).to_be_disabled()
        page.locator("#lineup-editor .item", has_text="Fixture implementer").locator(".assign-check").check()
        expect(page.locator("#lineup-pipeline")).to_contain_text("Unsaved changes")
        page.click("#lineup-save")
        toast_contains(page, "Default lineup saved")
        page.wait_for_timeout(400)                    # let the section finish painting
        page.screenshot(path=str(SHOT_DIR / "05d-default-lineup.png"))
        close_settings(page)
        expect(page.locator("#task-pipeline")).to_contain_text("Runs: Fixture implementer (implementer)")
        page.reload()
        expect(page.locator("#task-pipeline")).to_contain_text("Runs: Fixture implementer (implementer)", timeout=15000)

    def run_task(page, profile_name, objective, expect_files, timeout_ms):
        if profile_name != "Fixture implementer":  # the default lineup runs Fixture implementer
            pick_for_task(page, [profile_name])
        page.fill("#task-objective", objective)
        page.click("#task-submit")
        toast_contains(page, "Task submitted")
        expect(page.locator("#task-log")).to_contain_text("task.submitted", timeout=15000)
        expect(page.locator("#task-log")).to_contain_text("awaiting_review", timeout=timeout_ms)
        for f in expect_files:
            expect(page.locator(f"#review-files .item[data-path='{f}']")).to_be_visible(timeout=15000)

    @step("agent task: submit, stream, review diff, approve (fixture adapter)")
    def agent_task(page):
        select_project(page, "plain-folder")
        run_task(page, "Fixture implementer",
                 "SAY implementing\nWRITE agent_output.txt <<< written by agent\\n\nWRITE hello.txt <<< replaced by agent\\n",
                 ["agent_output.txt", "hello.txt"], 60000)
        assert not (plain / "agent_output.txt").exists(), "changes must not land before approval"
        expect(page.locator("#task-log")).to_contain_text("agent.tool_use")
        assert "do-not-leak" not in page.locator("#task-log").inner_text()
        page.locator("#review-files .item[data-path='hello.txt'] .grow").click()
        expect(page.locator("#diff-editor")).to_be_visible()
        page.wait_for_timeout(600)
        page.screenshot(path=str(SHOT_DIR / "06-agent-review.png"))
        page.click("#btn-diff-close")
        # clicking a step in the Selected task section shows that step's full output
        page.locator("#review-meta .rm-step").first.click()
        expect(page.locator("#step-modal")).to_be_visible(timeout=5000)
        expect(page.locator("#step-modal-title")).to_have_text("Step 1: implementer")
        expect(page.locator("#step-modal-file")).to_have_text("step-01-implementer.md")
        expect(page.locator("#step-modal-body")).to_contain_text("fixture run complete")
        page.wait_for_timeout(400)
        page.screenshot(path=str(SHOT_DIR / "06a-step-output.png"))
        page.locator("#step-modal .btn-close").click()
        expect(page.locator("#step-modal")).to_be_hidden(timeout=5000)
        page.click("#btn-approve")
        dialog_ok(page)
        toast_contains(page, "Applied 2 file(s)")
        assert (plain / "agent_output.txt").read_bytes() == b"written by agent\n"
        assert (plain / "hello.txt").read_bytes() == b"replaced by agent\n"
        expect(page.locator("#task-list .item").first).to_contain_text("applied", timeout=10000)

    @step("multi-agent task runs in the order ticked (not alphabetical)")
    def agent_order(page):
        page.evaluate("""async () => {
            const csrf = document.querySelector('meta[name=csrf-token]').content;
            const r = await fetch('/api/profiles', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf},
              body: JSON.stringify({name: 'Fixture reviewer', adapter: 'fixture', role: 'reviewer', execution_policy: {read_only: true, timeout_seconds: 120}})});
            if (!r.ok) throw new Error(await r.text());
        }""")
        open_settings(page)                            # opening Settings reloads agents and profiles
        close_settings(page)
        rev = page.locator("#task-assignments .item", has_text="Fixture reviewer")
        imp = page.locator("#task-assignments .item", has_text="Fixture implementer")
        expect(rev).to_have_count(1, timeout=10000)
        pick_for_task(page, ["Fixture reviewer", "Fixture implementer"])   # reviewer ticked first -> step 1
        expect(rev.locator(".assign-step")).to_have_text("1")
        expect(imp.locator(".assign-step")).to_have_text("2")
        expect(page.locator("#task-pipeline")).to_contain_text("Fixture reviewer (reviewer) → Fixture implementer (implementer)")
        expect(page.locator("#task-pipeline")).to_contain_text("Changed for this task")
        page.fill("#task-objective", "SAY ordered run\nWRITE order.txt <<< ordered")
        page.click("#task-submit")
        toast_contains(page, "Task submitted")
        log = page.locator("#task-log")
        expect(log).to_contain_text("awaiting_review", timeout=60000)
        text = log.inner_text()
        assert text.index("reviewer via fixture") < text.index("implementer via fixture"), text[:2000]
        page.screenshot(path=str(SHOT_DIR / "06b-agent-order.png"))
        # a change "for this task" ends with that task: back to the default lineup
        expect(page.locator("#task-pipeline")).to_have_text("Runs: Fixture implementer (implementer)")
        page.click("#btn-reject")
        page.click("#dialog-ok")
        toast_contains(page, "Changes rejected")

    @step("agent apply blocked by concurrent edit (conflict)")
    def agent_conflict(page):
        run_task(page, "Fixture implementer", "WRITE hello.txt <<< second agent pass\\n", ["hello.txt"], 60000)
        (plain / "hello.txt").write_bytes(b"user changed it meanwhile\n")
        page.locator("#task-list .item").first.click()
        expect(page.locator("#review-files .item[data-path='hello.txt']")).to_contain_text("changed in the project")
        page.click("#btn-reject")
        dialog_ok(page)
        toast_contains(page, "Changes rejected")
        assert (plain / "hello.txt").read_bytes() == b"user changed it meanwhile\n"

    @step("agent task cancellation")
    def agent_cancel(page):
        page.fill("#task-objective", "SLEEP 60")      # default lineup
        page.click("#task-submit")
        expect(page.locator("#task-log")).to_contain_text("agent.started", timeout=20000)
        page.click("#btn-cancel-task")
        expect(page.locator("#task-log")).to_contain_text("state → cancelled", timeout=20000)

    @step("follow-up continues the cancelled task's conversation in the same working copy")
    def agent_followup(page):
        items = page.locator("#task-list .item")
        before = items.count()
        page.fill("#task-objective", "SLEEP 60")      # its own task: the cancelled one above is used later
        page.click("#task-submit")
        toast_contains(page, "Task submitted")
        expect(items).to_have_count(before + 1, timeout=15000)
        expect(items.first).to_have_attribute("data-state", "running", timeout=20000)
        expect(page.locator("#task-log")).to_contain_text("agent.system", timeout=20000)  # session id reported
        page.click("#btn-cancel-task")
        expect(items.first).to_have_attribute("data-state", "cancelled", timeout=20000)
        box = page.locator("#followup-box")
        expect(box.locator("#followup-form")).to_be_visible(timeout=15000)
        expect(page.locator("#followup-step option")).to_have_count(1)
        page.fill("#followup-message", "SAY picking up\nWRITE followup.txt <<< continued\nREPLY done after follow-up")
        page.press("#followup-message", "Control+Enter")
        toast_contains(page, "Follow-up sent")
        log = page.locator("#task-log")
        expect(log).to_contain_text("resumed session", timeout=30000)
        expect(log).to_contain_text("state → awaiting_review", timeout=60000)
        expect(page.locator("#review-files .item[data-path='followup.txt']")).to_be_visible(timeout=15000)
        summary = page.locator("#task-summary")
        page.click("#tabbtn-tasksummary")
        expect(summary.locator(".ts-step[data-step='1'] .badge.text-bg-info")).to_have_text("follow-up to step 1")
        expect(summary.locator(".ts-followup-msg")).to_contain_text("WRITE followup.txt")
        expect(summary.locator(".ts-step[data-step='1'] .ts-reply")).to_contain_text("done after follow-up")
        page.screenshot(path=str(SHOT_DIR / "06e-follow-up.png"))
        assert not (plain / "followup.txt").exists(), "follow-up edits wait for review too"
        page.click("#btn-reject")
        dialog_ok(page)
        toast_contains(page, "Changes rejected")
        expect(page.locator("#followup-unavailable")).to_contain_text("rejected", timeout=15000)

    @step("images: attach to a task and a follow-up, shown in the summary, given to the agents")
    def agent_images(page):
        import struct
        import zlib

        def png(rgb):  # a real 8x8 one-colour PNG the browser can render
            raw = b"".join(b"\x00" + bytes(rgb) * 8 for _ in range(8))
            chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

        imgs = work / "e2e-images"
        imgs.mkdir(exist_ok=True)
        (imgs / "red.png").write_bytes(png((255, 0, 0)))
        (imgs / "blue.png").write_bytes(png((0, 0, 255)))
        (imgs / "notes.txt").write_bytes(b"not an image")
        picker = page.locator("#task-images input[type=file]")
        picker.set_input_files(str(imgs / "notes.txt"))
        toast_contains(page, "is not a PNG, JPEG, GIF or WebP image")
        picker.set_input_files([str(imgs / "red.png"), str(imgs / "blue.png")])
        thumbs = page.locator("#task-images .attach-thumb")
        expect(thumbs).to_have_count(2)
        thumbs.nth(1).locator(".attach-remove").click()
        expect(thumbs).to_have_count(1)
        expect(page.locator("#task-images")).to_contain_text("1 image attached")
        page.fill("#task-objective", "WRITE pic.txt <<< from a screenshot")
        page.click("#task-submit")
        toast_contains(page, "Task submitted")
        expect(thumbs).to_have_count(0)  # the form starts empty again
        expect(page.locator("#task-log")).to_contain_text("state → awaiting_review", timeout=60000)
        page.click("#tabbtn-tasksummary")
        summary = page.locator("#task-summary")
        shown = summary.locator(".ts-images img")
        expect(shown).to_have_count(1)
        expect(shown.first).to_have_attribute("alt", "red.png")
        page.wait_for_function("() => { const i = document.querySelector('#task-summary .ts-images img');"
                               " return i && i.complete && i.naturalWidth === 8; }", timeout=15000)
        ws = Path(servers.env["CODE_STUDIO_DATA_DIR"]) / "engine" / "workspaces"
        found = list(ws.glob("*/work/.codestudio-task/attachments/task-01.png"))
        assert found and found[0].read_bytes() == (imgs / "red.png").read_bytes(), "agent got the image"

        page.locator("#followup-images input[type=file]").set_input_files(str(imgs / "blue.png"))
        expect(page.locator("#followup-images .attach-thumb")).to_have_count(1)
        page.fill("#followup-message", "SAY saw it\nREPLY looked at the blue one")
        page.press("#followup-message", "Control+Enter")
        toast_contains(page, "Follow-up sent")
        expect(page.locator("#task-log")).to_contain_text("state → awaiting_review", timeout=60000)
        page.click("#tabbtn-tasksummary")
        expect(summary.locator(".ts-step[data-step='1'] .ts-images img")).to_have_attribute("alt", "blue.png")
        page.screenshot(path=str(SHOT_DIR / "06e2-images.png"))
        page.click("#btn-reject")
        dialog_ok(page)
        toast_contains(page, "Changes rejected")

    @step("project checks: settings, Verify step runs the commands, result in the summary")
    def project_checks(page):
        (plain / "check_settings.py").write_bytes(
            b"import os, sys\nprint('settings=' + os.environ.get('DJANGO_SETTINGS_MODULE', ''))\n"
            b"sys.exit(0 if os.environ.get('DJANGO_SETTINGS_MODULE') == 'plain.settings_test' else 5)\n")
        open_settings(page, "checks")
        expect(page.locator("#pc-form")).to_be_visible()
        expect(page.locator("#pc-project")).to_contain_text("plain-folder")
        page.fill("#pc-env", "DATABASE_URL=postgres://u:p@h/d")
        page.click("#pc-save")
        expect(page.locator("#pc-error")).to_contain_text("database settings are never given to agents")
        page.fill("#pc-commands", "python check_settings.py")
        page.fill("#pc-env", "DJANGO_SETTINGS_MODULE=plain.settings_test")
        page.fill("#pc-exclude", "*.csv")
        page.click("#pc-save")
        toast_contains(page, "Project checks saved")
        expect(page.locator("#pc-venv-status")).to_contain_text("No virtual environment found")
        page.screenshot(path=str(SHOT_DIR / "06f-project-checks.png"))
        # a Verify profile: the "Checks" agent hides model/access choices
        page.click("#settings-tab-profiles")
        page.click("#pm-new")
        page.select_option("#pm-adapter", "verify")
        expect(page.locator("#pm-role")).to_have_value("verify")
        expect(page.locator("#pm-access-ro")).to_be_hidden()
        page.fill("#pm-name", "Verify")
        page.click("#pm-save")
        expect(page.locator("#pm-table tbody")).to_contain_text("Verify", timeout=10000)
        close_settings(page)
        pick_for_task(page, ["Fixture implementer", "Verify"])
        page.fill("#task-objective", "WRITE checked.txt <<< ok")
        page.click("#task-submit")
        toast_contains(page, "Task submitted")
        summary = page.locator("#task-summary")
        expect(page.locator("#tabbtn-tasksummary")).to_have_class(re.compile(r"\bactive\b"), timeout=60000)
        expect(summary.locator(".ts-step[data-step='1'] [data-checks='passed']")).to_have_text("1 check passed", timeout=15000)
        expect(summary.locator(".ts-outcome")).to_contain_text("All checks passed in step 2")
        expect(summary.locator(".ts-step[data-step='1'] .ts-reply")).to_contain_text("python check_settings.py")
        expect(page.locator("#review-files .item[data-path='checked.txt']")).to_be_visible(timeout=15000)
        expect(page.locator("#review-files .item")).to_have_count(1)  # the checks' own files are not in the change set
        page.wait_for_timeout(1500)
        expect(summary.locator(".ts-step[data-step='1'] [data-checks='passed']")).to_be_visible()
        page.screenshot(path=str(SHOT_DIR / "06g-verify-step.png"))
        assert not (plain / "checked.txt").exists()
        page.click("#btn-reject")
        dialog_ok(page)
        toast_contains(page, "Changes rejected")

    @step("post-task summary: agent reply shown (research task, no file changes)")
    def task_summary(page):
        reply = ("# Research notes\\n\\nTwo options:\\n\\n- option **one** is faster\\n- docs: https://example.com/docs"
                 "\\n\\n| Option | Speed |\\n|---|---|\\n| one | fast |\\n\\n```\\n<img src=x onerror=alert(1)> code\\n```")
        page.fill("#task-objective", "REPLY " + reply)
        page.click("#task-submit")
        toast_contains(page, "Task submitted")
        summary = page.locator("#task-summary")
        # the bottom panel flips from the live log to the summary when the task finishes
        expect(page.locator("#tabbtn-tasksummary")).to_have_class(re.compile(r"\bactive\b"), timeout=60000)
        expect(summary.locator(".ts-outcome")).to_contain_text("No files were changed", timeout=15000)
        expect(summary.locator(".ts-reply h3")).to_have_text("Research notes")
        expect(summary.locator(".ts-reply strong")).to_have_text("one")
        expect(summary.locator(".ts-reply a[href='https://example.com/docs']")).to_have_attribute("rel", "noopener noreferrer")
        expect(summary.locator(".ts-reply table td").first).to_have_text("one")
        expect(summary.locator(".ts-reply pre code")).to_contain_text("<img src=x onerror=alert(1)> code")
        assert summary.locator(".ts-reply img").count() == 0, "agent output must never become HTML"
        expect(summary.locator(".ts-step[data-status='succeeded'] .badge")).to_have_text("done")
        # finish notification with a shortcut to the summary
        expect(page.locator(".toast", has_text='Task finished: "REPLY # Research').locator(".toast-action")).to_have_text("View summary", timeout=15000)
        page.screenshot(path=str(SHOT_DIR / "06c-task-summary.png"))
        page.click("#btn-summary-enlarge")
        expect(page.locator("#summary-modal")).to_be_visible()
        expect(page.locator("#summary-modal-body .ts-reply h3")).to_have_text("Research notes")
        page.screenshot(path=str(SHOT_DIR / "06d-task-summary-modal.png"))
        page.locator("#summary-modal .btn-close").click()
        expect(page.locator("#summary-modal")).to_be_hidden()
        # an earlier failed/cancelled task explains itself too
        page.locator("#task-list .item[data-state='cancelled']").first.click()
        expect(summary.locator(".ts-outcome")).to_contain_text("Cancelled", timeout=15000)

    if real_agents:
        @step("REAL Claude Code task end-to-end")
        def claude_task(page):
            run_task(page, "Claude implementer",
                     "Create a new file named claude_hello.txt in the project root whose entire content is the "
                     "single line: hello from claude. Do not modify any other file.",
                     ["claude_hello.txt"], 300000)
            expect(page.locator("#task-log")).to_contain_text("agent.result")
            page.screenshot(path=str(SHOT_DIR / "07-claude-review.png"))
            page.click("#btn-approve")
            dialog_ok(page)
            toast_contains(page, "Applied", timeout=20000)
            assert "hello from claude" in (plain / "claude_hello.txt").read_text(encoding="utf-8")

    @step("resizable panels: drag, keyboard, persist, reset")
    def resize_panels(page):
        width = lambda sel: page.locator(sel).bounding_box()["width"]    # noqa: E731
        height = lambda sel: page.locator(sel).bounding_box()["height"]  # noqa: E731

        def drag(handle, dx, dy):
            b = page.locator(handle).bounding_box()
            x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x + dx / 2, y + dy / 2, steps=4)
            page.mouse.move(x + dx, y + dy, steps=4)
            page.mouse.up()

        l0, r0, b0 = width("#left-panel"), width("#right-panel"), height("#bottom-panel")
        drag("#split-left", 120, 0)
        drag("#split-right", -100, 0)
        drag("#split-bottom", 0, -80)
        l1, r1, b1 = width("#left-panel"), width("#right-panel"), height("#bottom-panel")
        assert abs(l1 - (l0 + 120)) <= 3 and abs(r1 - (r0 + 100)) <= 3 and abs(b1 - (b0 + 80)) <= 3, (l0, l1, r0, r1, b0, b1)
        editor_w = page.locator("#editor").bounding_box()["width"]
        assert editor_w > 300, editor_w                       # editor follows its container
        page.screenshot(path=str(SHOT_DIR / "09-resized.png"))
        # sizes survive a reload
        page.reload()
        expect(page.locator("body[data-editor-ready='1']")).to_be_attached(timeout=20000)
        assert abs(width("#left-panel") - l1) <= 3 and abs(height("#bottom-panel") - b1) <= 3
        # keyboard: focus the handle, arrow keys move it in 16px steps
        page.locator("#split-left").focus()
        page.keyboard.press("ArrowLeft")
        page.keyboard.press("ArrowLeft")
        assert abs(width("#left-panel") - (l1 - 32)) <= 3
        expect(page.locator("#split-left")).to_have_attribute("aria-valuenow", str(round(l1 - 32)))
        # limits: can't squeeze the editor away
        drag("#split-right", -3000, 0)
        assert page.locator(".ide-center").bounding_box()["width"] >= 318
        # double-click resets to the defaults
        for h in ("#split-left", "#split-right", "#split-bottom"):
            page.locator(h).dblclick()
        assert abs(width("#left-panel") - 280) <= 2 and abs(width("#right-panel") - 360) <= 2 and abs(height("#bottom-panel") - 260) <= 2

    @step("audit log visible")
    def audit(page):
        page.click("#tabbtn-audit")
        expect(page.locator("#audit-list")).to_contain_text("agent.apply", timeout=10000)
        expect(page.locator("#audit-list")).to_contain_text("terminal.command")

    @step("engine stopped: IDE keeps working")
    def offline(page):
        servers.stop_engine()
        page.evaluate("fetch('/api/engine/status?fresh=1')")
        expect(page.locator("#engine-badge")).to_have_text("engine offline", timeout=25000)
        expect(page.locator("#engine-offline-banner")).to_be_visible()
        expect(page.locator("#task-submit")).to_be_disabled()
        page.click("#tabbtn-explorer")
        page.click("#btn-refresh-tree")
        page.locator(".tree-node[data-path='hello.txt']").click()
        expect(page.locator(".editor-tab.active")).to_contain_text("hello.txt")
        editor_type(page, "\noffline edit")
        page.keyboard.press("Control+s")
        toast_contains(page, "Saved hello.txt")
        assert b"offline edit" in (plain / "hello.txt").read_bytes()
        page.click("#tabbtn-terminal")
        expect(page.locator("#term-input")).to_be_enabled(timeout=15000)
        page.fill("#term-input", "echo still-alive")
        page.press("#term-input", "Enter")
        expect(page.locator("#term-output")).to_contain_text("still-alive", timeout=20000)
        page.locator("#task-list .item").first.click()  # history still readable from the IDE DB
        expect(page.locator("#task-log")).to_contain_text("task.submitted", timeout=10000)
        page.screenshot(path=str(SHOT_DIR / "08-engine-offline.png"))

    @step("engine restarted: reconnects")
    def online_again(page):
        servers.start_engine()
        expect(page.locator("#engine-badge")).to_have_text("engine v1 online", timeout=30000)
        expect(page.locator("#task-submit")).to_be_enabled()

    @step("sign out")
    def logout(page):
        page.click("#user-menu")
        page.click("#logout-btn")
        expect(page).to_have_url(f"{IDE}/login/")
        r = page.request.get(IDE + "/api/projects")
        assert r.status == 401

    for fn in (login, add_root, reg_plain, reg_git, edit_save, file_ops, conflict, restore, search, terminal, system_terminal, git,
               git_settings, agents, profiles_modal, agent_task, agent_order, agent_conflict, agent_cancel, agent_followup, agent_images, project_checks,
               task_summary):
        fn(page)
    if real_agents:
        claude_task(page)
    for fn in (resize_panels, audit, offline, online_again, logout):
        fn(page)


def mobile_check(page):
    @step("mobile layout (monitoring/approvals first)")
    def mobile(page):
        page.goto(IDE + "/login/")
        page.fill("#id_username", USER)
        page.fill("#id_password", PASSWORD)
        page.click("button[type=submit]")
        expect(page.locator("#right-panel")).to_be_visible()
        expect(page.locator("#left-panel")).to_be_hidden()
        width = page.evaluate("document.documentElement.scrollWidth")
        assert width <= 390, f"horizontal scroll: {width}"
        page.screenshot(path=str(SHOT_DIR / "09-mobile.png"), full_page=False)

    mobile(page)


if __name__ == "__main__":
    main()
