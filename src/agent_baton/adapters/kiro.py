from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ..model import Message, Session, SessionInfo, short
from .base import Adapter
from .claude_code import _jsonl


LIST_TIMEOUT = 30  # Kiro's own listing takes a few seconds


def _run_kiro_list() -> str:
    """stdout of Kiro's official listing. Raises on any failure so the caller can fall back to reading files."""
    proc = subprocess.run(["kiro-cli", "chat", "--list-sessions", "--all-cwds", "--format", "json"],
                          capture_output=True, text=True, timeout=LIST_TIMEOUT, check=True)
    return proc.stdout


def _parse_listing(raw: str) -> list:
    """The listing is a JSON array of {cwd, sessions, complete}; tolerate a banner line before it."""
    try:
        data = json.loads(raw)
    except ValueError:
        data = json.loads(raw[raw.index("["):])  # ValueError again if there is no JSON at all
    if not isinstance(data, list):
        raise ValueError("unexpected listing shape")
    return data


def default_db() -> Path:
    mac = Path.home() / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3"
    return mac if sys.platform == "darwin" or mac.exists() else Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3"


def default_sessions_dir() -> Path:
    return Path.home() / ".kiro" / "sessions" / "cli"


def _strings(x) -> str:
    """Pull readable text out of Kiro's tagged-union content values."""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return "\n".join(filter(None, (_strings(i) for i in x)))
    if isinstance(x, dict):
        if "kind" in x and "data" in x:  # v2 tagged block: {"kind": "text", "data": ...}
            return _strings(x["data"])
        return "\n".join(filter(None, (_strings(v) for v in x.values())))
    return ""


def _epoch(iso: str, fallback: float) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return fallback


def _call(name, args) -> Message:
    return Message("assistant", "tool_call", f"{name}({short(json.dumps(args), 300)})", {"name": name, "args": args})


class KiroAdapter(Adapter):
    """Reads Kiro CLI sessions.

    Listing asks Kiro itself first (`kiro-cli chat --list-sessions --all-cwds --format json`) and also reads
    the files, so a session is found even if one source misses it. Content always comes from the files:
    v2 (~/.kiro/sessions/cli/*.jsonl) or the older v1 SQLite store, because Kiro has no non-interactive,
    documented way to export a session.
    """

    name = "kiro"
    cli = "kiro-cli chat"

    def __init__(self, db: Path | None = None, sessions_dir: Path | None = None, include_subagents: bool = True,
                 runner=None, use_kiro_cli: bool = True):
        custom_paths = db is not None or sessions_dir is not None
        self.db = db or default_db()
        self.sessions_dir = sessions_dir or default_sessions_dir()
        self.include_subagents = include_subagents
        self.hidden_subagents = 0  # set by list_sessions so the CLI can hint
        self.listing_source = "files"  # "kiro-cli" when Kiro's own listing answered
        # Kiro's own listing reflects the real home folder, so it is skipped when custom paths are given
        # use_kiro_cli=False skips Kiro's own listing, which takes a few seconds, and reads the files only
        self._runner = runner if runner is not None else (None if custom_paths or not use_kiro_cli else _run_kiro_list)
        self._cache: list[SessionInfo] | None = None

    # ---- listing (metadata only) ----

    def _sidecar(self, session_id: str) -> dict:
        try:
            return json.loads((self.sessions_dir / f"{session_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _v2_sessions(self) -> list[SessionInfo]:
        out = []
        for meta_path in self.sessions_dir.glob("*.json"):
            jsonl = meta_path.with_suffix(".jsonl")
            if not jsonl.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            out.append(SessionInfo(
                self.name, meta.get("session_id") or meta_path.stem, meta.get("cwd", ""),
                short(meta.get("title") or "", 80), _epoch(meta.get("updated_at", ""), jsonl.stat().st_mtime),
                subagent=meta.get("session_created_reason") == "subagent",
            ))
        return out

    def _v1_sessions(self) -> list[SessionInfo]:
        if not self.db.exists():
            return []
        # json_extract keeps the transcript inside SQLite: only the first prompt reaches Python.
        sql = (
            "select conversation_id, key, updated_at,"
            " json_extract(value, '$.history[0].user.content.Prompt.prompt')"
            " from conversations_v2"
        )
        with closing(sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)) as db:
            rows = db.execute(sql).fetchall()
        return [SessionInfo(self.name, cid, cwd, short(first or "", 80), ts / 1000) for cid, cwd, ts, first in rows]

    def _official_sessions(self) -> list[SessionInfo] | None:
        """Sessions as Kiro itself lists them, or None when its listing is unavailable."""
        if self._runner is None:
            return None
        try:
            data = _parse_listing(self._runner())
        except (OSError, subprocess.SubprocessError, ValueError):
            return None  # kiro-cli missing, failed, timed out, or printed something unexpected
        out = []
        for env in data:
            for s in (env.get("sessions") or []) if isinstance(env, dict) else []:
                sid = s.get("sessionId")
                if not sid:
                    continue
                side = self._sidecar(sid)  # Kiro's listing does not say a session is a subagent run: the file does
                title = s.get("title") or ""
                title = side.get("title", "") if title in ("", "(no title)") else title
                out.append(SessionInfo(self.name, sid, env.get("cwd", ""), short(title or "", 80),
                                       _epoch(s.get("updatedAt", ""), 0.0), "",
                                       side.get("session_created_reason") == "subagent"))
        return out

    def list_sessions(self) -> list[SessionInfo]:
        if self._cache is not None:
            return list(self._cache)
        official = self._official_sessions()
        if official is None and not self.sessions_dir.exists() and not self.db.exists():
            raise FileNotFoundError(f"no Kiro CLI sessions found ({self.sessions_dir} or {self.db})")
        found = {s.id: s for s in self._v1_sessions()}
        found.update({s.id: s for s in self._v2_sessions()})  # v2 wins on id clash
        if official is not None:
            self.listing_source = "kiro-cli"
            found.update({s.id: s for s in official})  # Kiro's own answer wins; files fill any gaps
        self.hidden_subagents = 0
        if not self.include_subagents:
            self.hidden_subagents = sum(s.subagent for s in found.values())
            found = {i: s for i, s in found.items() if not s.subagent}
        self._cache = sorted(found.values(), key=lambda s: s.updated, reverse=True)
        return list(self._cache)

    # ---- loading one session ----

    def load(self, session_id: str) -> Session:
        info = self.resolve(session_id)
        path = self.sessions_dir / f"{info.id}.jsonl"
        if path.exists():
            return Session(info, self._load_v2(path))
        msgs = self._load_v1(info.id) if self.db.exists() else None
        if msgs is None:
            raise FileNotFoundError(f"Kiro lists session {info.id[:12]} but baton found no readable local copy "
                                    f"of it (looked in {self.sessions_dir} and {self.db})")
        return Session(info, msgs)

    def _load_v2(self, path: Path) -> list[Message]:
        msgs: list[Message] = []
        for d in _jsonl(path):
            kind, blocks = d.get("kind"), (d.get("data") or {}).get("content") or []
            if kind == "Prompt":
                for b in blocks:
                    if b.get("kind") == "text" and str(b.get("data", "")).strip():
                        msgs.append(Message("user", "text", b["data"]))
            elif kind == "AssistantMessage":
                for b in blocks:
                    if b.get("kind") == "text" and str(b.get("data", "")).strip():
                        msgs.append(Message("assistant", "text", b["data"]))
                    elif b.get("kind") == "toolUse":
                        tu = b.get("data") or {}
                        msgs.append(_call(tu.get("name"), tu.get("input", {})))
            elif kind == "ToolResults":
                for b in blocks:
                    if b.get("kind") == "toolResult":
                        msgs.append(Message("tool", "tool_result", _strings((b.get("data") or {}).get("content"))))
        return msgs

    def _load_v1(self, conversation_id: str) -> list[Message] | None:
        with closing(sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)) as db:
            row = db.execute("select value from conversations_v2 where conversation_id = ?", (conversation_id,)).fetchone()
        if row is None:
            return None
        raw = row[0]
        msgs: list[Message] = []
        for turn in json.loads(raw).get("history", []):
            content = (turn.get("user") or {}).get("content") or {}
            if "Prompt" in content:
                msgs.append(Message("user", "text", content["Prompt"].get("prompt", "")))
            elif "ToolUseResults" in content:
                for r in content["ToolUseResults"].get("tool_use_results", []):
                    msgs.append(Message("tool", "tool_result", _strings(r.get("content"))))
            asst = turn.get("assistant") or {}
            if "Response" in asst:
                msgs.append(Message("assistant", "text", asst["Response"].get("content", "")))
            elif "ToolUse" in asst:
                tu = asst["ToolUse"]
                if tu.get("content"):
                    msgs.append(Message("assistant", "text", tu["content"]))
                for call in tu.get("tool_uses", []):
                    msgs.append(_call(call.get("name"), call.get("args", {})))
        return msgs
