from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from ..codex_rpc import AppServer, CodexRpcError
from ..model import Message, Session, SessionInfo, is_injected, short
from ..redact import redact
from ..turns import flatten_turns
from .base import Adapter, Native, NativeError
from .claude_code import _jsonl


def _parts(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if isinstance(p, dict))


def _latest_db(home: Path, stem: str) -> Path | None:
    """Newest versioned database, e.g. state_5.sqlite beats state_4.sqlite."""
    found = list(home.glob(f"{stem}_*.sqlite"))
    return max(found, key=lambda p: int((re.search(r"_(\d+)\.sqlite$", p.name) or [0, 0])[1]), default=None)


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


class CodexAdapter(Adapter):
    """Reads Codex rollout files, and falls back to the SQLite history for sessions whose
    rollout file was deleted (state_*.sqlite lists threads, thread_history_*.sqlite has the items)."""

    name = "codex"
    cli = "codex"
    native = True
    import_sources = frozenset({"claude"})  # sessions Codex's own importer can bring in
    import_timeout = 120.0  # seconds to wait for the importer to finish

    def __init__(self, root: Path | None = None, home: Path | None = None, server=None):
        self.home = home or (root.parent if root else Path.home() / ".codex")
        self.root = root or self.home / "sessions"
        self.server = server or AppServer  # factory returning an AppServer context manager

    def import_native(self, info: SessionInfo, cwd: Path) -> Native | None:
        """Import one Claude Code session with Codex's official importer (externalAgentConfig/detect and
        /import), selecting only that session. Raises NativeError if Codex does not list or import it,
        for example when the session's folder no longer exists or it was imported before.
        """
        if info.agent not in self.import_sources or not info.source_path:
            return None
        wanted = str(Path(info.source_path).resolve())
        detect = {"includeHome": True, "maxSessions": 500, "maxSessionAgeDays": 3650}
        if info.cwd and Path(info.cwd).is_dir():
            detect["cwds"] = [info.cwd]
        try:
            with self.server() as srv:
                found = srv.call("externalAgentConfig/detect", detect)
                item, match = None, None
                for it in found.get("items", []):
                    if it.get("itemType") != "SESSIONS":
                        continue
                    hit = next((s for s in (it.get("details") or {}).get("sessions") or []
                                if str(Path(s.get("path", "")).resolve()) == wanted), None)
                    if hit:
                        item, match = it, hit
                        break
                if not match:
                    raise NativeError("Codex's importer did not list this session (its folder may no longer "
                                      "exist, or it was imported before)")
                selected = {**item, "details": {"sessions": [match]}}  # only this session, nothing else
                started = srv.call("externalAgentConfig/import", {"migrationItems": [selected], "source": "baton"})
                import_id = started.get("importId")
                done = srv.wait_for("externalAgentConfig/import/completed",
                                    lambda p: p.get("importId") == import_id, timeout=self.import_timeout)
        except (CodexRpcError, KeyError, TypeError) as e:
            raise NativeError(f"Codex's importer failed: {e}")
        for res in done.get("itemTypeResults", []):
            if res.get("itemType") != "SESSIONS":
                continue
            ok = next((s for s in res.get("successes", []) if str(Path(s.get("source", "")).resolve()) == wanted), None)
            if ok and ok.get("target"):
                return Native(ok["target"], f"codex resume {ok['target']}",
                              note="imported with Codex's official importer: tool calls appear as tagged text")
            if res.get("failures"):
                raise NativeError(f"Codex's importer reported a failure: {json.dumps(res['failures'])[:200]}")
        raise NativeError("Codex's importer finished without importing this session")

    def write_native(self, session: Session, cwd: Path) -> Native:
        """Create a Codex thread through `codex app-server`, the supported way in: thread/start,
        thread/inject_items (history), thread/name/set. Writes nothing into Codex's files directly.

        Codex hides threads with no user turn from its resume picker, so the thread is resumed by id.
        """
        items = [
            {"type": "message", "role": role,
             "content": [{"type": "input_text" if role == "user" else "output_text", "text": redact(text)}]}
            for role, text in flatten_turns(session.messages)
        ]
        title = short(redact(f"Moved from {session.info.agent}: {session.info.title}".strip(": ")), 80)
        try:
            with self.server() as srv:
                tid = srv.call("thread/start", {"cwd": str(cwd), "ephemeral": False})["thread"]["id"]
                srv.call("thread/inject_items", {"threadId": tid, "items": items})
                srv.call("thread/name/set", {"threadId": tid, "name": title})
        except (CodexRpcError, KeyError, TypeError) as e:
            raise NativeError(f"could not create a Codex session: {e}")
        return Native(tid, f"codex resume {tid}",
                      note="Codex's resume picker only lists threads that have a user turn: resume by id")

    def _files(self):
        return self.root.rglob("rollout-*.jsonl")

    # ---- listing (metadata only) ----

    def _rollout_sessions(self) -> list[SessionInfo]:
        out = []
        for p in self._files():
            sid, cwd, first = "", "", ""
            for n, d in enumerate(_jsonl(p)):
                pl = d.get("payload") or {}
                if d.get("type") == "session_meta":
                    sid, cwd = pl.get("id", ""), pl.get("cwd", "")
                elif (d.get("type") == "response_item" and pl.get("type") == "message" and pl.get("role") == "user") \
                        or (d.get("type") == "event_msg" and pl.get("type") == "user_message"):
                    t = _parts(pl.get("content")) if "content" in pl else str(pl.get("message") or "")
                    if t and not is_injected(t) and not first:
                        first = t
                if n > 80 or (sid and first):
                    break
            if sid:
                out.append(SessionInfo(self.name, sid, cwd, short(first, 80), p.stat().st_mtime))
        return out

    def _history_dbs(self) -> tuple[Path, Path] | None:
        state, history = _latest_db(self.home, "state"), _latest_db(self.home, "thread_history")
        return (state, history) if state and history else None

    def _db_sessions(self, skip: set[str]) -> list[SessionInfo]:
        dbs = self._history_dbs()
        if not dbs:
            return []
        state, history = dbs
        with closing(_ro(history)) as h:
            with_items = {r[0] for r in h.execute("select distinct thread_id from thread_items")}
        with closing(_ro(state)) as s:
            rows = s.execute("select id, cwd, title, first_user_message, updated_at from threads").fetchall()
        return [
            SessionInfo(self.name, tid, cwd or "", short(title or first or "", 80), ts if ts < 1e11 else ts / 1000)
            for tid, cwd, title, first, ts in rows if tid in with_items and tid not in skip
        ]

    def list_sessions(self) -> list[SessionInfo]:
        rollouts = self._rollout_sessions()
        found = rollouts + self._db_sessions({s.id for s in rollouts})  # a rollout file beats the SQLite copy
        return sorted(found, key=lambda s: s.updated, reverse=True)

    # ---- loading one session ----

    def load(self, session_id: str) -> Session:
        info = self.resolve(session_id)
        path = next((p for p in self._files() if info.id in p.name), None)
        return Session(info, self._load_rollout(path) if path else self._load_db(info.id))

    def _load_db(self, thread_id: str) -> list[Message]:
        _, history = self._history_dbs()
        with closing(_ro(history)) as h:
            rows = h.execute("select item_type, item_json from thread_items where thread_id = ? order by rollout_ordinal",
                             (thread_id,)).fetchall()
        msgs: list[Message] = []
        for kind, raw in rows:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if kind == "userMessage":
                t = _parts(d.get("content"))
                if t.strip() and not is_injected(t):
                    msgs.append(Message("user", "text", t))
            elif kind == "agentMessage" and str(d.get("text") or "").strip():
                msgs.append(Message("assistant", "text", d["text"]))
            elif kind == "commandExecution":
                args = {"command": d.get("command", ""), "cwd": d.get("cwd", "")}
                msgs.append(Message("assistant", "tool_call", f"commandExecution({short(json.dumps({'command': args['command']}), 300)})",
                                    {"name": "commandExecution", "args": args}))
                msgs.append(Message("tool", "tool_result", str(d.get("aggregatedOutput") or f"exit {d.get('exitCode')}")))
            elif kind == "fileChange":
                for ch in d.get("changes") or []:
                    args = {"path": ch.get("path", "")}
                    msgs.append(Message("assistant", "tool_call", f"fileChange({short(json.dumps(args), 300)})",
                                        {"name": "fileChange", "args": args}))
                msgs.append(Message("tool", "tool_result", f"file change {d.get('status', '')}"))
            # reasoning items carry no text worth keeping
        return msgs

    def _load_rollout(self, path: Path) -> list[Message]:
        msgs: list[Message] = []
        events: list[Message] = []  # fallback for sessions that only log event_msg text
        for d in _jsonl(path):
            if d.get("type") == "event_msg":
                pl = d.get("payload") or {}
                role = {"user_message": "user", "agent_message": "assistant"}.get(pl.get("type"))
                text = pl.get("message") or pl.get("text") or ""
                if role and isinstance(text, str) and text.strip() and not is_injected(text):
                    events.append(Message(role, "text", text))
            if d.get("type") != "response_item":
                continue
            pl = d.get("payload") or {}
            kind = pl.get("type")
            if kind == "message" and pl.get("role") in ("user", "assistant"):
                t = _parts(pl.get("content"))
                if t.strip() and not (pl["role"] == "user" and is_injected(t)):
                    msgs.append(Message(pl["role"], "text", t))
            elif kind == "function_call":
                try:
                    args = json.loads(pl.get("arguments") or "{}")
                except ValueError:
                    args = {"raw": pl.get("arguments")}
                msgs.append(Message("assistant", "tool_call", f"{pl.get('name')}({short(json.dumps(args), 300)})",
                                    {"name": pl.get("name"), "args": args}))
            elif kind == "function_call_output":
                msgs.append(Message("tool", "tool_result", _parts(pl.get("output"))))
        if not any(m.kind == "text" for m in msgs):
            msgs = events + [m for m in msgs if m.kind != "text"]
        return msgs
