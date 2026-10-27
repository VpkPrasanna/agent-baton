"""Neutral session model that every adapter reads into and writes out of."""
from __future__ import annotations

from dataclasses import dataclass, field

INJECTED_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "<system-reminder>",
    "<command-",
    "<local-command",
    "# AGENTS.md",
    "<session_context>",  # Gemini CLI puts its environment summary in the first user message
    "Caveat:",
)


def is_injected(text: str) -> bool:
    """True for text the agent harness inserted, not something the user typed."""
    return text.lstrip().startswith(INJECTED_PREFIXES)


PLACEHOLDER_PREFIXES = (
    "Tool uses were interrupted",  # Kiro
    "[Request interrupted by user",  # Claude Code
    "No response requested",  # Claude Code
)


def is_placeholder(text: str) -> bool:
    """True for filler an agent inserts when a turn was interrupted: not real content."""
    return text.strip().startswith(PLACEHOLDER_PREFIXES)


def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clip(text: str, limit: int) -> str:
    """Truncate without flattening: keeps newlines so markdown and code stay readable."""
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n… [truncated]"


@dataclass
class Message:
    role: str  # user | assistant | tool
    kind: str  # text | tool_call | tool_result
    text: str
    meta: dict = field(default_factory=dict)  # tool_call: {"name": str, "args": dict}


@dataclass
class SessionInfo:
    agent: str
    id: str
    cwd: str
    title: str
    updated: float  # epoch seconds
    source_path: str = ""  # the session's file on disk, when it has one (needed by official importers)
    subagent: bool = False  # a session the agent itself started (Kiro tags these); shown, but labelled


@dataclass
class Session:
    info: SessionInfo
    messages: list[Message]
