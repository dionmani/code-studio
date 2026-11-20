# Git integration — Dulwich evaluation and hardening

**Decision (PROJECT_DESIGN.md §5, approved 2026-11-16):** use Dulwich
(pure-Python Git, version 1.2.21) instead of shelling out to `git.exe`.
GitHub stays optional, and GitHub Actions and container builds are unchanged.

## Supported operations (`vcs/service.py`)
| Operation | How it works |
|---|---|
| Status | `porcelain.status(untracked_files="all")`: branch, HEAD, remotes (credentials in URLs masked), staged, unstaged and untracked changes. |
| Diff | Unified diff plus original/modified text for Monaco. Unstaged diffs compare index (or HEAD) to the working tree; staged diffs compare HEAD to the index. Binary files are detected. |
| Stage / unstage | Stage uses `porcelain.add`; deletions update the index directly. Unstage resets index entries to HEAD, or removes them if they aren't in HEAD. |
| Commit | Author is the signed-in user, `no_verify=True`, and refused when nothing is staged. |
| Pull | Fast-forward only. Refused if the working tree is dirty. Needs explicit confirmation. |
| Push | Pushes the current branch. Needs explicit confirmation. Optional HTTPS username/token, used once and never stored. |

Unmanaged folders: `git/status` returns `{"enabled": false}` without touching
the disk. Code Studio never runs `git init`. Git mode is detected only from
a `.git` folder at the project root.

## Repository content is untrusted: hardening
1. **Hooks never run.** `SafeRepo` clears Dulwich's shell-hook registry
   (`pre-commit`, `commit-msg`, `post-commit`, …). Tested with hooks that would
   write a marker file.
2. **Filter drivers never run.** Dulwich 1.2 *does* execute
   `filter.<name>.clean`/`smudge`/`process` commands from `.gitattributes` and
   config. `SafeRepo.get_blob_normalizer` uses a filter registry that refuses
   every config-defined driver and drops the LFS factory, while keeping
   built-in line-ending handling. Tested with a malicious `filter.evil` config.
3. **No SSH transport.** `core.sshCommand` in repo config could run any
   program, so push/pull accept only `http(s)://`, `git://`, `file://` and
   local paths.
4. `.git/` is protected from the file API (no reads or writes of hooks/config
   through the editor), and is hidden in the tree.
5. Dulwich's credential module doesn't run credential helpers.

## Windows note: native accelerators
Windows Application Control (Smart App Control) on this machine blocks
Dulwich's `_objects` C extension, but not its Rust `_diff_tree` extension.
That mixed state breaks tree diffs during pull (`'generator' object is not an
instance of 'Sequence'`). `vcs/service.py` detects it and switches the
diff-tree helpers to their pure-Python versions.

## Line endings (Windows checkouts)
A repository cloned on Windows with Git for Windows' global `core.autocrlf=true`
has CRLF working files while Git stores LF. That setting lives in the Windows
user profile, not in the repo, so Linux/Docker Git (and Dulwich) would report
every line of every file as modified. For each project Code Studio:
1. uses the repository's own `core.autocrlf` if it sets one;
2. otherwise samples up to 40 tracked text files and, if most are CRLF on disk
   while LF in Git, applies `core.autocrlf=true` **in memory** (`.git/config`
   is never modified);
3. lets an administrator override it in **Git settings → Line endings**
   (`auto` / `crlf` / `lf`). Changing it clears the index stat cache so the
   next status re-reads every file.
Commits made this way store LF, exactly as Git for Windows would.

## HTTPS logins (push/pull)
Each user saves their own login per host (e.g. `github.com`) in **Git settings**:
username plus an access token (GitHub: fine-grained token with
*Contents: Read and write*). Tokens are sealed with an HMAC-SHA256 stream
cipher plus HMAC tag, using a key in `<data dir>/credentials.key`, separate
from the database. They are write-only through the API, sent only to the host
they were saved for (no redirects are followed), and redacted from logs.
**Test access** checks the remote's `git-upload-pack` and
`git-receive-pack` endpoints to report "can pull" and "can push" without
changing anything. Rejected or missing logins return a clear error that
points to Git settings.
