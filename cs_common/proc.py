"""Subprocess helpers: new process group + whole-tree termination."""
from __future__ import annotations

import os
import signal
import subprocess
import sys

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

IS_WINDOWS = sys.platform == "win32"


def popen_kwargs() -> dict:
    if IS_WINDOWS:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    """Terminate ``proc`` and all descendants."""
    if proc.poll() is not None:
        return
    if psutil is not None:
        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
        except psutil.NoSuchProcess:
            return
        for p in children + [parent]:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(children, timeout=timeout)
        # Reap our direct child through subprocess, not psutil: on POSIX a
        # psutil wait would consume the exit status and Popen would report 0.
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass
        return
    if IS_WINDOWS:  # pragma: no cover
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:  # pragma: no cover
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()
