"""System terminal: local-only gating, environment hygiene and a live PTY session."""
import asyncio
import os
import re
import sys

import pytest
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator

from audit.models import AuditEvent
from terminal.routing import websocket_urlpatterns
from terminal.syspty import shell_environment
from terminal.system import is_loopback

WIN = os.name == "nt"
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)")


def _comm(project, user, client=("127.0.0.1", 50000), query="shell=" + ("powershell" if WIN else "sh")):
    comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), f"/ws/systerm/{project.id}/?{query}")
    comm.scope["user"] = user
    comm.scope["client"] = client
    return comm


@pytest.fixture
def enabled(settings):
    settings.CODE_STUDIO["SYSTEM_TERMINAL"] = True


@pytest.mark.parametrize("host,ok", [("127.0.0.1", True), ("::1", True), ("127.8.0.1", True),
                                     ("192.168.1.5", False), ("0.0.0.0", False), ("", False), (None, False),
                                     ("not-an-ip", False)])
def test_is_loopback(host, ok):
    assert is_loopback(host) is ok


def test_shell_environment_strips_code_studio_state():
    base = {"CODE_STUDIO_ENGINE_TOKEN": "secret", "code_studio_secret_key": "x", "DJANGO_SETTINGS_MODULE": "m",
            "OPENAI_API_KEY": "users-own", "PATH": os.pathsep.join([os.path.join(sys.prefix, "Scripts"), "keep"]),
            "VIRTUAL_ENV": sys.prefix, "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
    env = shell_environment(base)
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env  # `claude login` must not see a parent session
    assert not any(k.upper().startswith("CODE_STUDIO_") for k in env)
    assert "DJANGO_SETTINGS_MODULE" not in env
    assert env["OPENAI_API_KEY"] == "users-own"  # a real shell keeps the user's own variables
    if sys.prefix != sys.base_prefix:
        assert env["PATH"] == "keep" and "VIRTUAL_ENV" not in env


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_refused_when_disabled(settings, plain_project, admin_user):
    settings.CODE_STUDIO["SYSTEM_TERMINAL"] = False
    connected, code = await _comm(plain_project, admin_user).connect()
    assert not connected and code == 4403


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_refused_for_remote_client(enabled, plain_project, admin_user):
    connected, code = await _comm(plain_project, admin_user, client=("192.168.1.20", 50000)).connect()
    assert not connected and code == 4403
    connected, code = await _comm(plain_project, admin_user, client=None).connect()
    assert not connected and code == 4403


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_refused_when_signed_out_or_not_member(enabled, plain_project, plain_user):
    from django.contrib.auth.models import AnonymousUser

    connected, code = await _comm(plain_project, AnonymousUser()).connect()
    assert not connected and code == 4401
    connected, code = await _comm(plain_project, plain_user).connect()
    assert not connected and code == 4404


async def _read_until(comm, pattern, timeout=30):
    out = ""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        msg = await comm.receive_json_from(timeout=max(0.1, deadline - loop.time()))
        if msg["type"] == "output":
            out += msg["data"]
            if re.search(pattern, ANSI.sub("", out)):
                return out
        elif msg["type"] == "exit":
            return out + f"\n<exit {msg['code']}>"


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_live_session_is_persistent_and_unscoped(enabled, plain_project, admin_user, workroot):
    comm = _comm(plain_project, admin_user)
    connected, _ = await comm.connect()
    assert connected
    ready = await comm.receive_json_from(timeout=20)
    assert ready["type"] == "ready", ready
    assert os.path.normcase(ready["cwd"]) == os.path.normcase(str((workroot / "plain").resolve()))
    await comm.send_json_to({"type": "resize", "cols": 100, "rows": 20})

    if WIN:  # state survives between commands, real absolute paths, engine token stripped
        lines = ["$env:CS_PERSIST = 'kept'", "cd src",
                 "Write-Output \"V=$env:CS_PERSIST|P=$PWD|T=[$env:CODE_STUDIO_ENGINE_TOKEN]|DONE\""]
    else:
        lines = ["CS_PERSIST=kept", "cd src", "echo \"V=$CS_PERSIST|P=$PWD|T=[$CODE_STUDIO_ENGINE_TOKEN]|DONE\""]
    for line in lines:
        await comm.send_json_to({"type": "input", "data": line + "\r"})
    out = ANSI.sub("", await _read_until(comm, r"V=kept\|P=.+\|T=\[\]\|DONE"))
    m = re.search(r"V=kept\|P=(.+?)\|T=\[\]\|DONE", out)
    assert m, out
    assert os.path.normcase(m.group(1).strip()) == os.path.normcase(str((workroot / "plain" / "src").resolve()))

    await comm.send_json_to({"type": "input", "data": "exit 3\r"})
    msg = None
    for _ in range(200):
        msg = await comm.receive_json_from(timeout=20)
        if msg["type"] == "exit":
            break
    assert msg == {"type": "exit", "code": 3}
    await comm.wait(timeout=10)

    actions = await asyncio.to_thread(lambda: list(AuditEvent.objects.values_list("action", flat=True)))
    assert "terminal.system_start" in actions and "terminal.system_end" in actions


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_disconnect_kills_shell_tree(enabled, plain_project, admin_user):
    import psutil

    me = psutil.Process()
    before = {p.pid for p in me.children(recursive=True)}
    comm = _comm(plain_project, admin_user)
    await comm.connect()
    await comm.receive_json_from(timeout=20)
    sleeper = "Start-Sleep -Seconds 120" if WIN else "sleep 120"
    await comm.send_json_to({"type": "input", "data": sleeper + "\r"})
    await asyncio.sleep(3)
    kids = [p for p in me.children(recursive=True) if p.pid not in before]
    assert kids, "shell did not start"
    await comm.disconnect()
    gone, alive = psutil.wait_procs(kids, timeout=10)
    assert not alive, [p.name() for p in alive]


@pytest.mark.django_db
def test_ide_page_shows_system_terminal_only_when_enabled(client_admin, settings):
    settings.CODE_STUDIO["SYSTEM_TERMINAL"] = False
    html = client_admin.get("/").content.decode()
    assert 'data-system-terminal="0"' in html and "tab-systerm" not in html and "xterm.js" not in html
    settings.CODE_STUDIO["SYSTEM_TERMINAL"] = True
    html = client_admin.get("/").content.decode()  # the test client is 127.0.0.1
    assert 'data-system-terminal="1"' in html and 'id="tab-systerm"' in html
    assert html.index("vendor/xterm/xterm.js") < html.index("vendor/monaco/vs/loader.js")
    html = client_admin.get("/", REMOTE_ADDR="10.0.0.7").content.decode()
    assert 'data-system-terminal="0"' in html
