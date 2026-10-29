from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from ..model import Session, SessionInfo


class AmbiguousSession(Exception):
    pass


class NativeError(Exception):
    """The target agent could not create a native session; the caller falls back to the launch command."""


@dataclass
class Native:
    """A session baton created inside the target agent."""
    session_id: str
    command: str  # how to resume it
    path: Path | None = None  # file backing it, when there is one
    note: str = ""


class Adapter:
    name = ""
    cli = ""  # command used to launch this agent with an initial prompt
    native = False  # True if write_native can create a resumable session in this agent
    import_sources: frozenset = frozenset()  # source agents whose sessions this agent can import officially
    session_file = False  # True if the agent imports a session file through its own flag (Gemini's --session-file)
    handoff_suffix = ".md"  # extension of the file baton writes for this target
    source_only = False  # True if baton can read this agent's sessions but cannot move a session into it

    def list_sessions(self) -> list[SessionInfo]:
        """Cheap metadata only: never parse full transcripts here."""
        raise NotImplementedError

    def load(self, session_id: str) -> Session:
        """Parse exactly one session."""
        raise NotImplementedError

    def resolve(self, prefix: str) -> SessionInfo:
        hits = [s for s in self.list_sessions() if s.id.startswith(prefix)]
        if not hits:
            raise KeyError(f"no {self.name} session matches '{prefix}'")
        if len(hits) > 1:
            raise AmbiguousSession(
                f"'{prefix}' matches {len(hits)} {self.name} sessions: "
                + ", ".join(h.id for h in hits[:5]) + " (use a longer prefix)"
            )
        return hits[0]

    def launch_command(self, prompt: str) -> str:
        return f"{self.cli} {shlex.quote(prompt)}"

    def import_native(self, info: SessionInfo, cwd: Path) -> Native | None:
        """Bring a whole session in through the agent's own importer, when it has one for this source.

        Returns None when not applicable (the caller creates the session itself). Raises NativeError when
        the importer was tried and did not produce a session, so the caller can fall back and say why.
        """
        return None

    def render_session_file(self, session: Session, cwd: Path) -> str:
        """The text of the session file this agent imports (only when `session_file` is True)."""
        raise NotImplementedError

    def import_command(self, relpath: str) -> str:
        """The command that imports that file, run from the project folder."""
        raise NotImplementedError

    def write_native(self, session: Session, cwd: Path) -> Native:
        """Create a resumable session in this agent (only when `native` is True). Raises NativeError."""
        raise NativeError(f"{self.name} has no native session support")
