"""Interactive pseudo-terminal sessions for the local-only System terminal.

Windows uses the built-in ConPTY API (kernel32, Windows 10 1809+) through
ctypes, so no third-party DLL is needed (Smart App Control blocks unsigned
ones). POSIX uses the standard library ``pty`` module.

``PtySession`` is deliberately small and blocking: ``read()`` blocks until
output is available (call it from a thread) and returns ``b""`` once the
shell has exited and all output has been drained.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# Never inherited by the user's shell: Django's settings module, and the markers a
# Claude Code session sets (with them, `claude login` / `claude` refuse to run
# "inside another session" if Code Studio itself was started from Claude Code).
_DROP = {"DJANGO_SETTINGS_MODULE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"}


# --------------------------------------------------------------------------- shells
def available_shells() -> dict[str, list[str]]:
    """Shell name -> argv. The first entry is the default."""
    if IS_WINDOWS:
        shells: dict[str, list[str]] = {}
        pwsh = shutil.which("pwsh.exe")
        winps = shutil.which("powershell.exe") or os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), r"System32\WindowsPowerShell\v1.0\powershell.exe")
        if pwsh:
            shells["pwsh"] = [pwsh, "-NoLogo"]
        shells["powershell"] = [winps, "-NoLogo"]
        shells["cmd"] = [os.environ.get("ComSpec") or "cmd.exe"]
        return shells
    login = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    shells = {Path(login).name: [login, "-l"]}
    for name in ("bash", "zsh", "sh"):
        exe = shutil.which(name)
        if exe and name not in shells:
            shells[name] = [exe] + (["-l"] if name != "sh" else [])
    return shells


def shell_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """The user's environment without Code Studio's own process state.

    Drops ``CODE_STUDIO_*`` (engine token, secret key, ...) and Django's
    settings module, and removes Code Studio's virtualenv from ``PATH`` so the
    prompt only shows an environment the user activated themselves.
    """
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.upper().startswith("CODE_STUDIO_") or key.upper() in _DROP:
            del env[key]
    if sys.prefix != sys.base_prefix:  # running from a venv
        own = os.path.normcase(os.path.realpath(sys.prefix))
        path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
        parts = [p for p in env.get(path_key, "").split(os.pathsep)
                 if p and not os.path.normcase(os.path.realpath(p)).startswith(own)]
        env[path_key] = os.pathsep.join(parts)
        venv_key = next((k for k in env if k.upper() == "VIRTUAL_ENV"), None)
        if venv_key and os.path.normcase(os.path.realpath(env[venv_key])) == own:
            for k in list(env):
                if k.upper() in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "_OLD_VIRTUAL_PATH", "_OLD_VIRTUAL_PROMPT",
                                 "_OLD_VIRTUAL_PYTHONHOME"):
                    del env[k]
    env["TERM_PROGRAM"] = "CodeStudio"
    if not IS_WINDOWS:
        env["TERM"] = "xterm-256color"
    return env


def _clamp(cols: int, rows: int) -> tuple[int, int]:
    return max(2, min(int(cols), 1000)), max(1, min(int(rows), 500))


# --------------------------------------------------------------------------- Windows
if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    HANDLE = ctypes.c_void_p
    HPCON = ctypes.c_void_p
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", HANDLE), ("hStdOutput", HANDLE),
                    ("hStdError", HANDLE)]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    def _fn(name, restype, *argtypes):
        f = getattr(_k32, name)
        f.restype, f.argtypes = restype, list(argtypes)
        return f

    _CreatePipe = _fn("CreatePipe", wintypes.BOOL, ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE), ctypes.c_void_p,
                      wintypes.DWORD)
    _CloseHandle = _fn("CloseHandle", wintypes.BOOL, HANDLE)
    _CreatePseudoConsole = _fn("CreatePseudoConsole", ctypes.c_long, COORD, HANDLE, HANDLE, wintypes.DWORD,
                               ctypes.POINTER(HPCON))
    _ResizePseudoConsole = _fn("ResizePseudoConsole", ctypes.c_long, HPCON, COORD)
    _ClosePseudoConsole = _fn("ClosePseudoConsole", None, HPCON)
    _InitAttrList = _fn("InitializeProcThreadAttributeList", wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD,
                        wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t))
    _UpdateAttr = _fn("UpdateProcThreadAttribute", wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                      ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
    _DeleteAttrList = _fn("DeleteProcThreadAttributeList", None, ctypes.c_void_p)
    _CreateProcessW = _fn("CreateProcessW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                          ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                          ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION))
    _WaitForSingleObject = _fn("WaitForSingleObject", wintypes.DWORD, HANDLE, wintypes.DWORD)
    _GetExitCodeProcess = _fn("GetExitCodeProcess", wintypes.BOOL, HANDLE, ctypes.POINTER(wintypes.DWORD))

    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    STARTF_USESTDHANDLES = 0x00000100
    INFINITE = 0xFFFFFFFF

    def _check(ok, what):
        if not ok:
            raise OSError(ctypes.get_last_error(), f"{what} failed")

    def _env_block(env: dict[str, str]) -> ctypes.Array:
        items = sorted(env.items(), key=lambda kv: kv[0].upper())
        text = "".join(f"{k}={v}\0" for k, v in items if k and "=" not in k[1:]) + "\0"
        return ctypes.create_unicode_buffer(text, len(text))

    class PtySession:
        def __init__(self, argv: list[str], cwd: str, env: dict[str, str], cols: int = 120, rows: int = 30):
            cols, rows = _clamp(cols, rows)
            in_read, in_write, out_read, out_write = HANDLE(), HANDLE(), HANDLE(), HANDLE()
            _check(_CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0), "CreatePipe")
            _check(_CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0), "CreatePipe")
            self._hpc = HPCON()
            hr = _CreatePseudoConsole(COORD(cols, rows), in_read, out_write, 0, ctypes.byref(self._hpc))
            # The pseudo console holds its own references to the far ends.
            _CloseHandle(in_read)
            _CloseHandle(out_write)
            if hr != 0:
                _CloseHandle(in_write)
                _CloseHandle(out_read)
                raise OSError(hr, f"CreatePseudoConsole failed (HRESULT 0x{hr & 0xFFFFFFFF:08X})")
            try:
                self.pid, self._hproc = self._spawn(argv, cwd, env)
            except Exception:
                _ClosePseudoConsole(self._hpc)
                _CloseHandle(in_write)
                _CloseHandle(out_read)
                raise
            self._wfd = msvcrt.open_osfhandle(in_write.value, os.O_WRONLY | os.O_BINARY)
            self._rfd = msvcrt.open_osfhandle(out_read.value, os.O_RDONLY | os.O_BINARY)
            self._lock = threading.Lock()
            self._closed = False
            self.exit_code: int | None = None
            # ConPTY never reports EOF on its own; close it when the shell exits.
            threading.Thread(target=self._wait_exit, daemon=True).start()

        def _spawn(self, argv, cwd, env):
            size = ctypes.c_size_t(0)
            _InitAttrList(None, 1, 0, ctypes.byref(size))
            attrs = ctypes.create_string_buffer(size.value)
            _check(_InitAttrList(attrs, 1, 0, ctypes.byref(size)), "InitializeProcThreadAttributeList")
            try:
                _check(_UpdateAttr(attrs, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, self._hpc,
                                   ctypes.sizeof(HPCON), None, None), "UpdateProcThreadAttribute")
                si = STARTUPINFOEXW()
                si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
                # Without this the child can inherit the server's (redirected) std handles
                # instead of the pseudo console's.
                si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
                si.StartupInfo.hStdInput = si.StartupInfo.hStdOutput = si.StartupInfo.hStdError = None
                si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
                pi = PROCESS_INFORMATION()
                cmdline = ctypes.create_unicode_buffer(_list2cmdline(argv))
                block = _env_block(env)
                _check(_CreateProcessW(None, cmdline, None, None, False,
                                       EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                                       ctypes.cast(block, ctypes.c_void_p), cwd, ctypes.byref(si),
                                       ctypes.byref(pi)), "CreateProcess")
                _CloseHandle(pi.hThread)
                return pi.dwProcessId, pi.hProcess
            finally:
                _DeleteAttrList(attrs)

        def _wait_exit(self):
            _WaitForSingleObject(self._hproc, INFINITE)
            code = wintypes.DWORD()
            _GetExitCodeProcess(self._hproc, ctypes.byref(code))
            self.exit_code = code.value
            self._close_console()

        def _close_console(self):
            with self._lock:
                if self._hpc is None:
                    return
                hpc, self._hpc = self._hpc, None
            # Flushes remaining output and makes read() return b"".
            _ClosePseudoConsole(hpc)

        def read(self, n: int = 65536) -> bytes:
            try:
                return os.read(self._rfd, n)
            except OSError:
                return b""

        def write(self, data: bytes) -> None:
            if not self._closed:
                os.write(self._wfd, data)

        def resize(self, cols: int, rows: int) -> None:
            cols, rows = _clamp(cols, rows)
            with self._lock:
                if self._hpc is not None:
                    _ResizePseudoConsole(self._hpc, COORD(cols, rows))

        def alive(self) -> bool:
            return self.exit_code is None

        def close(self) -> None:
            """Kill the shell and everything it started, then release handles."""
            if self._closed:
                return
            self._closed = True
            _kill_tree(self.pid)
            self._close_console()
            for fd in (self._wfd, self._rfd):
                try:
                    os.close(fd)
                except OSError:
                    pass
            _CloseHandle(self._hproc)

    def _list2cmdline(argv):
        import subprocess

        return subprocess.list2cmdline(argv)


# --------------------------------------------------------------------------- POSIX
else:
    import fcntl
    import pty
    import struct
    import subprocess
    import termios

    class PtySession:
        def __init__(self, argv: list[str], cwd: str, env: dict[str, str], cols: int = 120, rows: int = 30):
            self._master, slave = pty.openpty()
            self._set_size(self._master, *_clamp(cols, rows))

            def _ctty():  # make the pty the shell's controlling terminal (job control, ^C)
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

            try:
                self._proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                              start_new_session=True, preexec_fn=_ctty, close_fds=True)
            finally:
                os.close(slave)
            self.pid = self._proc.pid
            self._closed = False

        @staticmethod
        def _set_size(fd, cols, rows):
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        @property
        def exit_code(self):
            return self._proc.poll()

        def read(self, n: int = 65536) -> bytes:
            try:
                return os.read(self._master, n)
            except OSError:  # EIO once the slave side is closed
                return b""

        def write(self, data: bytes) -> None:
            if not self._closed:
                os.write(self._master, data)

        def resize(self, cols: int, rows: int) -> None:
            if not self._closed:
                self._set_size(self._master, *_clamp(cols, rows))

        def alive(self) -> bool:
            return self._proc.poll() is None

        def close(self) -> None:
            if self._closed:
                return
            self._closed = True
            _kill_tree(self.pid)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
            try:
                os.close(self._master)
            except OSError:
                pass


def _kill_tree(pid: int) -> None:
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return
    try:
        parent = psutil.Process(pid)
        procs = parent.children(recursive=True) + [parent]
    except psutil.NoSuchProcess:
        return
    for p in procs:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(procs, timeout=5)
