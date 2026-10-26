from .base import Adapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .cursor import CursorAdapter
from .kiro import KiroAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "kiro": KiroAdapter,
    "claude": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "cursor": CursorAdapter,
}


def get(name: str, include_subagents: bool = True) -> Adapter:
    try:
        cls = ADAPTERS[name]
    except KeyError:
        raise SystemExit(f"unknown agent '{name}'. choose from: {', '.join(ADAPTERS)}")
    return cls(include_subagents=include_subagents) if cls is KiroAdapter else cls()
