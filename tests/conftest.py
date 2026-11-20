from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model

TOKEN = os.environ["CODE_STUDIO_ENGINE_TOKEN"]


@pytest.fixture(autouse=True)
def isolated_data_dir(settings, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    settings.DATA_DIR = data
    settings.CODE_STUDIO = dict(settings.CODE_STUDIO)
    return data


@pytest.fixture
def workroot(tmp_path) -> Path:
    root = tmp_path / "workroot"
    root.mkdir()
    return root


@pytest.fixture
def admin_user(db):
    return get_user_model().objects.create_superuser("admin", "admin@example.com", "pw-admin-123")


@pytest.fixture
def plain_user(db):
    return get_user_model().objects.create_user("bob", "bob@example.com", "pw-bob-123")


@pytest.fixture
def client_admin(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def root(admin_user, workroot):
    from projects.models import AllowlistedRoot
    from projects.paths import validate_root_path

    return AllowlistedRoot.objects.create(alias="work", path=str(validate_root_path(str(workroot))),
                                          created_by=admin_user)


def _make_project(root, admin_user, rel, git_mode):
    from projects.models import Project

    p = Project.objects.create(name=rel, root=root, relative_path=rel, git_mode=git_mode, owner=admin_user)
    p.members.add(admin_user)
    return p


@pytest.fixture
def plain_project(root, admin_user, workroot):
    d = workroot / "plain"
    d.mkdir()
    (d / "hello.txt").write_text("hello\n", encoding="utf-8", newline="")
    (d / "src").mkdir()
    (d / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8", newline="")
    return _make_project(root, admin_user, "plain", "unmanaged")


def init_git_repo(path: Path):
    from dulwich import porcelain

    path.mkdir(parents=True, exist_ok=True)
    repo = porcelain.init(str(path))
    (path / "README.md").write_text("# demo\n", encoding="utf-8", newline="")
    (path / "main.py").write_text("x = 1\n", encoding="utf-8", newline="")
    porcelain.add(repo, paths=[str(path / "README.md"), str(path / "main.py")])
    porcelain.commit(repo, message=b"initial", author=b"T <t@example.com>", committer=b"T <t@example.com>")
    repo.close()


@pytest.fixture
def git_project(root, admin_user, workroot):
    init_git_repo(workroot / "repo")
    return _make_project(root, admin_user, "repo", "git")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveEngine:
    def __init__(self, data_dir: Path):
        import uvicorn

        from agent_engine.app import create_app
        from agent_engine.config import EngineConfig

        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.config = EngineConfig(data_dir=data_dir, token=TOKEN, port=self.port, enable_test_adapter=True)
        self.app = create_app(self.config)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("engine did not start")
            time.sleep(0.05)
        return self

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def live_engine(settings, tmp_path):
    engine_data = tmp_path / "engine-data"
    engine_data.mkdir()
    eng = LiveEngine(engine_data).start()
    settings.CODE_STUDIO["ENGINE_URL"] = eng.url
    from agents_client import engine_client

    engine_client._health_cache.update(at=0.0, value=None)
    yield eng
    eng.stop()


@pytest.fixture
def fixture_profile(db, admin_user):
    from agents_client.models import AgentAdapter, AgentProfile

    adapter, _ = AgentAdapter.objects.get_or_create(identifier="fixture", defaults={"available": True})
    return AgentProfile.objects.create(name="Fixture implementer", adapter=adapter, role="implementer",
                                       execution_policy={"timeout_seconds": 60})


def wait_for(fn, timeout=30, interval=0.2):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"condition not met in {timeout}s (last={last!r})")
