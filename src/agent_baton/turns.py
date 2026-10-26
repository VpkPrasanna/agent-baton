"""Flatten a session into plain user/assistant turns for agents that import history as text."""
from __future__ import annotations

from .model import Message, short


def flatten_turns(messages: list[Message]) -> list[tuple[str, str]]:
    """Tool calls and results become text so no orphaned tool blocks remain; same-role neighbours merge.

    The result always starts with a user turn.
    """
    turns: list[tuple[str, str]] = []
    for m in messages:
        role = "user" if m.role == "user" else "assistant"
        text = m.text if m.kind == "text" else f"[{m.kind}] {short(m.text, 500)}"
        if turns and turns[-1][0] == role:
            turns[-1] = (role, turns[-1][1] + "\n\n" + text)
        else:
            turns.append((role, text))
    if turns and turns[0][0] != "user":
        turns.insert(0, ("user", "(continuing a moved session)"))
    return turns
