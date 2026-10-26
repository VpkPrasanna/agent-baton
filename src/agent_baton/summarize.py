"""Deterministic handoff rendering: a compact brief, or a size-capped transcript."""
from __future__ import annotations

import fnmatch
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .model import Message, Session, clip, is_placeholder, short
from .redact import redact

LAST_ANSWER_CHARS = 6000
OTHER_MESSAGE_CHARS = 800
SHORT_REPLY_CHARS = 80  # a user message shorter than this is a reply that needs context
FULL_MAX_CHARS = 200_000  # about 50k tokens: fits most agents' context with room to work
FULL_TEXT_CHARS = 4000
FULL_TOOL_CHARS = 600
MAX_EDITED, MAX_READ = 25, 15

_PATH_KEYS = ("path", "file_path", "filePath", "filename", "file", "notebook_path", "absolute_path")
_PATH_LISTS = ("paths", "operations", "edits")
_EDIT_TOOLS = {
    "write", "edit", "multiedit", "notebookedit", "apply_patch", "fs_write",
    "str_replace_editor", "str_replace_based_edit_tool", "create", "filechange",  # filechange: Codex SQLite history
    "strreplace", "editnotebook",  # Cursor
    "write_file", "replace",  # Gemini
}
# File names that usually hold secrets: never listed in a handoff.
_SENSITIVE = (".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
              "credentials*", ".netrc", ".npmrc", "*secret*")

def _has_git(cwd: str) -> bool:
    p = Path(cwd)
    return any((d / ".git").exists() for d in (p, *p.parents))


def _footer(session: Session) -> str:
    cwd = session.info.cwd
    if cwd and Path(cwd).is_dir() and _has_git(cwd):
        verify = ("Run `git status` / `git diff` first: earlier edits may be uncommitted and this "
                  "file is a record of the conversation, not proof the code is in the state described.")
    elif cwd and Path(cwd).is_dir():
        verify = ("This folder is not a git repository, so check the state of the files named above directly "
                  "(open them) instead of using git: this file is a record of the conversation, "
                  "not proof the code is in the state described.")
    else:
        verify = ("The original working directory does not exist on this machine: locate the project first, "
                  "then check the state of the files named above directly. This file is a record of the "
                  "conversation, not proof the code is in the state described.")
    return ("## How to continue\n"
            "Work was moved to you from another assistant because its usage ran out. "
            f"{verify} Then continue from the current task above.\n")


def _header(session: Session, mode: str) -> str:
    i = session.info
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(i.updated)) if i.updated else "unknown"
    missing = " (does not exist on this machine; paths below may not resolve)" if i.cwd and not Path(i.cwd).is_dir() else ""
    return (
        f"# Session handoff ({mode}) from {i.agent}\n\n"
        f"- Source session: `{i.id}`\n- Working directory: `{i.cwd}`{missing}\n- Last activity: {when}\n"
    )


def _texts(session: Session) -> list[Message]:
    return [m for m in session.messages if m.kind == "text" and not is_placeholder(m.text)]


def _quote(text: str, limit: int) -> str:
    return "\n".join("> " + line for line in clip(text, limit).splitlines())


# ---- files ----

_NOISE_DIRS = {".venv", "venv", "node_modules", "site-packages", "__pycache__", ".git"}


@dataclass
class FileUse:
    edited: list[str] = field(default_factory=list)  # most recent first
    read: list[str] = field(default_factory=list)
    omitted: int = 0  # secret-looking files left out
    earlier_edited: int = 0  # edited before the current plan started (not listed)
    scoped: bool = False  # lists cover only work since the current plan started


def _paths(args) -> list[str]:
    out: list[str] = []
    if isinstance(args, dict):
        out += [v for k in _PATH_KEYS if isinstance(v := args.get(k), str)]
        for k in _PATH_LISTS:
            for item in args.get(k) or []:
                out += [item] if isinstance(item, str) else _paths(item)
    return out


def _display(path: str, cwd: str) -> str:
    try:
        return str(Path(path).relative_to(cwd)) if cwd else path
    except ValueError:
        return path


def files_touched(session: Session) -> FileUse:
    cwd = session.info.cwd
    start = _scan_plan(session)[2]  # index where the current plan began, or None
    last: dict[str, tuple[bool, int]] = {}  # path -> (ever edited, last-use index); insertion order = recency
    for n, m in enumerate(session.messages):
        if m.kind != "tool_call":
            continue
        edit = (m.meta.get("name") or "").lower() in _EDIT_TOOLS
        for p in _paths(m.meta.get("args")):
            if _NOISE_DIRS & set(Path(p).parts) or Path(p).is_dir():
                continue
            last[p] = (last.pop(p, (False, 0))[0] or edit, n)  # re-insert so order tracks latest use
    use = FileUse(scoped=start is not None)
    for p, (edited, n) in reversed(list(last.items())):
        if any(fnmatch.fnmatch(Path(p).name.lower(), pat) for pat in _SENSITIVE):
            use.omitted += 1
        elif start is not None and n < start:
            use.earlier_edited += edited  # older work: counted, not listed
        else:
            (use.edited if edited else use.read).append(_display(p, cwd))
    return use


def _files_section(session: Session) -> list[str]:
    use = files_touched(session)
    out: list[str] = []
    since = " since the current plan started" if use.scoped else ""
    for title, items, cap in (("Files edited", use.edited, MAX_EDITED), ("Files read", use.read, MAX_READ)):
        if items:
            out.append(f"## {title}{since} (most recent first, from tool calls)")
            out += [f"- `{f}`" for f in items[:cap]]
            if len(items) > cap:
                out.append(f"- … and {len(items) - cap} more")
            out.append("")
    if use.earlier_edited:
        out.append(f"_{use.earlier_edited} file(s) edited earlier in the session are not listed._\n")
    if use.omitted:
        out.append(f"_{use.omitted} secret-looking file(s) (e.g. .env) omitted from these lists._\n")
    if use.edited:
        out.append("_Edits made through shell commands are not tracked here._\n")
    return out


# ---- plan ----

def extract_plan(session: Session) -> tuple[list[tuple[str, bool]], str]:
    """Latest task list (Kiro todo_list, Claude TodoWrite, Codex update_plan) and its last progress note."""
    return _scan_plan(session)[:2]


def _scan_plan(session: Session) -> tuple[list[tuple[str, bool]], str, int | None]:
    """Also returns the message index where the latest Kiro plan was created (None for other agents,
    whose plan tools rewrite the whole list on every call, so there is no clear start)."""
    items: list[list] = []
    note = ""
    start: int | None = None
    for n, m in enumerate(session.messages):
        a = m.meta.get("args")
        if m.kind != "tool_call" or not isinstance(a, dict):
            continue
        name = m.meta.get("name")
        if name == "todo_list":
            if a.get("command") == "create":
                items = [[t.get("task_description") or t.get("details") or "", False]
                         for t in a.get("tasks", []) if isinstance(t, dict)]
                note, start = "", n
            elif a.get("command") == "complete":
                for i in a.get("completed_task_ids", []):
                    if str(i).isdigit() and 1 <= int(i) <= len(items):
                        items[int(i) - 1][1] = True
                note = a.get("context_update") or note
        elif name == "TodoWrite":
            items = [[t.get("content", ""), t.get("status") == "completed"] for t in a.get("todos", []) if isinstance(t, dict)]
        elif name == "update_plan":
            items = [[t.get("step", ""), t.get("status") == "completed"] for t in a.get("plan", []) if isinstance(t, dict)]
        elif name == "write_todos":  # Gemini
            items = [[t.get("description", ""), t.get("status") == "completed"] for t in a.get("todos", []) if isinstance(t, dict)]
    return [(t, d) for t, d in items if t], note, start


def _plan_section(session: Session) -> list[str]:
    items, note = extract_plan(session)
    if not items:
        return []
    done = sum(d for _, d in items)
    out = [f"## Task plan (latest; {done}/{len(items)} done)"]
    out += [f"- [{'x' if d else ' '}] {short(t, 200)}" for t, d in items[:25]]
    if len(items) > 25:
        out.append(f"- … and {len(items) - 25} more")
    if note:
        out.append(f"\nLatest progress note:\n{_quote(note, 1200)}")
    return out + [""]


# ---- current task ----

def _current_task_section(texts: list[Message]) -> list[str]:
    users = [i for i, m in enumerate(texts) if m.role == "user"]
    if not users:
        return []
    idx = users[-1]
    out = ["## Current task (latest user request)", clip(texts[idx].text, 1500), ""]
    if len(texts[idx].text.strip()) < SHORT_REPLY_CHARS:  # "yes do it" means nothing alone
        substantive = next((texts[i] for i in reversed(users[:-1]) if len(texts[i].text.strip()) >= SHORT_REPLY_CHARS), None)
        if substantive:
            out += ["Last substantive user request:", _quote(substantive.text, 800), ""]
        prior = next((m for m in reversed(texts[:idx]) if m.role == "assistant"), None)
        if prior:
            out += ["Short reply: the assistant message it answers:", _quote(prior.text, 2000), ""]
    return out


def _context_sections(session: Session) -> list[str]:
    return _current_task_section(_texts(session)) + _plan_section(session) + _files_section(session)


# ---- renderers ----

def build_brief(session: Session, last_n: int = 8) -> str:
    texts = _texts(session)
    users = [m for m in texts if m.role == "user"]
    tools = Counter(m.meta.get("name", "?") for m in session.messages if m.kind == "tool_call")

    out = [_header(session, "summary")] + _context_sections(session)
    if len(users) > 1:
        out.append("## Session started with\n" + clip(users[0].text, 600) + "\n")
    if len(users) > 2:
        out.append("## Earlier user requests")
        out += [f"- {short(m.text, 300)}" for m in users[-5:-1]]
        out.append("")
    if tools:
        out.append("## Tools used\n" + ", ".join(f"{n} x{c}" for n, c in tools.most_common(10)) + "\n")
    recent = texts[-last_n:]
    last_assistant = max((i for i, m in enumerate(recent) if m.role == "assistant"), default=-1)
    out.append(f"## Last {len(recent)} messages (verbatim; the final assistant answer gets the largest budget)")
    for i, m in enumerate(recent):
        budget = LAST_ANSWER_CHARS if i == last_assistant else OTHER_MESSAGE_CHARS
        out.append(f"**{m.role}:**\n{_quote(m.text, budget)}\n")
    out.append(_footer(session))
    return redact("\n".join(out))


def _render(m: Message) -> str:
    if m.kind == "text":
        return f"\n**{m.role}:**\n{clip(m.text, FULL_TEXT_CHARS)}"
    label = "tool call" if m.kind == "tool_call" else "tool result"
    return f"> {label}: {short(m.text, FULL_TOOL_CHARS)}"


def native_messages(session: Session, max_chars: int = FULL_MAX_CHARS) -> tuple[list[Message], int]:
    """Messages for a native resumable session: the brief first (task, plan, files), then the most
    recent messages that fit in max_chars, with long text clipped. Returns (messages, omitted)."""
    seed = Message("user", "text", build_brief(session))
    pool = [m for m in session.messages if not (m.kind == "text" and is_placeholder(m.text))]
    clipped = [Message(m.role, m.kind, clip(m.text, FULL_TEXT_CHARS) if m.kind == "text" else short(m.text, FULL_TOOL_CHARS), m.meta)
               for m in pool]
    budget, kept = max_chars - len(seed.text), 0
    for m in reversed(clipped):
        if kept and budget - len(m.text) - 12 < 0:
            break
        budget -= len(m.text) + 12  # 12 chars of per-turn framing
        kept += 1
    omitted = len(clipped) - kept
    return [seed] + clipped[omitted:], omitted


def build_transcript(session: Session, max_chars: int = FULL_MAX_CHARS) -> tuple[str, int]:
    """Plan and current task up front, then as much of the recent transcript as fits.

    Returns (markdown, number of earlier messages omitted).
    """
    head = "\n".join([_header(session, "full transcript")] + _context_sections(session) + ["## Transcript"])
    rows = [_render(m) for m in session.messages if not (m.kind == "text" and is_placeholder(m.text))]
    footer = _footer(session)
    budget, kept = max_chars - len(head) - len(footer) - 200, 0
    for row in reversed(rows):
        if kept and budget - len(row) - 1 < 0:
            break
        budget -= len(row) + 1
        kept += 1
    omitted = len(rows) - kept
    marker = [f"\n_[{omitted} earlier messages omitted to fit {max_chars:,} characters; most recent shown]_"] if omitted else []
    body = "\n".join([head] + marker + rows[omitted:] + ["\n" + footer])
    return redact(body), omitted
