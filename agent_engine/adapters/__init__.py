from .antigravity import AntigravityAdapter
from .base import AdapterUnavailable, BaseAdapter, ProbeResult
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .fixture import FixtureAdapter
from .verify import VerifyAdapter


def build_registry(enable_test_adapter: bool = False, settings=None) -> dict[str, BaseAdapter]:
    adapters: list[BaseAdapter] = [ClaudeCodeAdapter(settings), CodexAdapter(settings), AntigravityAdapter(settings),
                                   VerifyAdapter(settings)]
    if enable_test_adapter:
        adapters.append(FixtureAdapter(settings))
    return {a.identifier: a for a in adapters}


__all__ = ["AdapterUnavailable", "BaseAdapter", "ProbeResult", "build_registry"]
