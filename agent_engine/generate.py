"""One-shot text generation (e.g. commit messages) through an agent CLI.

Unlike a task there is no staging copy and no review: the CLI runs read-only, with no
tools where the CLI allows it, in an empty scratch folder that is deleted afterwards.
Only the prompt (which the IDE builds) reaches the agent.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from cs_common.datadir import ensure_dir

from .adapters.base import AdapterUnavailable, BaseAdapter

# Used when the caller names no adapter: the first one that is available and switched on.
PREFERENCE = ("claude-code", "codex", "antigravity")
MAX_PROMPT_CHARS = 200_000
# A short reply needs no top model; callers can still pass "model".
DEFAULT_MODELS = {"claude-code": "haiku"}
_FENCE = re.compile(r"^```[\w-]*\n(.*?)\n```$", re.DOTALL)


def pick_adapter(adapters: dict[str, BaseAdapter], wanted: str | None) -> BaseAdapter:
    if wanted:
        adapter = adapters.get(wanted)
        if adapter is None:
            raise LookupError(f"unknown agent {wanted!r}")
        probe = adapter.probe()
        if not (probe.available and adapter.is_enabled()):
            raise AdapterUnavailable(f"{adapter.display_name} is not available")
        return adapter
    for ident in PREFERENCE:
        adapter = adapters.get(ident)
        if adapter is not None and adapter.is_enabled() and adapter.probe().available:
            return adapter
    raise AdapterUnavailable("No AI agent is available. Install and log in to Claude Code or Codex.")


def clean_reply(text: str) -> str:
    text = (text or "").strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    return text


def generate(engine_dir: Path, adapters: dict[str, BaseAdapter], payload: dict) -> dict:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt is too long (max {MAX_PROMPT_CHARS} characters)")
    adapter = pick_adapter(adapters, payload.get("adapter") or None)
    limit = adapter.max_prompt_chars()
    if limit and len(prompt) > limit:
        prompt = prompt[:limit - 40] + "\n[diff truncated]\n"
    timeout = min(max(int(payload.get("timeout_seconds") or 120), 10), 300)
    policy = {"read_only": True, "no_tools": True, "timeout_seconds": timeout}
    model = payload.get("model") or DEFAULT_MODELS.get(adapter.identifier)
    if model:
        policy["model"] = str(model)[:100]
    scratch = Path(tempfile.mkdtemp(prefix="gen-", dir=ensure_dir(engine_dir / "scratch")))
    try:
        run_id = adapter.start_task({}, prompt, scratch, policy)
        for _ in adapter.stream_events(run_id):  # blocks until the CLI exits
            pass
        result = adapter.get_result(run_id)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    text = clean_reply(result.get("result") or "")
    if result.get("status") != "succeeded" or not text:
        raise RuntimeError(result.get("error") or f"{adapter.display_name} returned no text")
    return {"text": text, "adapter": adapter.identifier, "adapter_name": adapter.display_name,
            "stats": result.get("stats") or {}}
