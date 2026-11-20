"""Scoped terminal: policy unit tests + live WebSocket sessions."""
import asyncio
import os

import pytest
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator

from audit.models import AuditEvent
from terminal.policy import check_command, resolve_cd
from terminal.routing import websocket_urlpatterns


@pytest.fixture
def troot(tmp_path):
    r = tmp_path / "proj"
    (r / "sub").mkdir(parents=True)
    return r.resolve()


WIN = os.name == "nt"
win_only = pytest.mark.skipif(not WIN, reason="Windows shell syntax")
posix_only = pytest.mark.skipif(WIN, reason="POSIX shell syntax")


@win_only
@pytest.mark.parametrize("cmd", [
    "dir", "python -V", "git status", "echo hello", "type sub\\file.txt", "dir /s /b", "python -c \"print(1/2)\"",
    "Get-ChildItem sub", "curl https://example.com/x/y",
])
def test_policy_allows(cmd, troot):
    assert check_command(cmd, troot, troot), check_command(cmd, troot, troot).reason


@win_only
@pytest.mark.parametrize("cmd", [
    "type ..\\..\\secret.txt", "Get-Content C:\\Windows\\win.ini", "dir \\\\server\\share", "cat ~/.ssh/id_rsa",
    "dir $env:USERPROFILE", "type %APPDATA%\\x", "runas /user:Administrator cmd", "Start-Process cmd -Verb RunAs",
    "mklink /J out C:\\", "New-Item -ItemType Junction -Path x -Target C:\\", "reg add HKLM\\Software\\x",
    "Set-ExecutionPolicy Unrestricted", "shutdown /s", "Set-Location C:\\", "Get-ChildItem env:", "set",
    "docker run -v C:\\:/host alpine", "type D:secret.txt",
])
def test_policy_blocks(cmd, troot):
    assert not check_command(cmd, troot, troot)


@posix_only
@pytest.mark.parametrize("cmd", [
    "ls -la", "python -V", "git status", "cat sub/file.txt", "grep -rn TODO .", "ls ./sub",
    "git commit -m \"mount the service at boot\"", "python -c \"print(1/2)\"", "curl https://example.com/x/y",
    "npm run build && echo done", "FOO=1 python x.py",
])
def test_policy_allows_posix(cmd, troot):
    assert check_command(cmd, troot, troot), check_command(cmd, troot, troot).reason


@posix_only
@pytest.mark.parametrize("cmd", [
    "cat /etc/passwd", "ls /data", "cat ../../secret.txt", "cat ~/.ssh/id_rsa", "ls $HOME", "cat ${HOME}/x",
    "sudo ls", "su root", "ls; su -", "ln -s / out", "echo x && ln -s /etc e", "chown 0 file", "mount /dev/sda1 x",
    "env", "printenv", "export -p", "declare -px", "nsenter -t 1 -m sh", "dd if=/dev/zero of=x",
    "cat /proc/self/environ", "docker run -v /:/host alpine", "ls /var/run/docker.sock", "env FOO=1 su",
])
def test_policy_blocks_posix(cmd, troot):
    assert not check_command(cmd, troot, troot), cmd


@posix_only
def test_cd_containment_posix(troot):
    assert resolve_cd("/", troot, troot)[0] is None
    assert resolve_cd("/etc", troot, troot)[0] is None
    assert resolve_cd(str(troot / "sub"), troot, troot)[0] == troot / "sub"


@win_only
def test_cd_containment(troot):
    target, err = resolve_cd("sub", troot, troot)
    assert target == troot / "sub" and not err
    assert resolve_cd("..", troot, troot)[0] is None
    assert resolve_cd("C:\\", troot, troot)[0] is None
    assert resolve_cd("..", troot / "sub", troot)[0] == troot


async def _recv_until(comm, kind, timeout=30):
    msgs = []
    while True:
        msg = await comm.receive_json_from(timeout=timeout)
        msgs.append(msg)
        if msg["type"] == kind:
            return msgs


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_terminal_session(plain_project, admin_user, workroot):
    comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), f"/ws/terminal/{plain_project.id}/")
    comm.scope["user"] = admin_user
    connected, _ = await comm.connect()
    assert connected
    ready = await comm.receive_json_from(timeout=10)
    assert ready["type"] == "ready" and ready["cwd"] == "~"
    assert ready["shell"] == ("powershell" if WIN else "bash") and ready["shell"] in ready["shells"]

    await comm.send_json_to({"type": "run", "command": "Get-Location | Select-Object -ExpandProperty Path" if WIN else "python -c \"import os; print(os.getcwd())\""})
    msgs = await _recv_until(comm, "exit")
    out = "".join(m.get("data", "") for m in msgs if m["type"] == "output")
    assert str((workroot / "plain").resolve()).lower() in out.lower(), msgs
    assert msgs[-1]["code"] == 0

    # cd inside the root works, outside is denied
    await comm.send_json_to({"type": "run", "command": "cd src"})
    msgs = await _recv_until(comm, "exit")
    assert msgs[-1]["cwd"] == "~/src"
    await comm.send_json_to({"type": "run", "command": "cd ..\\.." if WIN else "cd ../.."})
    msgs = await _recv_until(comm, "exit")
    assert any(m["type"] == "denied" for m in msgs) and msgs[-1]["cwd"] == "~/src"

    await comm.send_json_to({"type": "run", "command": "type ..\\..\\..\\secret.txt" if WIN else "cat ../../../secret.txt"})
    msgs = await _recv_until(comm, "exit")
    assert msgs[0]["type"] == "denied"

    # secrets in the environment never reach the terminal
    await comm.send_json_to({"type": "run", "command": "echo $env:CODE_STUDIO_ENGINE_TOKEN" if WIN else "echo $CODE_STUDIO_ENGINE_TOKEN"})
    msgs = await _recv_until(comm, "exit")
    out = "".join(m.get("data", "") for m in msgs if m["type"] == "output")
    assert "test-engine-token" not in out

    # Code Studio's own Django settings module must not leak into the project's commands
    assert os.environ.get("DJANGO_SETTINGS_MODULE")  # set for the test process by pytest-django
    await comm.send_json_to({"type": "run", "command": "python -c \"import os; print('DSM=' + str(os.environ.get('DJANGO_SETTINGS_MODULE')))\""})
    msgs = await _recv_until(comm, "exit")
    out = "".join(m.get("data", "") for m in msgs if m["type"] == "output")
    assert "DSM=None" in out, msgs
    await comm.disconnect()

    actions = await asyncio.to_thread(lambda: list(AuditEvent.objects.values_list("action", "outcome")))
    assert ("terminal.session_start", "success") in actions
    assert ("terminal.command", "denied") in actions
    assert ("terminal.command", "success") in actions


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_terminal_cancel_kills_process(plain_project, admin_user):
    comm = WebsocketCommunicator(URLRouter(websocket_urlpatterns), f"/ws/terminal/{plain_project.id}/")
    comm.scope["user"] = admin_user
    await comm.connect()
    await comm.receive_json_from(timeout=10)
    await comm.send_json_to({"type": "run", "command": "Start-Sleep -Seconds 60" if WIN else "sleep 60"})
    await asyncio.sleep(1.5)
    await comm.send_json_to({"type": "cancel"})
    msgs = await _recv_until(comm, "exit", timeout=20)
    assert msgs[-1]["code"] != 0, msgs
    await comm.disconnect()
