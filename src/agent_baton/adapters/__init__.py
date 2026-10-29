from .base import Adapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .cursor import CursorAdapter
from .gemini import GeminiAdapter
from .kiro import KiroAdapter
from .kiro_ide import KiroIdeAdapter

ADAPTERS: dict[str, type[Adapter]] = {
    "kiro": KiroAdapter,
    "claude": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "cursor": CursorAdapter,
    "gemini": GeminiAdapter,
    "kiro-ide": KiroIdeAdapter,
}


def get(name: str, include_subagents: bool = True, use_kiro_cli: bool = True) -> Adapter:
    try:
        cls = ADAPTERS[name]
    except KeyError:
        raise SystemExit(f"unknown agent '{name}'. choose from: {', '.join(ADAPTERS)}")
    return cls(include_subagents=include_subagents, use_kiro_cli=use_kiro_cli) if cls is KiroAdapter else cls()
