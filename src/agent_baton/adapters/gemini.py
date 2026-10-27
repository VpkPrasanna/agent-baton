from __future__ import annotations

import hashlib
import json
import os
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..model import Message, Session, SessionInfo, is_injected, is_placeholder, short
from ..redact import redact
from ..turns import flatten_turns
from .base import Adapter


def project_hash(folder: str | Path) -> str:
    """Gemini names a project by the SHA-256 of its folder path."""
    return hashlib.sha256(str(folder).encode()).hexdigest()


def _content_text(content) -> str:
    """Message content is a string, or a list of parts such as {"text": "..."}."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p if isinstance(p, str) else str(p.get("text", ""))
                         for p in content if isinstance(p, (str, dict)) and (isinstance(p, str) or "text" in p))
    return ""


def _result_text(result) -> str:
    """A tool result is a string, or a list of parts such as {"functionResponse": {"response": {"output": ...}}}."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    out = []
    for item in result if isinstance(result, list) else [result]:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            response = (item.get("functionResponse") or {}).get("response") if isinstance(item.get("functionResponse"), dict) else None
            if isinstance(response, dict):
                out.append(str(response.get("output") or response.get("error") or json.dumps(response)))
            elif "text" in item:
                out.append(str(item["text"]))
            else:
                out.append(json.dumps(item))
        else:
            out.append(str(item))
    return "\n".join(out)


# ---- replaying a session file, following Gemini's own rules ----

def _apply_patch(msgs: dict, patch: dict) -> None:
    def apply(p: dict) -> None:
        m = msgs.get(p.get("id"))
        if m is None:
            return
        if p.get("content") is not None:
            m["content"] = p["content"]
        if isinstance(p.get("toolCalls"), list) and m.get("type") == "gemini" and m.get("toolCalls"):
            for tp in p["toolCalls"]:
                tc = next((t for t in m["toolCalls"] if isinstance(t, dict) and t.get("id") == tp.get("id")), None)
                if tc is not None and "result" in tp:
                    tc["result"] = tp["result"]

    if isinstance(patch.get("id"), str):
        apply(patch)
    for update in patch.get("updates") or []:
        if isinstance(update, dict) and isinstance(update.get("id"), str):
            apply(update)
    for removed in patch.get("removeIds") or []:
        if isinstance(removed, str):
            msgs.pop(removed, None)
    if isinstance(patch.get("orderIds"), list):
        order = [i for i in patch["orderIds"] if isinstance(i, str) and i in msgs]
        wanted = set(order)
        kept = {i: m for i, m in msgs.items() if i not in wanted}  # messages not named keep their place, in front
        moved = {i: msgs[i] for i in order}
        msgs.clear()
        msgs.update(kept)
        msgs.update(moved)


def replay(lines) -> tuple[dict, list[dict]]:
    """Session metadata and messages after applying every record, as Gemini's own loader does.

    Lines are JSON objects of four kinds: a message (it has a string `id`), `{"$patch": ...}` which updates,
    removes or reorders messages, `{"$set": ...}` which updates metadata, and `{"$rewindTo": id}` which drops
    that message and everything after it (everything, if the id is unknown). Any other object is metadata.
    """
    meta: dict = {}
    msgs: dict[str, dict] = {}
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if isinstance(rec.get("$rewindTo"), str):
            ids = list(msgs)
            if rec["$rewindTo"] in msgs:
                for i in ids[ids.index(rec["$rewindTo"]):]:
                    del msgs[i]
            else:
                msgs.clear()
        elif isinstance(rec.get("$patch"), dict):
            _apply_patch(msgs, rec["$patch"])
        elif isinstance(rec.get("$set"), dict):
            meta.update(rec["$set"])
        elif isinstance(rec.get("id"), str):
            msgs[rec["id"]] = rec  # an id seen again replaces the message in place
        else:
            meta.update(rec)
    return meta, list(msgs.values())


def read_session_file(path: Path) -> tuple[dict, list[dict]]:
    """Metadata and messages from a session file: JSONL (current), or one JSON object with `messages` (older)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix != ".json":
        meta, msgs = replay(text.splitlines())
        if msgs or meta:
            return meta, msgs
    try:
        data = json.loads(text)
    except ValueError:
        return {}, []
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return {k: v for k, v in data.items() if k != "messages"}, [m for m in data["messages"] if isinstance(m, dict)]
    return {}, []


def to_messages(raw: list[dict]) -> list[Message]:
    out: list[Message] = []
    for m in raw:
        kind = m.get("type")
        if kind == "user":
            text = _content_text(m.get("content")).strip()
            if text and not is_injected(text) and not is_placeholder(text):
                out.append(Message("user", "text", text))
        elif kind == "gemini":
            text = _content_text(m.get("content")).strip()
            if text:
                out.append(Message("assistant", "text", text))
            for tc in m.get("toolCalls") or []:
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name") or tc.get("displayName") or "tool"
                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                out.append(Message("assistant", "tool_call", f"{name}({short(json.dumps(args), 300)})",
                                   {"name": name, "args": args}))
                result = _result_text(tc.get("result"))
                if result:
                    out.append(Message("tool", "tool_result", result))
        # "info", "error" and "warning" messages and the model's thoughts carry nothing to hand over
    return out


class GeminiAdapter(Adapter):
    """Reads Gemini CLI sessions, and imports a handoff through Gemini's own --session-file flag.

    Sessions live in ~/.gemini/tmp/<project>/chats/session-<time>-<id>.jsonl, one record per line (older
    versions wrote one JSON file). <project> is the project folder's name (older versions used a hash of
    its path), and each project folder holds a .project_root file naming the folder. As a target, baton writes a JSON session file and prints
    `gemini --session-file <file>`: Gemini then creates a new session from it itself, so baton never writes
    into Gemini's own store.
    """

    name = "gemini"
    cli = "gemini"
    session_file = True
    handoff_suffix = ".gemini.json"

    def __init__(self, root: Path | None = None):
        self.root = root or Path.home() / ".gemini" / "tmp"
        self._known: dict[str, str] | None = None

    # ---- listing (metadata only) ----

    def _files(self) -> list[Path]:
        return [p for pattern in ("session-*.jsonl", "session-*.json") for p in self.root.glob(f"*/chats/{pattern}")]

    def _folders(self) -> dict[str, str]:
        """project hash -> folder, for the folders baton can name: the current one and its parents."""
        if self._known is None:
            here = Path(os.path.realpath(os.getcwd()))
            self._known = {project_hash(p): str(p) for p in (here, *here.parents)}
        return self._known

    def _cwd_for(self, meta: dict, session_file: Path | None = None) -> str:
        if session_file is not None:  # Gemini records each project's folder in <project>/.project_root
            try:
                root = (session_file.parent.parent / ".project_root").read_text(encoding="utf-8").strip()
            except OSError:
                root = ""
            if root:
                return root
        for d in meta.get("directories") or []:  # recorded workspace folders, trusted only if they match the hash
            if isinstance(d, str) and project_hash(d) == meta.get("projectHash"):
                return d
        return self._folders().get(meta.get("projectHash", ""), "")

    def list_sessions(self) -> list[SessionInfo]:
        out = []
        for p in self._files():
            try:
                meta, raw = read_session_file(p)
            except OSError:
                continue
            if meta.get("kind") == "subagent" or not raw:
                continue
            first = next((m.text for m in to_messages(raw) if m.role == "user"), "")
            sid = meta.get("sessionId") or p.stem.rsplit("-", 1)[-1]
            title = short(str(meta.get("summary") or first), 80)
            out.append(SessionInfo(self.name, sid, self._cwd_for(meta, p), title, p.stat().st_mtime, str(p)))
        return sorted(out, key=lambda s: s.updated, reverse=True)

    # ---- loading one session ----

    def load(self, session_id: str) -> Session:
        info = self.resolve(session_id)
        _, raw = read_session_file(Path(info.source_path))
        return Session(info, to_messages(raw))

    # ---- as a target ----

    def render_session_file(self, session: Session, cwd: Path) -> str:
        """The JSON file `gemini --session-file` imports: user and gemini messages, redacted."""
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        title = short(redact(f"Moved from {session.info.agent}: {session.info.title}".strip(": ")), 80)
        messages = [
            {"id": f"baton-{n}", "timestamp": now, "type": "user" if role == "user" else "gemini", "content": redact(text)}
            for n, (role, text) in enumerate(flatten_turns(session.messages), 1)
        ]
        return json.dumps({
            "sessionId": str(uuid.uuid4()), "projectHash": project_hash(os.path.realpath(cwd)),
            "startTime": now, "lastUpdated": now, "summary": title, "messages": messages,
        }, indent=2)

    def import_command(self, relpath: str) -> str:
        return f"gemini --session-file {shlex.quote(relpath)}"
