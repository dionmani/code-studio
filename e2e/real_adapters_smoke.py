"""Smoke-test real CLI adapters directly against a live Agent Engine.

    .venv\\Scripts\\python e2e\\real_adapters_smoke.py [claude-code] [codex]

Runs a tiny file-creation task per adapter in an isolated staging copy and
prints the resulting state, assignment status, event types and proposed changes.
Uses real model calls (small cost).
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from cs_common import signing  # noqa: E402


TOKEN = "smoke-" + str(int(time.time()))
import os  # noqa: E402
os.environ.setdefault("CODE_STUDIO_ENGINE_TOKEN", TOKEN)


from tests.conftest import LiveEngine  # noqa: E402


def call(eng, method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else b""
    req = httpx.Request(method, eng.url + path, content=body or None)
    req.headers.update(signing.sign(eng.config.token, method, req.url.raw_path.decode(), body))
    with httpx.Client(timeout=60) as c:
        r = c.send(req)
    r.raise_for_status()
    return r.json()


def main():
    adapters = sys.argv[1:] or ["claude-code", "codex"]
    src = REPO / ".e2e" / "adapter-smoke"
    if src.exists():
        shutil.rmtree(src)
    src.mkdir(parents=True)
    (src / "README.md").write_bytes(b"# smoke\n")
    # NB: not tempfile.mkdtemp(): on Windows it applies an owner-only ACL that the Codex
    # sandbox identity cannot write through. Production data dirs use a plain mkdir.
    engine_dir = REPO / ".e2e" / "adapter-smoke-engine"
    shutil.rmtree(engine_dir, ignore_errors=True)
    engine_dir.mkdir(parents=True)
    eng = LiveEngine(engine_dir)
    eng.config.token = TOKEN
    eng.start()
    ok = True
    try:
        for adapter in adapters:
            fname = f"{adapter.replace('-', '_')}_hello.txt"
            t = call(eng, "POST", "/tasks", {
                "objective": f"Create a new file named {fname} in the current directory whose entire content is "
                             f"the single line: hello from {adapter}. Do not modify any other file.",
                "project": {"id": 1, "name": "smoke", "root": str(src)},
                "assignments": [{"adapter": adapter, "role": "implementer", "policy": {"timeout_seconds": 300}}]})
            tid = t["id"]
            while True:
                t = call(eng, "GET", f"/tasks/{tid}")
                if t["state"] in ("awaiting_review", "completed", "failed", "cancelled"):
                    break
                time.sleep(2)
            events = call(eng, "GET", f"/tasks/{tid}/events?after=0&follow=0")["events"]
            changes = call(eng, "GET", f"/tasks/{tid}/changes")["changes"]
            types = sorted({e["type"] for e in events})
            a = t["assignments"][0]
            print(f"\n== {adapter}: state={t['state']} assignment={a['status']} exit={a.get('exit_code')}")
            print("   event types:", ", ".join(types))
            print("   result:", (a.get("result") or a.get("error") or "")[:200])
            for c in changes:
                print(f"   change: {c['action']} {c['path']} -> {c.get('new_text', '')!r}")
            good = t["state"] == "awaiting_review" and any(c["path"] == fname for c in changes) \
                and not (src / fname).exists()
            print("   VERDICT:", "OK" if good else "PROBLEM")
            ok &= good
    finally:
        eng.stop()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
