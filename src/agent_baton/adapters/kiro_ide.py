from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from ..model import Message, Session, SessionInfo, is_injected, short
from .base import Adapter
from .claude_code import _jsonl

_TITLE_PLACEHOLDERS = {"", "New Session"}
# What Kiro's workflows and agent runtime put in the user turn: not something a person typed
_INTERNAL_PREFIXES = ('{"method":"_kiro', "[WORKFLOW NOTICE", "<original_user_request>", "A workflow you launched")


def default_workspace_dir() -> Path:
    """Kiro IDE's older chat store: one folder per workspace, with a sessions.json index."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    return base / "Kiro" / "User" / "globalStorage" / "kiro.kiroagent" / "workspace-sessions"


def default_sessions_dir() -> Path:
    """Kiro IDE's newer store, next to the CLI's own `cli` folder: <workspace hash>/sess_<id>/."""
    return Path.home() / ".kiro" / "sessions"


def _epoch(value, fallback: float) -> float:
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return float(value) / 1000  # milliseconds
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return fallback


def _internal(text: str) -> bool:
    return text.lstrip().startswith(_INTERNAL_PREFIXES) or is_injected(text)


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class KiroIdeAdapter(Adapter):
    """Reads chats from the Kiro IDE (not the Kiro CLI, which the `kiro` adapter reads). Source only.

    The IDE keeps chats in two places, and a machine can have both: the newer ~/.kiro/sessions/<hash>/sess_*/
    (messages.jsonl with tool calls and results) and the older workspace-sessions folder under Kiro's
    application data (one JSON file per session with the user and assistant text). Both are read-only here.
    The IDE has no command that resumes a session, so there is no way to move a session into it.
    """

    name = "kiro-ide"
    source_only = True

    def __init__(self, workspace_dir: Path | None = None, sessions_dir: Path | None = None):
        self.workspace_dir = workspace_dir or default_workspace_dir()
        self.sessions_dir = sessions_dir or default_sessions_dir()

    # ---- listing (metadata only) ----

    def _new_sessions(self) -> list[SessionInfo]:
        out = []
        for meta_path in self.sessions_dir.glob("*/sess_*/session.json"):
            messages = meta_path.with_name("messages.jsonl")
            meta = _json(meta_path)
            if not messages.exists() or not isinstance(meta, dict):
                continue
            title = meta.get("title") or ""
            if title in _TITLE_PLACEHOLDERS:
                title = self._first_prompt(messages)
            paths = meta.get("workspacePaths") or meta.get("rootPaths") or [""]
            out.append(SessionInfo(self.name, meta.get("id") or meta_path.parent.name, paths[0], short(title, 80),
                                   _epoch(meta.get("lastModifiedAt"), messages.stat().st_mtime), str(messages)))
        return out

    @staticmethod
    def _first_prompt(messages: Path) -> str:
        for rec in _jsonl(messages):
            pl = rec.get("payload") or {}
            if pl.get("type") == "user" and isinstance(pl.get("content"), str) and not _internal(pl["content"]):
                return pl["content"]
        return ""

    def _old_sessions(self) -> list[SessionInfo]:
        out = []
        for index in self.workspace_dir.glob("*/sessions.json"):
            entries = _json(index)
            for e in entries if isinstance(entries, list) else []:
                sid = e.get("sessionId") if isinstance(e, dict) else None
                path = index.with_name(f"{sid}.json") if sid else None
                if not path or not path.exists():
                    continue
                out.append(SessionInfo(self.name, sid, e.get("workspaceDirectory", ""), short(e.get("title") or "", 80),
                                       path.stat().st_mtime, str(path)))
        return out

    def list_sessions(self) -> list[SessionInfo]:
        found = {s.id: s for s in self._old_sessions()}
        found.update({s.id: s for s in self._new_sessions()})
        if not found and not self.sessions_dir.exists() and not self.workspace_dir.exists():
            raise FileNotFoundError(f"no Kiro IDE sessions found ({self.sessions_dir} or {self.workspace_dir})")
        return sorted(found.values(), key=lambda s: s.updated, reverse=True)

    # ---- loading one session ----

    def load(self, session_id: str) -> Session:
        info = self.resolve(session_id)
        path = Path(info.source_path)
        return Session(info, self._load_new(path) if path.suffix == ".jsonl" else self._load_old(path))

    def _load_new(self, path: Path) -> list[Message]:
        msgs: list[Message] = []
        for rec in _jsonl(path):
            pl = rec.get("payload") or {}
            kind = pl.get("type")
            if kind == "user" and isinstance(pl.get("content"), str):
                if pl["content"].strip() and not _internal(pl["content"]):
                    msgs.append(Message("user", "text", pl["content"]))
            elif kind == "assistant" and pl.get("operationType") == "Say":  # "Reasoning" is the model's private notes
                if str(pl.get("content", "")).strip():
                    msgs.append(Message("assistant", "text", pl["content"]))
            elif kind == "tool_call":
                args = {k: v for k, v in (pl.get("args") or {}).items() if k != "toolName"}
                name = pl.get("toolName") or (pl.get("args") or {}).get("toolName") or "tool"
                msgs.append(Message("assistant", "tool_call", f"{name}({short(json.dumps(args), 300)})",
                                    {"name": name, "args": args}))
            elif kind == "tool_result":
                msgs.append(Message("tool", "tool_result", str(pl.get("content", ""))))
        return msgs

    def _load_old(self, path: Path) -> list[Message]:
        data = _json(path)
        msgs: list[Message] = []
        for turn in (data or {}).get("history", []):
            m = turn.get("message") or {}
            content = m.get("content")
            if isinstance(content, list):  # user turns are blocks; pasted logs arrive split one line per block
                content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            if not isinstance(content, str) or not content.strip():
                continue
            role = "assistant" if m.get("role") == "assistant" else "user"
            if role == "user" and _internal(content):
                continue
            msgs.append(Message(role, "text", content))
        return msgs
