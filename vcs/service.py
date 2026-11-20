"""Git operations via Dulwich (pure Python; git.exe is never invoked).

Hardening (repository content is untrusted):
* ``SafeRepo`` clears Dulwich's shell hook registry (pre-commit, commit-msg,
  post-commit, ...), so ``.git/hooks/*`` never execute.
* Filter drivers configured in repo/user config (``filter.<x>.clean/smudge/
  process``) are never instantiated; only Dulwich's built-in line-ending
  handling is used. The LFS factory is disabled as well.
* Remotes are restricted to http(s)/git:// and local paths: ssh transports are
  refused because ``core.sshCommand`` would let repo config run a program.
* Pull refuses to run on a dirty working tree, and push/pull require explicit
  confirmation from the user.
"""
from __future__ import annotations

import difflib
import io
import os
import re
from pathlib import Path

from dulwich import porcelain
from dulwich.config import ConfigDict, StackedConfig
from dulwich.errors import NotGitRepository
from dulwich.filters import FilterBlobNormalizer, FilterContext, FilterRegistry
from dulwich.object_store import tree_lookup_path
from dulwich.repo import Repo

from core.api import ApiError
from projects.paths import project_root, resolve, split_relative

MAX_DIFF_BYTES = 2 * 1024 * 1024


def _harmonize_dulwich_accelerators() -> None:
    """Dulwich ships optional native accelerators. If ``_objects`` cannot load
    (e.g. Windows Application Control blocks the DLL) but ``_diff_tree`` can,
    the Rust tree walker receives pure-Python generators and fails. Use the
    pure-Python diff helpers consistently in that case."""
    try:
        import dulwich._objects  # noqa: F401
    except ImportError:
        from dulwich import diff_tree

        diff_tree._merge_entries = diff_tree._merge_entries_py
        diff_tree._is_tree = diff_tree._is_tree_py
        diff_tree._count_blocks = diff_tree._count_blocks_py


_harmonize_dulwich_accelerators()


class _SafeFilterRegistry(FilterRegistry):
    def __init__(self, config=None, repo=None):
        super().__init__(config, repo)
        self._factories.pop("lfs", None)

    def _create_from_config(self, name):  # never build process/command drivers
        return None


class SafeRepo(Repo):
    autocrlf: bytes | None = None   # in-memory core.autocrlf override (never written to .git/config)

    def __init__(self, root, *args, **kwargs):
        super().__init__(root, *args, **kwargs)
        self.hooks.clear()

    def get_config_stack(self):
        stack = super().get_config_stack()
        if self.autocrlf is None:
            return stack
        overlay = ConfigDict()
        overlay.set((b"core",), b"autocrlf", self.autocrlf)
        return StackedConfig([overlay] + list(stack.backends), writable=stack.writable)

    def get_blob_normalizer(self, config=None, tree=None):
        if config is None:
            config = self.get_config_stack()
        registry = _SafeFilterRegistry(config, self)
        return FilterBlobNormalizer(config, self.get_gitattributes(tree), repo=self,
                                    filter_context=FilterContext(registry))


# ---- line endings ----------------------------------------------------------
# A repository cloned on Windows with the user's global core.autocrlf=true has
# CRLF working files while Git stores LF. That setting lives in the Windows
# user profile, not in the repo, so any other Git (Linux, Docker, Dulwich)
# sees every line of every file as modified. Code Studio detects this and
# applies core.autocrlf=true in memory (the repository config is not changed).

_detect_cache: dict[str, tuple[float, bool]] = {}


def _detect_windows_checkout(repo: Repo, base: Path, sample: int = 40) -> bool:
    try:
        index_path = os.path.join(repo.controldir(), "index")
        stamp = os.path.getmtime(index_path)
    except OSError:
        return False
    cached = _detect_cache.get(str(base))
    if cached and cached[0] == stamp:
        return cached[1]
    crlf = lf = 0
    try:
        index = repo.open_index()
        for path, entry in index.iteritems():
            if crlf + lf >= sample:
                break
            sha = getattr(entry, "sha", None)
            if sha is None or (getattr(entry, "mode", 0) & 0o170000) != 0o100000 or entry.size > 512 * 1024:
                continue
            try:
                blob = repo[sha].data
                if b"\x00" in blob[:8192] or b"\n" not in blob or b"\r" in blob:
                    continue  # binary, single-line, or CRLF stored in Git: no signal
                work = (base / _dec(path)).read_bytes()
            except (KeyError, OSError):
                continue
            if b"\r\n" in work:
                crlf += 1
            elif b"\n" in work:
                lf += 1
    except Exception:  # detection is best-effort; never break status
        return False
    result = crlf > lf
    _detect_cache[str(base)] = (stamp, result)
    return result


def invalidate_stat_cache(project) -> None:
    """Force the next status to re-read every tracked file.

    Status caches file stat data in the index once content is known to match
    (Git does the same). After the line-ending mode changes, that cache would
    hide differences, so zero the cached size/mtime (content is unaffected).
    """
    import dataclasses

    repo, _base = open_repo(project)
    with repo:
        index = repo.open_index()
        for path, entry in list(index.iteritems()):
            if getattr(entry, "sha", None) is None:
                continue
            if dataclasses.is_dataclass(entry):
                index[path] = dataclasses.replace(entry, mtime=(0, 0), size=0)
            else:  # older dulwich namedtuple entries
                index[path] = entry._replace(mtime=(0, 0), size=0)
        index.write()


def line_endings_info(repo: Repo, base: Path, setting: str) -> dict:
    """Decide the effective core.autocrlf and explain where it came from."""
    if setting == "crlf":
        return {"setting": setting, "autocrlf": "true", "effective": "crlf", "source": "project setting"}
    if setting == "lf":
        return {"setting": setting, "autocrlf": "false", "effective": "lf", "source": "project setting"}
    try:
        configured = repo.get_config().get((b"core",), b"autocrlf").decode().strip().lower()
    except KeyError:
        configured = None
    if configured:
        eff = "crlf" if configured == "true" else "lf"
        return {"setting": setting, "autocrlf": None, "effective": eff,
                "source": f"repository config (core.autocrlf={configured})"}
    if _detect_windows_checkout(repo, base):
        return {"setting": setting, "autocrlf": "true", "effective": "crlf",
                "source": "detected a Windows checkout (CRLF files, LF in Git)"}
    return {"setting": setting, "autocrlf": None, "effective": "lf", "source": "files match Git (no conversion)"}


def open_repo(project) -> tuple[SafeRepo, Path]:
    if project.git_mode != "git":
        raise ApiError("This project is an unmanaged folder; Git is not enabled", 400, code="not_git")
    base = project_root(project)
    if not (base / ".git").is_dir():
        raise ApiError("No .git directory at the project root", 400, code="not_git")
    try:
        repo = SafeRepo(str(base))
    except NotGitRepository:
        raise ApiError("Not a Git repository", 400, code="not_git")
    info = line_endings_info(repo, base, getattr(project, "git_line_endings", "auto") or "auto")
    repo.autocrlf = info["autocrlf"].encode() if info["autocrlf"] else None
    repo.line_endings = info
    return repo, base


def _dec(p: bytes | str) -> str:
    return p.decode("utf-8", errors="replace") if isinstance(p, bytes) else p


def branch_info(repo: Repo) -> dict:
    try:
        branch = _dec(porcelain.active_branch(repo))
        detached = False
    except (KeyError, IndexError, ValueError):
        branch, detached = None, True
    try:
        head = repo.head().decode()
    except KeyError:
        head = None
    if detached and head:
        branch = head[:8]
    remotes = {}
    cfg = repo.get_config()
    for section in cfg.sections():
        if len(section) == 2 and section[0] == b"remote":
            try:
                remotes[_dec(section[1])] = _redact_url(_dec(cfg.get(section, b"url")))
            except KeyError:
                pass
    return {"branch": branch, "detached": detached, "head": head, "remotes": remotes}


def _redact_url(url: str) -> str:
    return re.sub(r"(://[^/@:]+):[^@/]+@", r"\1:***@", url)


def status(project) -> dict:
    repo, base = open_repo(project)
    with repo:
        st = porcelain.status(repo, untracked_files="all")
        info = branch_info(repo)
        info["line_endings"] = repo.line_endings
    staged = []
    for kind in ("add", "delete", "modify"):
        for p in st.staged.get(kind, []):
            staged.append({"path": _dec(p), "status": {"add": "added", "delete": "deleted", "modify": "modified"}[kind]})
    unstaged = []
    for p in st.unstaged:
        rel = _dec(p)
        exists = (base.joinpath(*split_relative(rel))).exists() if rel else True
        unstaged.append({"path": rel, "status": "modified" if exists else "deleted"})
    untracked = [{"path": _dec(p), "status": "untracked"} for p in st.untracked]
    return {"enabled": True, **info, "staged": sorted(staged, key=lambda x: x["path"]),
            "unstaged": sorted(unstaged, key=lambda x: x["path"]),
            "untracked": sorted(untracked, key=lambda x: x["path"]),
            "clean": not (staged or unstaged or untracked)}


def _head_blob(repo: Repo, path: bytes) -> bytes | None:
    try:
        tree = repo[repo.head()].tree
        _mode, sha = tree_lookup_path(repo.__getitem__, tree, path)
        return repo[sha].data
    except (KeyError, NotADirectoryError):
        return None


def _index_blob(repo: Repo, path: bytes) -> bytes | None:
    index = repo.open_index()
    try:
        entry = index[path]
    except KeyError:
        return None
    sha = getattr(entry, "sha", None)
    if sha is None:  # conflicted entry
        return None
    return repo[sha].data


def _text(data: bytes | None) -> tuple[str, bool]:
    if data is None:
        return "", False
    if b"\x00" in data[:8192] or len(data) > MAX_DIFF_BYTES:
        return "", True
    return data.decode("utf-8", errors="replace"), False


def diff(project, rel: str, staged: bool = False) -> dict:
    repo, base = open_repo(project)
    parts = split_relative(rel)
    if not parts:
        raise ApiError("path is required")
    key = "/".join(parts).encode("utf-8")
    with repo:
        if staged:
            original = _head_blob(repo, key)
            modified = _index_blob(repo, key)
        else:
            original = _index_blob(repo, key)
            if original is None:
                original = _head_blob(repo, key)
            target = resolve(project, rel, base=base)
            modified = target.read_bytes() if target.is_file() else None
            if modified is not None and repo.line_endings["effective"] == "crlf" and b"\x00" not in modified[:8192]:
                modified = modified.replace(b"\r\n", b"\n")  # compare as Git stores it (core.autocrlf)
    o_text, o_bin = _text(original)
    m_text, m_bin = _text(modified)
    if o_bin or m_bin:
        unified = f"Binary files a/{rel} and b/{rel} differ\n"
    else:
        unified = "".join(difflib.unified_diff(
            o_text.splitlines(keepends=True), m_text.splitlines(keepends=True),
            fromfile=f"a/{rel}" if original is not None else "/dev/null",
            tofile=f"b/{rel}" if modified is not None else "/dev/null"))
    return {"path": "/".join(parts), "staged": staged, "original": o_text, "modified": m_text,
            "binary": o_bin or m_bin, "diff": unified}


def recent_subjects(project, limit: int = 8) -> list[str]:
    """First lines of the latest commits on HEAD (for matching the repo's message style)."""
    repo, _base = open_repo(project)
    with repo:
        try:
            walker = repo.get_walker(max_entries=limit)
        except KeyError:  # no commits yet
            return []
        return [e.commit.message.decode("utf-8", errors="replace").strip().splitlines()[0]
                for e in walker if e.commit.message.strip()]


def changes_for_message(project, max_chars: int = 20_000, per_file_chars: int = 4_000) -> dict:
    """The changes a commit message should describe: the staged files, or (when nothing is
    staged) every working-tree change, as one size-capped unified diff."""
    st = status(project)
    staged = bool(st["staged"])
    files = st["staged"] if staged else st["unstaged"] + st["untracked"]
    if not files:
        raise ApiError("There are no changes to describe", 400)
    parts, used, omitted = [], 0, 0
    for f in files:
        try:
            text = diff(project, f["path"], staged=staged)["diff"]
        except (ApiError, OSError):
            text = ""
        if not text:
            text = f"{f['status']}: {f['path']}\n"
        if len(text) > per_file_chars:
            text = text[:per_file_chars] + f"\n[... {len(text) - per_file_chars} more characters of this diff]\n"
        if used + len(text) > max_chars:
            omitted += 1
            continue
        parts.append(text)
        used += len(text)
    return {"staged": staged, "files": files, "diff": "".join(parts), "omitted": omitted}


def stage(project, paths: list[str]) -> None:
    repo, base = open_repo(project)
    with repo:
        existing = []
        for rel in paths:
            target = resolve(project, rel, base=base, allow_root=False)
            if target.exists():
                existing.append(str(target))
            else:
                index = repo.open_index()
                key = "/".join(split_relative(rel)).encode("utf-8")
                if key in index:
                    del index[key]
                    index.write()
        if existing:
            porcelain.add(repo, paths=existing)


def unstage(project, paths: list[str]) -> None:
    """Reset index entries to HEAD (or remove them if not in HEAD)."""
    repo, base = open_repo(project)
    from dulwich.index import IndexEntry

    with repo:
        index = repo.open_index()
        for rel in paths:
            key = "/".join(split_relative(rel)).encode("utf-8")
            try:
                tree = repo[repo.head()].tree
                mode, sha = tree_lookup_path(repo.__getitem__, tree, key)
            except (KeyError, NotADirectoryError):
                mode = sha = None
            if sha is None:
                if key in index:
                    del index[key]
                continue
            target = base.joinpath(*split_relative(rel))
            try:
                st = os.lstat(target)
                entry = IndexEntry(ctime=(int(st.st_ctime), 0), mtime=(0, 0), dev=st.st_dev, ino=st.st_ino,
                                   mode=mode, uid=0, gid=0, size=0, sha=sha)
            except OSError:
                entry = IndexEntry(ctime=(0, 0), mtime=(0, 0), dev=0, ino=0, mode=mode, uid=0, gid=0, size=0,
                                   sha=sha)
            index[key] = entry
        index.write()


def commit(project, message: str, author_name: str, author_email: str) -> str:
    if not message or not message.strip():
        raise ApiError("Commit message is required")
    repo, _base = open_repo(project)
    ident = f"{author_name} <{author_email}>".encode("utf-8")
    with repo:
        st = porcelain.status(repo, untracked_files="no")
        if not any(st.staged.get(k) for k in ("add", "delete", "modify")):
            raise ApiError("Nothing staged to commit", 400)
        sha = porcelain.commit(repo, message=message.encode("utf-8"), author=ident, committer=ident, no_verify=True)
    return sha.decode() if isinstance(sha, bytes) else str(sha)


_ALLOWED_REMOTE = re.compile(r"^(https?|git|file)://", re.IGNORECASE)


def _remote_url(repo: Repo, remote: str) -> str:
    if not re.match(r"^[A-Za-z0-9._-]+$", remote or ""):
        raise ApiError("Invalid remote name")
    try:
        url = _dec(repo.get_config().get((b"remote", remote.encode()), b"url"))
    except KeyError:
        raise ApiError(f"Remote '{remote}' is not configured", 400)
    is_local = bool(re.match(r"^[A-Za-z]:[\\/]", url)) or url.startswith(("/", "\\\\", "."))
    if not (_ALLOWED_REMOTE.match(url) or is_local):
        raise ApiError("Only http(s), git:// and local remotes are supported (ssh is disabled: repository "
                       "config could run arbitrary programs via core.sshCommand)", 400)
    return url


def _branch_ref(repo: Repo) -> bytes:
    try:
        return b"refs/heads/" + porcelain.active_branch(repo)
    except (KeyError, IndexError, ValueError):
        raise ApiError("Cannot push/pull from a detached HEAD", 400)


def remote_host(url: str) -> str | None:
    """'https://user@GitHub.com:443/x.git' -> 'github.com' (port kept if non-default)."""
    m = re.match(r"^(https?)://(?:[^@/]*@)?([^/:]+)(?::(\d+))?", url, re.IGNORECASE)
    if not m:
        return None
    scheme, host, port = m.group(1).lower(), m.group(2).lower(), m.group(3)
    if port and not ((scheme == "https" and port == "443") or (scheme == "http" and port == "80")):
        host = f"{host}:{port}"
    return host


def _strip_url_userinfo(url: str) -> str:
    return re.sub(r"^(https?://)[^@/]*@", r"\1", url, flags=re.IGNORECASE)


class Credentials:
    """Resolved HTTPS login for one remote operation (never logged or returned)."""

    def __init__(self, username: str, token: str, label: str):
        self.username, self.token, self.label = username, token, label


def _auth_error(exc: Exception, url: str, creds: Credentials | None) -> ApiError | None:
    from dulwich.client import HTTPUnauthorized

    host = remote_host(url) or "the remote"
    text = str(exc)
    if isinstance(exc, HTTPUnauthorized) or re.search(r"\b(401|403)\b|unauthori[sz]ed|forbidden|authentication", text,
                                                      re.IGNORECASE):
        if creds is None:
            return ApiError(f"{host} asked for a login and no Git credentials are saved for it. Open Git settings "
                            f"(⚙ in the Git panel) and add a username and access token for {host}.", 400,
                            code="git_auth_missing", host=host)
        return ApiError(f"{host} rejected the saved credentials ({creds.label}). The token may be wrong, expired or "
                        "missing permission for this repository (GitHub: Contents → Read and write). Update it in "
                        "Git settings.", 400, code="git_auth_rejected", host=host)
    return None


def _remote_call(fn, url: str, creds: Credentials | None, **kwargs):
    if creds is not None:
        kwargs.update(username=creds.username, password=creds.token)
    try:
        return fn(_strip_url_userinfo(url) if creds is not None else url, **kwargs)
    except ApiError:
        raise
    except Exception as exc:
        mapped = _auth_error(exc, url, creds)
        if mapped:
            raise mapped from None
        raise


def pull(project, remote: str = "origin", creds: Credentials | None = None) -> dict:
    st = status(project)
    if st["staged"] or st["unstaged"]:
        raise ApiError("Working tree has uncommitted changes; commit or discard them before pulling", 409)
    repo, _ = open_repo(project)
    out, err = io.BytesIO(), io.BytesIO()
    with repo:
        url = _remote_url(repo, remote)
        ref = _branch_ref(repo)
        before = repo.head().decode()
        _remote_call(lambda u, **kw: porcelain.pull(repo, u, **kw), url, creds, refspecs=[ref], outstream=out,
                     errstream=err, ff_only=True)
        after = repo.head().decode()
    return {"remote": remote, "before": before, "after": after, "updated": before != after,
            "output": _dec(err.getvalue()[-4000:])}


def push(project, remote: str = "origin", creds: Credentials | None = None) -> dict:
    repo, _ = open_repo(project)
    out, err = io.BytesIO(), io.BytesIO()
    with repo:
        url = _remote_url(repo, remote)
        ref = _branch_ref(repo)
        _remote_call(lambda u, **kw: porcelain.push(repo, u, **kw), url, creds, refspecs=[ref], outstream=out,
                     errstream=err)
    return {"remote": remote, "ref": _dec(ref), "output": _dec(err.getvalue()[-4000:])}


def remote_url_for(project, remote: str = "origin") -> str:
    repo, _ = open_repo(project)
    with repo:
        return _remote_url(repo, remote)


def check_access(url: str, creds: Credentials | None, timeout: float = 15.0) -> dict:
    """Ask the server what we may do, without changing anything.

    Read: the git-upload-pack advertisement (what fetch/pull uses).
    Write: the git-receive-pack advertisement, which hosts such as GitHub only
    serve to callers allowed to push.
    """
    import httpx

    if not re.match(r"^https?://", url, re.IGNORECASE):
        return {"checked": False, "message": "Access checks work for http(s) remotes only."}
    base = _strip_url_userinfo(url).rstrip("/")
    auth = (creds.username, creds.token) if creds else None
    result = {"checked": True, "host": remote_host(url)}
    try:
        # no redirects: the token must only ever go to the host it was saved for
        with httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": "git/2.45 (code-studio)"}) as c:
            for key, service in (("read", "git-upload-pack"), ("write", "git-receive-pack")):
                r = c.get(f"{base}/info/refs", params={"service": service}, auth=auth)
                result[key] = r.status_code == 200 and service.encode() in r.content[:200]
                result[f"{key}_status"] = r.status_code
    except httpx.HTTPError as exc:
        return {"checked": False, "message": f"Could not reach {remote_host(url)}: {type(exc).__name__}"}
    return result
