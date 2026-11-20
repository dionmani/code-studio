"""Low-level file helpers: hashing, version tokens and atomic writes."""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", ".idea", ".next", "dist", "build", ".gradle", "target"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def version_of(path: Path, data: bytes | None = None) -> dict:
    """Version token = mtime_ns:size:sha256. Conflicts are decided on the hash;
    mtime/size are informational (and let clients spot touch-only changes)."""
    st = path.stat()
    digest = sha256_bytes(data) if data is not None else sha256_file(path)
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "sha256": digest,
            "token": f"{st.st_mtime_ns}:{st.st_size}:{digest}"}


def token_hash(token: str | None) -> str | None:
    if not token or not isinstance(token, str):
        return None
    parts = token.split(":")
    return parts[-1] if len(parts[-1]) == 64 else None


def atomic_write(path: Path, data: bytes) -> None:
    """Write to a temp file in the same directory, fsync, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.cs-tmp-{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            if path.exists():
                os.chmod(tmp, path.stat().st_mode)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript", ".json": "json", ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss", ".less": "less", ".md": "markdown", ".yml": "yaml", ".yaml": "yaml",
    ".xml": "xml", ".sh": "shell", ".bash": "shell", ".ps1": "powershell", ".psm1": "powershell",
    ".bat": "bat", ".cmd": "bat", ".sql": "sql", ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".toml": "ini", ".ini": "ini", ".cfg": "ini", ".dockerfile": "dockerfile",
    ".lua": "lua", ".r": "r", ".scala": "scala", ".txt": "plaintext",
}


def language_for(name: str) -> str:
    lower = name.lower()
    if lower == "dockerfile":
        return "dockerfile"
    return LANG_BY_EXT.get(os.path.splitext(lower)[1], "plaintext")
