"""Minimal JSON-RPC client for `codex app-server --stdio`, the interface editor extensions use."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time

from . import __version__

CLIENT_INFO = {"name": "baton", "title": "baton", "version": __version__}


class CodexRpcError(Exception):
    """Codex is missing, not responding, or rejected a request."""


class StdioTransport:
    """Spawns `codex app-server --stdio` and exchanges one JSON object per line."""

    def __init__(self, cmd: tuple[str, ...] = ("codex", "app-server", "--stdio")):
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except FileNotFoundError:
            raise CodexRpcError("the `codex` command was not found; install the Codex CLI")
        self._lines: queue.Queue = queue.Queue()
        self._pump_thread = threading.Thread(target=self._pump, daemon=True)
        self._pump_thread.start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            try:
                self._lines.put(json.loads(line))
            except ValueError:
                continue  # not protocol traffic
        self._lines.put(None)  # stream ended

    def send(self, obj: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise CodexRpcError("codex app-server exited unexpectedly")

    def recv(self, timeout: float) -> dict | None:
        """Next message, or None on timeout. Raises if the process has ended."""
        try:
            msg = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if msg is None:
            raise CodexRpcError("codex app-server exited unexpectedly")
        return msg

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
            self.proc.wait()
        self._pump_thread.join(timeout=2)  # the pump ends at EOF; then the pipe can be closed
        self.proc.stdout.close()


class AppServer:
    """Use as a context manager: it performs the initialize handshake and closes the transport."""

    def __init__(self, transport=None, timeout: float = 30):
        self.transport = transport or StdioTransport()
        self.timeout = timeout
        self._id = 0
        self.notifications: list[dict] = []  # server messages that were not the answer to a call

    def call(self, method: str, params: dict) -> dict:
        self._id += 1
        rid = self._id
        self.transport.send({"id": rid, "method": method, "params": params})
        while True:
            msg = self.transport.recv(self.timeout)
            if msg is None:
                raise CodexRpcError(f"{method}: no response within {self.timeout:g}s")
            if msg.get("id") != rid:
                if msg.get("method"):  # keep notifications: a caller may wait for one later
                    self.notifications.append(msg)
                continue  # (server requests we do not handle are dropped)
            if "error" in msg:
                raise CodexRpcError(f"{method}: {msg['error']}")
            return msg.get("result") or {}

    def wait_for(self, method: str, match=lambda params: True, timeout: float | None = None) -> dict:
        """Params of the first notification named `method` whose params satisfy `match`.

        Checks notifications already received (they may arrive before a call returns), then waits.
        """
        limit = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + limit
        pending = list(self.notifications)
        while True:
            for msg in pending:
                if msg.get("method") == method and match(msg.get("params") or {}):
                    return msg.get("params") or {}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexRpcError(f"{method}: not received within {limit:g}s")
            msg = self.transport.recv(min(remaining, 1.0))
            pending = []
            if msg is not None and msg.get("method"):
                self.notifications.append(msg)
                pending = [msg]

    def __enter__(self) -> "AppServer":
        try:
            self.call("initialize", {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": True}})
            self.transport.send({"method": "initialized"})
        except BaseException:
            self.transport.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.transport.close()
