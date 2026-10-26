from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from ..model import Message, Session, SessionInfo, short
from .base import Adapter

_NAME_PLACEHOLDERS = {"", "New Agent"}  # Cursor's name for a chat that has not been titled yet
_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)


def _varint(data: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, i


def _blob_refs(data: bytes) -> list[bytes]:
    """32-byte fields of a protobuf node, in order: candidate ids of child blobs."""
    refs, i = [], 0
    try:
        while i < len(data):
            key, i = _varint(data, i)
            wire = key & 7
            if wire == 2:
                size, i = _varint(data, i)
                if size == 32:
                    refs.append(data[i:i + 32])
                i += size
            elif wire == 0:
                _, i = _varint(data, i)
            elif wire == 1:
                i += 8
            elif wire == 5:
                i += 4
            else:
                break  # not protobuf
    except IndexError:
        pass
    return refs


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _store_meta(store: Path) -> dict:
    """The chat's own metadata: a hex-encoded JSON value under key '0'."""
    with closing(_ro(store)) as db:
        row = db.execute("select value from meta limit 1").fetchone()
    return json.loads(bytes.fromhex(row[0]).decode()) if row else {}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text")


class CursorAdapter(Adapter):
    """Reads Cursor CLI chats (~/.cursor/chats/<md5 of folder>/<chat id>/).

    Each chat is a SQLite store of content-addressed blobs: messages are JSON, and protobuf nodes link
    them. The graph is walked from the root blob named in the chat's metadata. Cursor has no importable
    format that baton can safely write, so as a target it gets the handoff file and a launch command.
    """

    name = "cursor"
    cli = "cursor-agent"

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".cursor" / "chats"

    def _chat_dirs(self):
        return [p.parent for p in self.root.glob("*/*/meta.json")]

    # ---- listing (metadata only) ----

    def _title(self, chat: Path) -> str:
        try:
            name = _store_meta(chat / "store.db").get("name", "")
        except (sqlite3.Error, ValueError, OSError):
            name = ""
        if name in _NAME_PLACEHOLDERS:  # untitled: the first prompt the user saved is the best label
            try:
                prompts = json.loads((chat / "prompt_history.json").read_text(encoding="utf-8"))
                name = prompts[0] if prompts and isinstance(prompts[0], str) else ""
            except (OSError, ValueError):
                name = ""
        if not name:  # last resort: the first thing the user asked, read from the chat itself
            try:
                name = next((m.text for m in self._messages(chat / "store.db") if m.role == "user"), "")
            except (sqlite3.Error, ValueError, OSError):
                name = ""
        return short(name, 80)

    def list_sessions(self) -> list[SessionInfo]:
        out = []
        for chat in self._chat_dirs():
            try:
                meta = json.loads((chat / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not meta.get("hasConversation") or not (chat / "store.db").exists():
                continue
            updated = (meta.get("updatedAtMs") or meta.get("createdAtMs") or 0) / 1000
            out.append(SessionInfo(self.name, chat.name, meta.get("cwd", ""), self._title(chat), updated))
        return sorted(out, key=lambda s: s.updated, reverse=True)

    # ---- loading one session ----

    def load(self, session_id: str) -> Session:
        info = self.resolve(session_id)
        chat = next(c for c in self._chat_dirs() if c.name == info.id)
        return Session(info, self._messages(chat / "store.db"))

    def _walk(self, blobs: dict[str, bytes], root: str) -> list[dict]:
        """JSON message blobs reachable from the root, in graph order, each once."""
        found, seen, stack = [], set(), [root]
        while stack:
            h = stack.pop()
            if h in seen or h not in blobs:
                continue
            seen.add(h)
            try:
                msg = json.loads(blobs[h].decode())
            except (ValueError, UnicodeDecodeError):
                msg = None
            if isinstance(msg, dict):
                found.append(msg)
                continue
            stack.extend(r.hex() for r in reversed(_blob_refs(blobs[h])))  # reversed: keep field order
        return found

    def _messages(self, store: Path) -> list[Message]:
        with closing(_ro(store)) as db:
            blobs = dict(db.execute("select id, data from blobs"))
        root = _store_meta(store).get("latestRootBlobId", "")
        msgs: list[Message] = []
        for m in self._walk(blobs, root):
            role, content = m.get("role"), m.get("content")
            if role == "user":
                text = _text(content)
                queries = _USER_QUERY.findall(text)
                if queries:  # the user's own words live inside <user_query>; the rest is injected context
                    msgs.append(Message("user", "text", "\n\n".join(q for q in queries if q)))
                elif text.strip() and not text.lstrip().startswith("<"):
                    msgs.append(Message("user", "text", text))
            elif role == "assistant":
                if isinstance(content, str):
                    if content.strip():
                        msgs.append(Message("assistant", "text", content))
                    continue
                for p in content or []:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "text" and str(p.get("text", "")).strip():
                        msgs.append(Message("assistant", "text", p["text"]))
                    elif p.get("type") == "tool-call":
                        args = p.get("args") if isinstance(p.get("args"), dict) else {}
                        msgs.append(Message("assistant", "tool_call", f"{p.get('toolName')}({short(json.dumps(args), 300)})",
                                            {"name": p.get("toolName"), "args": args}))
                    # reasoning parts carry no text worth keeping
            elif role == "tool":
                for p in content or []:
                    if isinstance(p, dict) and p.get("type") == "tool-result":
                        r = p.get("result")
                        msgs.append(Message("tool", "tool_result", r if isinstance(r, str) else json.dumps(r)))
        return msgs
