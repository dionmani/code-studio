"""Engine settings that can change while the engine runs (no restart).

Stored as JSON in ``<data>/engine/settings.json`` (``engine-data`` in Docker,
so it survives container rebuilds). Today it holds the on/off switch for
adapters that need one (Antigravity); the IDE changes it through
``PUT /agents/{identifier}``.

The first time the file is created it is seeded from the old environment
switches (e.g. ``CODE_STUDIO_ENGINE_ENABLE_ANTIGRAVITY=1``), so upgrading keeps
the current behaviour. After that the file is the only source.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path


class SettingsStore:
    def __init__(self, path: Path, seed: dict[str, dict] | None = None):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = {k: dict(v) for k, v in (loaded.get("adapters") or {}).items() if isinstance(v, dict)}
            except (OSError, ValueError, AttributeError):
                self._data = {}
        else:
            self._data = {k: dict(v) for k, v in (seed or {}).items()}
            self._write()

    def adapter(self, identifier: str) -> dict:
        with self._lock:
            return dict(self._data.get(identifier, {}))

    def update_adapter(self, identifier: str, **values) -> dict:
        with self._lock:
            current = self._data.setdefault(identifier, {})
            current.update(values)
            self._write()
            return dict(current)

    def _write(self) -> None:
        """Atomic replace, so a crash never leaves half a file. Caller holds the lock (or is __init__)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"adapters": self._data}, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
