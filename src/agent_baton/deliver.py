"""Turn a loaded session into files the target agent can pick up."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .adapters.base import Adapter, NativeError
from .model import Message, Session
from .summarize import FULL_MAX_CHARS, build_brief, build_transcript, native_messages


@dataclass
class Plan:
    handoff_file: Path
    content: str
    command: str
    native: bool = False
    session_id: str | None = None  # id of the new session in the target, when baton could create one
    handoff_removed: bool = False
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # shown before the confirmation prompt
    max_chars: int = FULL_MAX_CHARS


def plan_move(session: Session, target: Adapter, mode: str, cwd: Path, last_n: int = 8,
              max_chars: int = FULL_MAX_CHARS) -> Plan:
    warnings: list[str] = []
    if mode == "summarize":
        body = build_brief(session, last_n)
    else:
        body, omitted = build_transcript(session, max_chars)
        if omitted:
            warnings.append(
                f"transcript capped at {max_chars:,} characters: {omitted} earlier messages omitted "
                "(raise --max-chars to keep more, but large handoffs may overflow the target's context)"
            )
    i = session.info
    path = cwd / ".baton" / f"{i.agent}-{i.id[:8]}-{mode}.md"
    prompt = (
        f"You are continuing a session moved from {i.agent}. Read {path.relative_to(cwd)} first, "
        "check the repo state with git, then continue where it left off."
    )
    plan = Plan(path, body, target.launch_command(prompt), warnings=warnings, max_chars=max_chars)
    return plan


def remove_handoff(plan: Plan) -> None:
    """Delete the handoff file baton just wrote (its content now lives in the new session), and the
    .baton folder if that left it empty. Never touches any other file."""
    plan.handoff_file.unlink(missing_ok=True)
    try:
        plan.handoff_file.parent.rmdir()  # only succeeds when empty
    except OSError:
        pass
    plan.handoff_removed = True
    plan.notes.append("removed the handoff file: its content is now in the new session (--keep-files to keep it)")


def _create_session(plan: Plan, session: Session, target: Adapter, mode: str, cwd: Path):
    """baton's own way in: the brief, plus the most recent messages for --full, as a new native session."""
    if mode == "full":
        msgs, omitted = native_messages(session, plan.max_chars)
        if omitted:
            plan.notes.append(f"native session holds the brief plus the most recent messages ({omitted} earlier omitted)")
    else:  # the brief, plus an acknowledgement so the conversation ends on an assistant turn
        msgs = [Message("user", "text", plan.content), Message(
            "assistant", "text",
            f"Handoff received from {session.info.agent}. I have the current task, plan and files above. "
            "Say \"continue\" to pick up where it left off, or give me new instructions.")]
    try:
        return target.write_native(Session(session.info, msgs), cwd)
    except NativeError as e:  # keep the handoff file and launch command, which always work
        plan.notes.append(f"{e}; use the launch command instead")
        return None


def apply_plan(plan: Plan, session: Session, target: Adapter, mode: str, cwd: Path, keep_files: bool = False,
               no_native: bool = False) -> Plan:
    """Write the handoff file, then (unless no_native) create a session in the target.

    With no_native, baton only writes the handoff file: the command it prints is the launch command.
    """
    plan.handoff_file.parent.mkdir(parents=True, exist_ok=True)
    plan.handoff_file.write_text(plan.content, encoding="utf-8")
    plan.handoff_file.chmod(0o600)
    if target.native and not no_native:
        made = None
        if mode == "full" and session.info.agent in target.import_sources:
            try:  # the target's own importer brings in the whole session, which beats rebuilding it from text
                made = target.import_native(session.info, cwd)
            except NativeError as e:
                plan.notes.append(f"{e}; creating the session from the transcript instead")
        if made is None:
            made = _create_session(plan, session, target, mode, cwd)
        if made:
            plan.native, plan.command, plan.session_id = True, made.command, made.session_id
            if made.path:
                plan.notes.append(f"native session file: {made.path} (experimental)")
            if made.note:
                plan.notes.append(made.note)
            if not keep_files:  # only after the session exists; on failure the file stays as the fallback
                remove_handoff(plan)
    elif mode == "full" and target.native:  # --no-native
        plan.notes.append("--no-native: the transcript is written as a file only, so this is a context replay, not a true resume.")
    elif mode == "full":
        plan.notes.append(f"{target.name} has no importable session format, so this is a context replay, not a true resume.")
    return plan
