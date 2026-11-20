"""Engine configuration (environment variables).

  CODE_STUDIO_DATA_DIR            shared data dir (engine uses <data>/engine)
  CODE_STUDIO_ENGINE_TOKEN        service token (default: <data>/engine_token)
  CODE_STUDIO_ENGINE_HOST/PORT    bind address (default 127.0.0.1:8765)
  CODE_STUDIO_ENGINE_ALLOWED_ROOTS  optional ';'-separated roots that source
                                  projects must live under (defence in depth)
  CODE_STUDIO_ENGINE_MAX_TASKS    concurrent tasks (default 2)
  CODE_STUDIO_ENGINE_TEST_ADAPTER "1" enables the deterministic fixture adapter
  CODE_STUDIO_ENGINE_MAX_WORKSPACE_MB  staging copy size cap (default 300)
  CODE_STUDIO_ENGINE_WORKSPACE_RETENTION_DAYS  how long a finished task keeps
                                  its staging copy for follow-ups (default 7)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from cs_common.datadir import default_data_dir, engine_token, ensure_dir


@dataclass
class EngineConfig:
    data_dir: Path
    token: str
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_roots: list[Path] = field(default_factory=list)
    max_tasks: int = 2
    enable_test_adapter: bool = False
    max_workspace_bytes: int = 300 * 1024 * 1024
    max_workspace_files: int = 50_000
    workspace_retention_days: float = 7.0

    @property
    def engine_dir(self) -> Path:
        return ensure_dir(self.data_dir / "engine")

    @property
    def workspaces_dir(self) -> Path:
        return ensure_dir(self.engine_dir / "workspaces")

    @property
    def tasks_dir(self) -> Path:
        return ensure_dir(self.engine_dir / "tasks")

    @classmethod
    def from_env(cls) -> "EngineConfig":
        data_dir = ensure_dir(default_data_dir())
        roots = [Path(os.path.realpath(p)) for p in os.environ.get("CODE_STUDIO_ENGINE_ALLOWED_ROOTS", "").split(";")
                 if p.strip()]
        return cls(
            data_dir=data_dir,
            token=engine_token(data_dir),
            host=os.environ.get("CODE_STUDIO_ENGINE_HOST", "127.0.0.1"),
            port=int(os.environ.get("CODE_STUDIO_ENGINE_PORT", "8765")),
            allowed_roots=roots,
            max_tasks=int(os.environ.get("CODE_STUDIO_ENGINE_MAX_TASKS", "2")),
            enable_test_adapter=os.environ.get("CODE_STUDIO_ENGINE_TEST_ADAPTER") == "1",
            max_workspace_bytes=int(os.environ.get("CODE_STUDIO_ENGINE_MAX_WORKSPACE_MB", "300")) * 1024 * 1024,
            workspace_retention_days=float(os.environ.get("CODE_STUDIO_ENGINE_WORKSPACE_RETENTION_DAYS", "7")),
        )
