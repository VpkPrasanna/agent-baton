from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import adapters
from .adapters.base import AmbiguousSession
from .deliver import apply_plan, plan_move
from .summarize import FULL_MAX_CHARS


def unique_prefixes(ids: list[str], start: int = 12) -> dict[str, str]:
    """Shortest prefix (at least `start` chars) of each id that no other id shares. Time-based ids
    (UUIDv7, used by Codex) often share their first 12 characters."""
    out = {}
    for i in ids:
        n = start
        while n < len(i) and any(o != i and o.startswith(i[:n]) for o in ids):
            n += 1
        out[i] = i[:n]
    return out


def _fmt(s, prefixes: dict[str, str]) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.updated))
    tag = "  [subagent]" if s.subagent else ""
    return f"{prefixes.get(s.id, s.id[:12])}  {when}  {s.cwd}  {s.title}{tag}"


def _sessions(adapter, all_dirs: bool):
    """Returns (sessions to show, id prefixes that are unique across all of the agent's sessions)."""
    cwd = str(Path.cwd())
    found = adapter.list_sessions()
    _hint_hidden(adapter)
    shown = found if all_dirs else [s for s in found if s.cwd == cwd]
    return shown, unique_prefixes([s.id for s in found])


def _hint_hidden(adapter) -> None:
    n = getattr(adapter, "hidden_subagents", 0)
    if n:
        print(f"note: {n} subagent session(s) hidden by --no-subagents", file=sys.stderr)


def cmd_list(a) -> int:
    shown, prefixes = _sessions(adapters.get(a.source, include_subagents=not a.no_subagents), a.all)
    for s in shown[: a.limit]:
        print(_fmt(s, prefixes))
    return 0


def _pick(adapter, all_dirs: bool):
    shown, prefixes = _sessions(adapter, all_dirs)
    found = shown[:10]
    if not found:
        raise SystemExit(f"no {adapter.name} sessions found here (try --all)")
    for n, s in enumerate(found, 1):
        print(f"{n:>2}. {_fmt(s, prefixes)}")
    choice = input("session number: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(found):
        raise SystemExit("cancelled")
    return found[int(choice) - 1]


def cmd_move(a) -> int:
    if a.source == a.target:
        raise SystemExit("source and target are the same agent")
    src, dst = adapters.get(a.source, include_subagents=not a.no_subagents), adapters.get(a.target)
    try:
        info = src.resolve(a.session) if a.session else _pick(src, a.all)
    except (KeyError, AmbiguousSession) as e:
        _hint_hidden(src)
        raise SystemExit(str(e))
    mode = "full" if a.full else "summarize"
    if info.cwd and Path(info.cwd).is_dir():
        cwd = Path(info.cwd)
    else:
        cwd = Path.cwd()
        print(f"warning: session directory '{info.cwd}' does not exist; using {cwd} instead", file=sys.stderr)
    try:
        session = src.load(info.id)  # only this one session is parsed
    except (FileNotFoundError, KeyError) as e:
        raise SystemExit(f"error: {e}")
    plan = plan_move(session, dst, mode, cwd, a.last_n, a.max_chars)

    print(f"moving {info.agent}:{info.id[:12]} -> {dst.name} ({mode}, {len(session.messages)} messages)")
    for w in plan.warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(f"will write: {plan.handoff_file}")
    if dst.native and a.no_native:
        print(f"--no-native: no {dst.name} session will be created; you get the handoff file and a launch command")
    elif dst.native:
        print(f"will also create a new {dst.name} session you can resume")
        if mode == "full" and info.agent in dst.import_sources:
            print(f"will try {dst.name}'s official importer for the whole session; if it does not list the session, "
                  "baton builds one from the transcript instead (--max-chars applies only to that fallback)")
        if not a.keep_files:
            print("the handoff file is removed once that session exists (--keep-files to keep it)")
    if a.dry_run:
        print("\n" + plan.content)
        return 0
    if not a.yes and input("write files? [y/N] ").strip().lower() != "y":
        print("cancelled, nothing written")
        return 1
    plan = apply_plan(plan, session, dst, mode, cwd, a.keep_files, a.no_native)
    for n in plan.notes:
        print("note:", n)
    if plan.session_id:
        print(f"\nnew {dst.name} session id: {plan.session_id}")
        print(f"resume it (run in {cwd}):\n  {plan.command}")
    else:
        print(f"\nno session id yet: {dst.name} creates one when you start it. Run in {cwd}:\n  {plan.command}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="baton", description="Pass one coding-agent session to another assistant.")
    sub = p.add_subparsers(dest="cmd", required=True)
    names = list(adapters.ADAPTERS)

    ls = sub.add_parser("list", help="list sessions (metadata only)")
    ls.add_argument("--from", dest="source", required=True, choices=names)
    ls.add_argument("--all", action="store_true", help="all directories, not just the current one")
    ls.add_argument("--limit", type=int, default=20)
    ls.add_argument("--no-subagents", action="store_true", help="hide sessions the agent started itself (Kiro tags them)")
    ls.add_argument("--subagents", action="store_true", help=argparse.SUPPRESS)  # old flag: now the default, kept so scripts still run
    ls.set_defaults(fn=cmd_list)

    mv = sub.add_parser("move", help="move one session to another agent")
    mv.add_argument("--from", dest="source", required=True, choices=names)
    mv.add_argument("--to", dest="target", required=True, choices=names)
    mv.add_argument("--session", help="session id or unique prefix; omit to pick interactively")
    mode = mv.add_mutually_exclusive_group()
    mode.add_argument("--summarize", action="store_true", help="compact brief (default)")
    mode.add_argument("--full", action="store_true", help="whole transcript")
    mv.add_argument("--all", action="store_true", help="picker shows every directory")
    mv.add_argument("--no-subagents", action="store_true", help="hide sessions the agent started itself (Kiro tags them)")
    mv.add_argument("--subagents", action="store_true", help=argparse.SUPPRESS)  # old flag: now the default, kept so scripts still run
    mv.add_argument("--last-n", type=int, default=8, help="messages kept verbatim in the brief")
    mv.add_argument("--max-chars", type=int, default=FULL_MAX_CHARS,
                    help=f"size cap for --full; keeps the most recent messages (default {FULL_MAX_CHARS:,})")
    mv.add_argument("--dry-run", action="store_true", help="print the handoff, write nothing")
    mv.add_argument("--no-native", action="store_true",
                    help="do not create a session in the target: only write the handoff file and print the launch command")
    mv.add_argument("--keep-files", action="store_true",
                    help="keep the handoff file even after a new session was created in the target")
    mv.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    mv.set_defaults(fn=cmd_move)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
