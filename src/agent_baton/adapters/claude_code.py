from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..model import Message, Session, SessionInfo, is_injected, short
from ..redact import redact
from ..turns import flatten_turns
from .base import Adapter, Native


def _jsonl(path: Path):
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


class ClaudeCodeAdapter(Adapter):
    name = "claude"
    cli = "claude"
    native = True

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".claude" / "projects"

    def _files(self):
        return self.root.glob("*/*.jsonl")

    def list_sessions(self) -> list[SessionInfo]:
        out = []
        for p in self._files():
            cwd, title, first = "", "", ""
            for n, d in enumerate(_jsonl(p)):
                cwd = cwd or d.get("cwd", "")
                if d.get("type") == "custom-title":
                    title = d.get("customTitle", "")
                if d.get("type") == "user" and not d.get("isSidechain") and not first:
                    t = _text_of(d.get("message", {}).get("content", ""))
                    if t and not is_injected(t):
                        first = t
                if n > 200 or (cwd and first and title):
                    break
            if first or title:
                out.append(SessionInfo(self.name, p.stem, cwd, short(title or first, 80), p.stat().st_mtime, str(p)))
        return sorted(out, key=lambda s: s.updated, reverse=True)

    def load(self, session_id: str) -> Session:
        info = self.resolve(session_id)
        path = next(p for p in self._files() if p.stem == info.id)
        msgs: list[Message] = []
        for d in _jsonl(path):
            if d.get("isSidechain") or d.get("isMeta"):
                continue
            content = d.get("message", {}).get("content", "")
            answers = (d.get("toolUseResult") or {}).get("answers") if isinstance(d.get("toolUseResult"), dict) else None
            if d.get("type") == "user" and isinstance(answers, dict) and answers:
                # AskUserQuestion: the user's choice is a decision, not tool output, so keep it as a user message
                lines = [f"- {q} → {', '.join(a) if isinstance(a, list) else a}" for q, a in answers.items()]
                msgs.append(Message("user", "text", "(answered a multiple-choice prompt)\n" + "\n".join(lines),
                                    {"answer": True}))
                continue
            if d.get("type") == "user":
                if isinstance(content, str):
                    if content and not is_injected(content):
                        msgs.append(Message("user", "text", content))
                    continue
                for b in content:
                    if b.get("type") == "text" and not is_injected(b.get("text", "")):
                        msgs.append(Message("user", "text", b["text"]))
                    elif b.get("type") == "tool_result":
                        msgs.append(Message("tool", "tool_result", _text_of(b.get("content", "")) or str(b.get("content", ""))))
            elif d.get("type") == "assistant" and isinstance(content, list):
                for b in content:
                    if b.get("type") == "text" and b.get("text", "").strip():
                        msgs.append(Message("assistant", "text", b["text"]))
                    elif b.get("type") == "tool_use":
                        args = b.get("input", {})
                        msgs.append(Message("assistant", "tool_call", f"{b.get('name')}({short(json.dumps(args), 300)})",
                                            {"name": b.get("name"), "args": args}))
        return Session(info, msgs)

    def write_native(self, session: Session, cwd: Path):
        """Best-effort: rebuild the conversation as plain user/assistant turns.

        Tool calls and results are flattened to text so no orphaned tool_use blocks exist.
        """
        sid = str(uuid.uuid4())
        folder = self.root / str(cwd).replace("/", "-").replace(".", "-")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{sid}.jsonl"
        parent, lines, buf = None, [], []

        def emit(role: str, text: str):
            nonlocal parent
            uid = str(uuid.uuid4())
            ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            body = text if role == "user" else [{"type": "text", "text": text}]
            lines.append(json.dumps({
                "type": role, "uuid": uid, "parentUuid": parent, "sessionId": sid, "cwd": str(cwd),
                "isSidechain": False, "userType": "external", "timestamp": ts, "version": "baton",
                "message": {"role": role, "content": body},
            }))
            parent = uid

        for role, text in flatten_turns(session.messages):
            emit(role, redact(text))
        title = redact(f"Moved from {session.info.agent}: {session.info.title}".strip(": "))
        lines.insert(0, json.dumps({"type": "custom-title", "customTitle": short(title, 80), "sessionId": sid}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)  # same as Claude's own session files
        return Native(sid, f"claude --resume {sid}", path)
