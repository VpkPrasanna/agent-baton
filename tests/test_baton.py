import hashlib
import json
import os
import queue
import subprocess
import sqlite3
import sys
import tempfile
from contextlib import closing
import unittest
from pathlib import Path

from agent_baton.adapters import ClaudeCodeAdapter, CodexAdapter, CursorAdapter, GeminiAdapter, KiroAdapter
import agent_baton.adapters.codex as codex_module
import agent_baton.adapters.gemini as gemini_module
import agent_baton.adapters.kiro as kiro_module
from agent_baton.adapters.base import AmbiguousSession, NativeError
from agent_baton.adapters.gemini import project_hash, read_session_file
from agent_baton.codex_rpc import AppServer, CodexRpcError, StdioTransport
from agent_baton.deliver import apply_plan, plan_move
from agent_baton.redact import redact
from agent_baton.summarize import build_brief, build_transcript


THREAD_ID = "01ff0001-aaaa-bbbb-cccc-dddddddddddd"


class FakeTransport:
    """Stands in for `codex app-server --stdio`: answers each request, after some notification noise."""

    def __init__(self, fail_on=None, silent=False):
        self.sent, self.out, self.closed, self.fail_on, self.silent = [], queue.Queue(), False, fail_on, silent

    def send(self, obj):
        self.sent.append(obj)
        if "id" not in obj or self.silent:
            return
        self.out.put({"method": "mcpServer/startupStatus/updated", "params": {}})
        if obj["method"] == self.fail_on:
            self.out.put({"id": obj["id"], "error": {"code": -1, "message": "boom"}})
        elif obj["method"] == "thread/start":
            self.out.put({"id": obj["id"], "result": {"thread": {"id": THREAD_ID, "path": "/x"}}})
        else:
            self.out.put({"id": obj["id"], "result": {}})

    def recv(self, timeout):
        try:
            return self.out.get(timeout=0.2)
        except queue.Empty:
            return None

    def close(self):
        self.closed = True

    def methods(self):
        return [m.get("method") for m in self.sent]


def fake_server(transport):
    return lambda: AppServer(transport, timeout=0.5)


IMPORTED_THREAD = "01ee0001-aaaa-bbbb-cccc-eeeeeeeeeeee"


class ImporterTransport(FakeTransport):
    """FakeTransport that also speaks Codex's importer: detect lists `sessions`, import reports `outcome`.

    outcome: success | early (completion arrives before the response) | failure | silent (never completes).
    """

    def __init__(self, sessions, outcome="success", detect_error=False, **kw):
        super().__init__(**kw)
        self.sessions, self.outcome, self.detect_error = sessions, outcome, detect_error

    def send(self, obj):
        method = obj.get("method", "")
        if "id" not in obj or not method.startswith("externalAgentConfig/"):
            return super().send(obj)
        self.sent.append(obj)
        self.out.put({"method": "mcpServer/startupStatus/updated", "params": {}})
        if method.endswith("/detect"):
            if self.detect_error:
                return self.out.put({"id": obj["id"], "error": {"code": -32603, "message": "invalid MCP config"}})
            items = [{"itemType": "MCP_SERVER_CONFIG", "description": "mcp", "cwd": None, "details": {"mcpServers": [{"name": "x"}]}},
                     {"itemType": "SESSIONS", "description": "Migrate recent sessions", "cwd": None,
                      "details": {"sessions": self.sessions}}]
            return self.out.put({"id": obj["id"], "result": {"items": items, "connectors": []}})
        ours = obj["params"]["migrationItems"][0]["details"]["sessions"][0]
        done = {"method": "externalAgentConfig/import/completed", "params": {"importId": "imp-1", "itemTypeResults": [
            {"itemType": "SESSIONS",
             "successes": [{"itemType": "SESSIONS", "cwd": ours["cwd"], "source": ours["path"], "target": IMPORTED_THREAD}]
             if self.outcome != "failure" else [],
             "failures": [{"itemType": "SESSIONS", "source": ours["path"], "error": "boom"}] if self.outcome == "failure" else []}]}}
        response = {"id": obj["id"], "result": {"importId": "imp-1"}}
        if self.outcome == "early":
            self.out.put(done)
        self.out.put(response)
        if self.outcome in ("success", "failure"):
            self.out.put(done)


def importer_server(transports, sessions, **kw):
    """A server factory that hands out a fresh ImporterTransport per connection and records them."""
    def factory():
        t = ImporterTransport(sessions, **kw)
        transports.append(t)
        return AppServer(t, timeout=0.5)
    return factory


def block_real_codex(test):
    """No test may start the real `codex app-server`: it would create threads in the user's Codex."""
    def blocked(*a, **k):
        raise CodexRpcError("real codex is blocked in tests")
    patcher = __import__("unittest.mock", fromlist=["patch"]).patch.object(codex_module, "AppServer", blocked)
    patcher.start()
    test.addCleanup(patcher.stop)


def cursor_chat(root: Path, chat_id: str, cwd: str = "/proj", name: str = "Fix parser", has_conversation: bool = True,
                history=None, store: bool = True, updated_ms: int = 1_790_000_000_000):
    """Builds a Cursor CLI chat: <md5 of cwd>/<chat id>/{meta.json, store.db, prompt_history.json}.

    The store is a graph of content-addressed blobs: JSON messages, linked by protobuf nodes. Layout of this
    fixture: root -> [system message, nodeA]; nodeA -> [context msg, user msg, assistant msg, nodeC];
    nodeC -> [tool result, assistant text, tool result (dict), assistant text again (duplicate ref)].
    One more message exists in the store but is not reachable from the root.
    """
    chat = root / hashlib.md5(cwd.encode()).hexdigest() / chat_id
    chat.mkdir(parents=True)
    (chat / "meta.json").write_text(json.dumps({"schemaVersion": 1, "createdAtMs": updated_ms - 1000, "updatedAtMs": updated_ms,
                                                "hasConversation": has_conversation, "cwd": cwd}))
    if history is not None:
        (chat / "prompt_history.json").write_text(json.dumps(history))
    if not store:
        return chat
    blobs = {}

    def put(data: bytes) -> str:
        h = hashlib.sha256(data).hexdigest()
        blobs[h] = data
        return h

    def msg(role, content):
        return put(json.dumps({"id": role, "role": role, "content": content}).encode())

    def node(*refs, field=8):
        return b"".join(bytes([(field << 3) | 2, 32]) + bytes.fromhex(r) for r in refs) + bytes([0x50, 0x01]) + \
            bytes([(15 << 3) | 2, 32]) + b"\x00" * 32  # a varint field and a 32-byte field that is not a blob id

    system = msg("system", "You are a coding agent.")
    ctx = msg("user", "<user_info>OS: darwin</user_info>")
    user = msg("user", [{"type": "text", "text": "<timestamp>Monday</timestamp>\n<user_query>\nfix the parser\n</user_query>"}])
    asst = msg("assistant", [{"type": "reasoning", "text": "thinking", "signature": "x"}, {"type": "text", "text": "On it."},
                             {"type": "tool-call", "toolCallId": "1", "toolName": "StrReplace",
                              "args": {"path": f"{cwd}/p.py", "old_string": "a", "new_string": "b"}}])
    tool = msg("tool", [{"type": "tool-result", "toolCallId": "1", "toolName": "StrReplace", "result": "ok"}])
    done = msg("assistant", "Done.")
    tool_dict = msg("tool", [{"type": "tool-result", "toolCallId": "2", "toolName": "CallDynamicTool", "result": {"a": 1}}])
    msg("user", [{"type": "text", "text": "<user_query>orphan: not reachable</user_query>"}])  # in the store, not in the graph
    node_c = put(node(tool, done, tool_dict, done))
    node_a = put(node(ctx, user, asst, node_c))
    root_blob = put(node(system, field=1) + node(node_a))
    meta = {"agentId": "a", "latestRootBlobId": root_blob, "name": name, "createdAt": 1, "mode": "agent"}
    with closing(sqlite3.connect(chat / "store.db")) as c:
        c.execute("create table blobs (id text primary key, data blob)")
        c.execute("create table meta (key text primary key, value text)")
        c.executemany("insert into blobs values (?, ?)", blobs.items())
        c.execute("insert into meta values ('0', ?)", (json.dumps(meta).encode().hex(),))
        c.commit()
    return chat


# ---- Gemini CLI: fixtures, and Gemini's own session loader used as a ground-truth oracle ----

TS = "2026-10-26T10:00:00.000Z"


def gemini_meta(sid="00000000-0000-4000-8000-000000000002", **extra):
    return {"sessionId": sid, "projectHash": "x", "startTime": TS, "lastUpdated": TS, "kind": "main", **extra}


def gm(id, type, content, **extra):
    return {"id": id, "timestamp": TS, "type": type, "content": content, **extra}


def gemini_fixtures(folder: Path) -> dict:
    """Session files covering every record kind Gemini writes, as {name: path}."""
    folder.mkdir(parents=True, exist_ok=True)
    cases = {
        "basic": [gemini_meta(), gm("u1", "user", "first question"), gm("g1", "gemini", "first answer")],
        "parts content": [gemini_meta(), gm("u1", "user", [{"text": "part one"}, {"text": "part two"}]), gm("g1", "gemini", "ok")],
        "tool result patch": [
            gemini_meta(), gm("u1", "user", "read it"),
            gm("g1", "gemini", "reading", toolCalls=[{"id": "t1", "name": "read_file", "args": {"absolute_path": "/p/a.py"}, "status": "executing"},
                                                     {"id": "t2", "name": "write_file", "args": {"file_path": "/p/b.py", "content": "x"}}]),
            {"$patch": {"id": "g1", "toolCalls": [{"id": "t1", "result": [{"functionResponse": {"id": "t1", "name": "read_file", "response": {"output": "file body"}}}]}]}}],
        "patch updates remove order": [
            gemini_meta(), gm("a", "user", "A"), gm("b", "gemini", "B"), gm("c", "user", "C"), gm("d", "gemini", "D"),
            {"$patch": {"updates": [{"id": "b", "content": "B patched"}, {"id": "zzz", "content": "no such message"}],
                        "removeIds": ["c"], "orderIds": ["d", "b"]}}],
        "rewind found": [gemini_meta(), gm("u1", "user", "keep"), gm("g1", "gemini", "keep"), gm("u2", "user", "drop"),
                         gm("g2", "gemini", "drop"), {"$rewindTo": "u2"}, gm("u3", "user", "after the rewind")],
        "rewind unknown clears all": [gemini_meta(), gm("u1", "user", "gone"), gm("g1", "gemini", "gone"),
                                      {"$rewindTo": "does-not-exist"}, gm("u9", "user", "fresh start")],
        "same id again replaces in place": [gemini_meta(), gm("u1", "user", "v1"), gm("g1", "gemini", "reply"), gm("u1", "user", "v2")],
        "shaped like a real 0.65.0 session": [
            {"kind": "main", "lastUpdated": TS, "projectHash": "x", "sessionId": "00000000-0000-4000-8000-0000000000aa", "startTime": TS},
            gm("c1", "user", [{"text": "<session_context>\nThis is the Gemini CLI. We are setting up the context for our chat.\n</session_context>"}]),
            {"$set": {"lastUpdated": TS}},
            gm("c2", "user", [{"text": "hello"}]),
            {"$set": {"lastUpdated": TS}}],
        "set metadata and info messages": [gemini_meta(), gm("i1", "info", "Imported session"), gm("u1", "user", "hello"),
                                           gm("e1", "error", "something failed"), gm("g1", "gemini", "hi", thoughts=[{"subject": "x"}]),
                                           {"$set": {"summary": "A tidy summary"}}],
    }
    paths = {}
    for name, records in cases.items():
        path = folder / (name.replace(" ", "-") + ".jsonl")
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        paths[name] = path
    messy = folder / "blank-and-garbage-lines.jsonl"  # a blank line and a half-written line must not break the replay
    messy.write_text(json.dumps(gemini_meta()) + "\n\n" + json.dumps(gm("u1", "user", "q")) + "\nnot json at all\n"
                     + json.dumps(gm("g1", "gemini", "a")) + "\n")
    paths["blank and garbage lines"] = messy
    legacy = folder / "legacy.json"
    legacy.write_text(json.dumps({**gemini_meta(), "messages": [gm("m1", "user", "old format question"), gm("m2", "gemini", "old format answer")]}))
    paths["legacy json"] = legacy
    return paths


_ORACLE = []


def gemini_oracle():
    """(node, chunk file) when Gemini CLI and Node are installed: Gemini's own loader, run offline, with no login."""
    if not _ORACLE:
        import shutil
        found = None
        node, gem = shutil.which("node"), shutil.which("gemini")
        if node and gem:
            for chunk in sorted(Path(os.path.realpath(gem)).parent.glob("chunk-*.js")):
                text = chunk.read_text(encoding="utf-8", errors="replace")
                if "async function loadConversationRecord(" in text and "loadConversationRecord" in text[text.rfind("export {"):]:
                    found = (node, chunk)
                    break
        _ORACLE.append(found)
    return _ORACLE[0]


def run_gemini_loader(paths) -> dict:
    """{path: {"summary", "messages": [{id, type, content, toolCalls}]} or None} as Gemini's loader reads each file."""
    node, chunk = gemini_oracle()
    script = Path(tempfile.mkdtemp()) / "oracle.mjs"
    script.write_text(
        "const { loadConversationRecord } = await import('file://' + process.argv[2]);\n"
        "const out = {};\n"
        "for (const f of process.argv.slice(3)) {\n"
        "  const r = await loadConversationRecord(f);\n"
        "  out[f] = r ? { summary: r.summary, messages: (r.messages || []).map(m => ({ id: m.id, type: m.type, content: m.content, toolCalls: m.toolCalls })) } : null;\n"
        "}\nconsole.log('ORACLE ' + JSON.stringify(out));\nprocess.exit(0);\n")
    done = subprocess.run([node, str(script), str(chunk), *map(str, paths)], capture_output=True, text=True, timeout=120)
    line = next(l for l in done.stdout.splitlines() if l.startswith("ORACLE "))
    return json.loads(line[len("ORACLE "):])


def block_real_kiro(test):
    """No test may run the real `kiro-cli`: its listing would mix this machine's sessions into the results."""
    def blocked(*a, **k):
        raise FileNotFoundError("real kiro-cli is blocked in tests")
    patcher = __import__("unittest.mock", fromlist=["patch"]).patch.object(kiro_module, "_run_kiro_list", blocked)
    patcher.start()
    test.addCleanup(patcher.stop)


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def claude_fixture(root: Path, sid="aaaa1111-0000"):
    write_jsonl(root / "-proj" / f"{sid}.jsonl", [
        {"type": "user", "cwd": "/proj", "message": {"role": "user", "content": "add a login page"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Creating it."},
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "/proj/login.py"}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "ignore me"}},
    ])


def codex_fixture(root: Path, sid="bbbb2222-0000"):
    write_jsonl(root / "2026/10/20" / f"rollout-x-{sid}.jsonl", [
        {"type": "session_meta", "payload": {"id": sid, "cwd": "/proj"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>x"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the bug"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": "{\"path\": \"a.py\"}", "call_id": "c"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c", "output": [{"type": "text", "text": "done"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Fixed."}]}},
    ])


def kiro_fixture(db: Path, cid="cccc3333-0000"):
    hist = [
        {"user": {"content": {"Prompt": {"prompt": "deploy the stack"}}}, "assistant": {"ToolUse": {"content": "Running", "tool_uses": [{"id": "1", "name": "execute_bash", "args": {"command": "ls"}}]}}},
        {"user": {"content": {"ToolUseResults": {"tool_use_results": [{"content": [{"Text": "file.txt"}]}]}}}, "assistant": {"Response": {"content": "Deployed."}}},
    ]
    with closing(sqlite3.connect(db)) as c:
        c.execute("create table conversations_v2 (key text, conversation_id text, value text, created_at int, updated_at int)")
        c.execute("insert into conversations_v2 values ('/proj', ?, ?, 1, 2000)", (cid, json.dumps({"history": hist})))
        c.commit()


class AdapterTests(unittest.TestCase):
    def setUp(self):
        block_real_codex(self)
        block_real_kiro(self)
        self.tmp = Path(tempfile.mkdtemp())

    def test_claude_loads_only_requested_and_skips_sidechain(self):
        claude_fixture(self.tmp)
        a = ClaudeCodeAdapter(self.tmp)
        s = a.load("aaaa")
        texts = [m.text for m in s.messages if m.kind == "text"]
        self.assertEqual(texts, ["add a login page", "Creating it."])
        self.assertEqual(s.info.cwd, "/proj")
        self.assertTrue(any(m.kind == "tool_call" for m in s.messages))

    def test_codex_filters_injected_context(self):
        codex_fixture(self.tmp)
        s = CodexAdapter(self.tmp).load("bbbb")
        self.assertEqual([m.text for m in s.messages if m.kind == "text"], ["fix the bug", "Fixed."])

    def test_codex_event_msg_fallback(self):
        write_jsonl(self.tmp / "2026/10/21" / "rollout-y-dddd4444.jsonl", [
            {"type": "session_meta", "payload": {"id": "dddd4444", "cwd": "/proj"}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "hello codex"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "hi"}},
        ])
        a = CodexAdapter(self.tmp)
        self.assertEqual(a.list_sessions()[0].title, "hello codex")
        self.assertEqual([m.text for m in a.load("dddd").messages], ["hello codex", "hi"])

    def _codex_dbs(self, home: Path, threads, items, state="state_5.sqlite", history="thread_history_1.sqlite"):
        """threads: (id, cwd, title, first_user_message, updated_at); items: (thread_id, ordinal, type, dict)."""
        with closing(sqlite3.connect(home / state)) as c:
            c.execute("create table threads (id text, cwd text, title text, first_user_message text, updated_at integer, archived integer)")
            c.executemany("insert into threads values (?,?,?,?,?,0)", threads)
            c.commit()
        with closing(sqlite3.connect(home / history)) as c:
            c.execute("create table thread_items (thread_id text, turn_id text, rollout_ordinal integer, item_type text, item_json text)")
            c.executemany("insert into thread_items values (?, 't', ?, ?, ?)", [(t, o, ty, json.dumps(d)) for t, o, ty, d in items])
            c.commit()

    def test_codex_sqlite_fallback_lists_and_loads(self):
        home = self.tmp / "codex"
        home.mkdir()
        self._codex_dbs(home,
            [("01aa0001-x", "/proj", "Fix the parser", "first msg", 1_790_000_000),
             ("01aa0002-x", "/proj", "No items", "", 1_790_000_100),          # thread without history items: not listed
             ("01aa0003-x", "/proj", "", "only first message", 1_790_000_200_000)],  # ms timestamp, falls back to first message
            [("01aa0001-x", 3, "agentMessage", {"text": "Patched it."}),
             ("01aa0001-x", 1, "userMessage", {"content": [{"type": "text", "text": "fix the parser"}]}),
             ("01aa0001-x", 2, "reasoning", {"summary": [], "content": []}),
             ("01aa0001-x", 4, "commandExecution", {"command": "pytest -q", "cwd": "/proj", "aggregatedOutput": "3 passed", "exitCode": 0}),
             ("01aa0001-x", 5, "fileChange", {"changes": [{"path": "/proj/parser.py", "kind": {}, "diff": "..."}], "status": "completed"}),
             ("01aa0003-x", 1, "userMessage", {"content": [{"type": "text", "text": "hello"}]})])
        a = CodexAdapter(home / "sessions", home=home)  # no rollout files at all
        listed = a.list_sessions()
        self.assertEqual([(s.id, s.title) for s in listed], [("01aa0003-x", "only first message"), ("01aa0001-x", "Fix the parser")])
        self.assertAlmostEqual(listed[0].updated, 1_790_000_200, delta=1)  # ms normalised to seconds
        s = a.load("01aa0001")
        self.assertEqual([(m.role, m.kind) for m in s.messages], [
            ("user", "text"), ("assistant", "text"), ("assistant", "tool_call"), ("tool", "tool_result"),
            ("assistant", "tool_call"), ("tool", "tool_result")])  # ordered by rollout_ordinal, reasoning skipped
        self.assertEqual(s.messages[0].text, "fix the parser")
        self.assertEqual(s.messages[3].text, "3 passed")
        from agent_baton.summarize import files_touched
        self.assertEqual(files_touched(s).edited, ["parser.py"] if s.info.cwd == "/proj" else ["/proj/parser.py"])

    def test_codex_rollout_file_beats_sqlite_copy_and_missing_dbs_are_fine(self):
        home = self.tmp / "codex"
        (home / "sessions").mkdir(parents=True)
        codex_fixture(home / "sessions", sid="bbbb2222-0000")
        self.assertEqual([s.id for s in CodexAdapter(home / "sessions", home=home).list_sessions()], ["bbbb2222-0000"])  # no DBs
        self._codex_dbs(home, [("bbbb2222-0000", "/proj", "dup", "dup", 1_790_000_000), ("01cc0001-x", "/p", "t", "t", 1_790_000_001)],
                        [("bbbb2222-0000", 1, "userMessage", {"content": [{"type": "text", "text": "FROM SQLITE"}]}),
                         ("01cc0001-x", 1, "userMessage", {"content": [{"type": "text", "text": "x"}]})])
        a = CodexAdapter(home / "sessions", home=home)
        self.assertEqual(sorted(s.id for s in a.list_sessions()), ["01cc0001-x", "bbbb2222-0000"])  # no duplicate id
        self.assertEqual([m.text for m in a.load("bbbb").messages if m.kind == "text"], ["fix the bug", "Fixed."])  # from the rollout file

    def test_list_shows_unique_prefixes_for_time_based_ids(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from agent_baton import cli
        from agent_baton.cli import unique_prefixes
        ids = ["01a0d247-7711-aaaa", "01a0d247-7712-bbbb", "01a0d247-7712-cccc", "01a158a8-f649-dddd"]
        p = unique_prefixes(ids)
        self.assertEqual(p["01a158a8-f649-dddd"], "01a158a8-f64")                  # default 12 chars is already unique
        self.assertEqual(p["01a0d247-7711-aaaa"], "01a0d247-7711")
        self.assertEqual(p["01a0d247-7712-bbbb"], "01a0d247-7712-b")               # extended until it differs
        self.assertEqual(len(set(p.values())), 4)
        home = self.tmp / "codex"
        home.mkdir()
        self._codex_dbs(home, [(i, "/p", f"t{n}", "x", 1_790_000_000 + n) for n, i in enumerate(ids)],
                        [(i, 1, "userMessage", {"content": [{"type": "text", "text": "x"}]}) for i in ids])
        a = CodexAdapter(home / "sessions", home=home)
        out = io.StringIO()
        with mock.patch.object(cli.adapters, "get", return_value=a), redirect_stdout(out):
            cli.main(["list", "--from", "codex", "--all"])
        shown = [line.split()[0] for line in out.getvalue().splitlines()]
        self.assertEqual(len(set(shown)), 4)  # no two rows look the same
        for prefix in shown:
            self.assertEqual(a.resolve(prefix).id[: len(prefix)], prefix)  # and each one resolves
        with self.assertRaises(AmbiguousSession) as e:
            a.resolve("01a0d247-771")
        self.assertIn("01a0d247-7711-aaaa", str(e.exception))  # error shows full ids

    def test_codex_uses_newest_versioned_database(self):
        home = self.tmp / "codex"
        home.mkdir()
        self._codex_dbs(home, [("01dd0001-x", "/p", "old", "old", 1_790_000_000)], [("01dd0001-x", 1, "userMessage", {"content": [{"type": "text", "text": "x"}]})],
                        state="state_4.sqlite", history="thread_history_1.sqlite")
        self._codex_dbs(home, [("01dd0002-x", "/p", "new", "new", 1_790_000_001)], [("01dd0002-x", 1, "userMessage", {"content": [{"type": "text", "text": "x"}]})],
                        state="state_5.sqlite", history="thread_history_2.sqlite")
        self.assertEqual([s.id for s in CodexAdapter(home / "sessions", home=home).list_sessions()], ["01dd0002-x"])

    def test_kiro_parses_tool_turns(self):
        db = self.tmp / "k.sqlite3"
        kiro_fixture(db)
        a = KiroAdapter(db, sessions_dir=self.tmp / "no-v2-sessions")
        self.assertEqual(a.list_sessions()[0].title, "deploy the stack")
        s = a.load("cccc")
        self.assertEqual([m.text for m in s.messages if m.kind == "text"], ["deploy the stack", "Running", "Deployed."])
        self.assertIn("file.txt", [m.text for m in s.messages if m.kind == "tool_result"])

    def _kiro_v2(self, folder: Path, sid: str, reason="user", title="v2 task"):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{sid}.json").write_text(json.dumps({
            "session_id": sid, "cwd": "/proj", "title": title, "session_created_reason": reason,
            "updated_at": "2026-10-23T09:28:28.913561Z"}))
        write_jsonl(folder / f"{sid}.jsonl", [
            {"version": "v1", "kind": "Prompt", "data": {"content": [{"kind": "text", "data": "refactor auth"}]}},
            {"version": "v1", "kind": "AssistantMessage", "data": {"content": [
                {"kind": "thinking", "data": {"text": ""}},
                {"kind": "text", "data": "On it."},
                {"kind": "toolUse", "data": {"toolUseId": "t1", "name": "fs_write", "input": {"path": "/proj/auth.py"}}}]}},
            {"version": "v1", "kind": "ToolResults", "data": {"content": [
                {"kind": "toolResult", "data": {"toolUseId": "t1", "content": [{"kind": "text", "data": "written"}]}}]}},
            {"version": "v1", "kind": "AssistantMessage", "data": {"content": [{"kind": "text", "data": "Done."}]}},
        ])

    def test_kiro_v2_reader(self):
        d = self.tmp / "cli"
        self._kiro_v2(d, "eeee5555-0000")
        a = KiroAdapter(db=self.tmp / "missing.sqlite3", sessions_dir=d)  # no SQLite store needed
        info = a.list_sessions()
        self.assertEqual([(s.id, s.cwd, s.title) for s in info], [("eeee5555-0000", "/proj", "v2 task")])
        self.assertGreater(info[0].updated, 1.7e9)
        s = a.load("eeee")
        self.assertEqual([m.text for m in s.messages if m.kind == "text"], ["refactor auth", "On it.", "Done."])
        self.assertEqual([m.text for m in s.messages if m.kind == "tool_result"], ["written"])
        self.assertEqual(s.messages[2].meta["args"], {"path": "/proj/auth.py"})  # thinking block skipped

    def test_kiro_v2_shows_subagents_by_default_and_keeps_sqlite_fallback(self):
        d = self.tmp / "cli"
        self._kiro_v2(d, "ffff6666-0000")
        self._kiro_v2(d, "aaaa7777-0000", reason="subagent")
        db = self.tmp / "k.sqlite3"
        kiro_fixture(db)
        a = KiroAdapter(db=db, sessions_dir=d)  # default: everything, subagent runs included and labelled
        listed = {x.id[:4]: x for x in a.list_sessions()}
        self.assertEqual(set(listed), {"ffff", "cccc", "aaaa"})
        self.assertEqual({k: v.subagent for k, v in listed.items()}, {"ffff": False, "cccc": False, "aaaa": True})
        self.assertEqual(a.hidden_subagents, 0)
        self.assertEqual(a.load("cccc").messages[-1].text, "Deployed.")  # v1 still loads
        self.assertEqual(a.load("aaaa").messages[-1].text, "Done.")  # so does a subagent-tagged session
        b = KiroAdapter(db=db, sessions_dir=d, include_subagents=False)  # opt-out
        self.assertEqual({x.id[:4] for x in b.list_sessions()}, {"ffff", "cccc"})
        self.assertEqual(b.hidden_subagents, 1)

    def test_kiro_nothing_installed_raises(self):
        a = KiroAdapter(db=self.tmp / "no.db", sessions_dir=self.tmp / "none")
        with self.assertRaises(FileNotFoundError):
            a.list_sessions()

    def test_cursor_list_titles_and_filters(self):
        root = self.tmp / "chats"
        cursor_chat(root, "aaaa0001", name="Fix parser")
        cursor_chat(root, "aaaa0002", name="Nothing said", has_conversation=False)       # no conversation: not listed
        cursor_chat(root, "aaaa0003", name="New Agent", history=["first saved prompt"], cwd="/other")
        cursor_chat(root, "aaaa0004", name="New Agent", cwd="/third")                    # untitled, no history: first user message
        cursor_chat(root, "aaaa0005", name="No store", store=False, cwd="/fourth")        # store.db missing: not listed
        listed = {s.id: s for s in CursorAdapter(root).list_sessions()}
        self.assertEqual(sorted(listed), ["aaaa0001", "aaaa0003", "aaaa0004"])
        self.assertEqual(listed["aaaa0001"].title, "Fix parser")
        self.assertEqual(listed["aaaa0001"].cwd, "/proj")
        self.assertEqual(listed["aaaa0003"].title, "first saved prompt")
        self.assertEqual(listed["aaaa0004"].title, "fix the parser")
        self.assertAlmostEqual(listed["aaaa0001"].updated, 1_790_000_000, delta=1)  # ms normalised to seconds

    def test_cursor_load_walks_the_blob_graph(self):
        root = self.tmp / "chats"
        cursor_chat(root, "bbbb0001")
        s = CursorAdapter(root).load("bbbb")
        self.assertEqual([(m.role, m.kind, m.text) for m in s.messages if m.kind != "tool_call"], [
            ("user", "text", "fix the parser"),        # only the <user_query> text; system and context messages are dropped
            ("assistant", "text", "On it."),            # reasoning part skipped
            ("tool", "tool_result", "ok"),
            ("assistant", "text", "Done."),             # duplicate reference to the same blob appears once
            ("tool", "tool_result", '{"a": 1}'),        # non-string results are serialised
        ])
        call = next(m for m in s.messages if m.kind == "tool_call")
        self.assertEqual((call.meta["name"], call.meta["args"]["path"]), ("StrReplace", "/proj/p.py"))
        self.assertNotIn("orphan", " ".join(m.text for m in s.messages))  # unreachable messages are ignored
        from agent_baton.summarize import files_touched
        self.assertEqual(files_touched(s).edited, ["p.py"])  # StrReplace counts as an edit, path relative to cwd

    def test_cursor_is_registered_and_is_a_launch_only_target(self):
        from agent_baton import adapters
        self.assertIn("cursor", adapters.ADAPTERS)
        self.assertFalse(CursorAdapter(self.tmp).native)
        self.assertEqual(adapters.get("cursor").launch_command("go"), "cursor-agent go")

    # ---- Kiro: official listing first, files as fallback ----

    def _official(self, *envelopes):
        return json.dumps([{"cwd": cwd, "sessions": sessions, "complete": True} for cwd, sessions in envelopes])

    def test_kiro_official_listing_is_primary_and_files_fill_the_gaps(self):
        d = self.tmp / "cli"
        self._kiro_v2(d, "aaaa1111-0000", title="From files")             # only on disk
        self._kiro_v2(d, "ffff6666-0000", title="File title")             # on disk and listed by Kiro
        self._kiro_v2(d, "bbbb7777-0000", reason="subagent", title="Sub title")
        listing = self._official(
            ("/proj", [{"sessionId": "ffff6666-0000", "source": "v2", "title": "Official title", "updatedAt": "2026-10-23T09:28:28.913Z", "messageCount": 4},
                       {"sessionId": "bbbb7777-0000", "source": "v2", "title": "(no title)", "updatedAt": "2026-10-23T09:00:00.000Z", "messageCount": 2}]),
            ("/other", [{"sessionId": "cccc8888-0000", "source": "v2", "title": "Only Kiro knows", "updatedAt": "2026-10-22T09:00:00.000Z", "messageCount": 9}]))
        calls = []
        def runner():
            calls.append(1)
            return listing
        a = KiroAdapter(db=self.tmp / "none.db", sessions_dir=d, runner=runner)
        got = {x.id[:4]: x for x in a.list_sessions()}
        self.assertEqual(set(got), {"aaaa", "ffff", "bbbb", "cccc"})
        self.assertEqual(a.listing_source, "kiro-cli")
        self.assertEqual(got["ffff"].title, "Official title")             # Kiro's own answer wins over the file
        self.assertEqual(got["aaaa"].title, "From files")                  # the files fill what Kiro did not list
        self.assertEqual(got["bbbb"].title, "Sub title")                   # "(no title)" falls back to the sidecar title
        self.assertTrue(got["bbbb"].subagent)                              # the subagent tag comes from the file
        self.assertFalse(got["ffff"].subagent)
        self.assertEqual(got["cccc"].cwd, "/other")
        self.assertAlmostEqual(got["ffff"].updated, 1_792_747_708.9, delta=1)
        a.list_sessions(); a.resolve("aaaa"); a.load("aaaa")
        self.assertEqual(len(calls), 1)                                    # one slow call per command, then cached
        with self.assertRaises(FileNotFoundError) as e:                    # listed by Kiro but no local copy
            a.load("cccc")
        self.assertIn("no readable local copy", str(e.exception))
        hidden = KiroAdapter(db=self.tmp / "none.db", sessions_dir=d, runner=lambda: listing, include_subagents=False)
        self.assertEqual({x.id[:4] for x in hidden.list_sessions()}, {"aaaa", "ffff", "cccc"})
        self.assertEqual(hidden.hidden_subagents, 1)

    def test_kiro_falls_back_to_files_when_the_official_listing_fails(self):
        d = self.tmp / "cli"
        self._kiro_v2(d, "aaaa1111-0000", title="From files")
        def raiser(exc):
            def run():
                raise exc
            return run
        failures = {
            "kiro-cli missing": raiser(FileNotFoundError("kiro-cli")),
            "non-zero exit": raiser(subprocess.CalledProcessError(1, "kiro-cli")),
            "timeout": raiser(subprocess.TimeoutExpired("kiro-cli", 1)),
            "not json": lambda: "something went wrong",
            "wrong shape": lambda: '{"sessions": []}',
        }
        for label, runner in failures.items():
            a = KiroAdapter(db=self.tmp / "none.db", sessions_dir=d, runner=runner)
            self.assertEqual([x.id for x in a.list_sessions()], ["aaaa1111-0000"], label)
            self.assertEqual(a.listing_source, "files", label)
        banner = "Update available\n" + self._official(("/proj", [{"sessionId": "dddd9999-0000", "source": "v2", "title": "t", "updatedAt": "2026-10-23T09:00:00.000Z", "messageCount": 1}]))
        a = KiroAdapter(db=self.tmp / "none.db", sessions_dir=d, runner=lambda: banner)
        self.assertEqual(a.listing_source if a.list_sessions() else "", "kiro-cli")  # a banner line before the JSON is tolerated
        with self.assertRaises(FileNotFoundError):  # nothing from Kiro and nothing on disk: say so
            KiroAdapter(db=self.tmp / "none.db", sessions_dir=self.tmp / "nope", runner=raiser(FileNotFoundError("kiro-cli"))).list_sessions()

    def test_kiro_custom_paths_do_not_use_the_real_listing(self):
        calls = []
        def spy(*a, **k):
            calls.append(1)
            raise FileNotFoundError
        from unittest import mock
        with mock.patch.object(kiro_module, "_run_kiro_list", spy):
            self._kiro_v2(self.tmp / "cli", "aaaa1111-0000")
            KiroAdapter(db=self.tmp / "none.db", sessions_dir=self.tmp / "cli").list_sessions()
            self.assertEqual(calls, [])                                    # custom paths: Kiro's own listing is skipped
            self.assertIs(KiroAdapter()._runner, spy)                      # default construction wires the official listing in

    def test_cli_labels_subagents_and_no_subagents_hides_them(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock
        from agent_baton import cli
        d = self.tmp / "cli"
        self._kiro_v2(d, "aaaa1111-0000", title="my own session")
        self._kiro_v2(d, "bbbb2222-0000", reason="subagent", title="ran as a subagent")
        def run(*flags):
            out, err = io.StringIO(), io.StringIO()
            def get(n, include_subagents=True, **_):
                return KiroAdapter(db=self.tmp / "none.db", sessions_dir=d, include_subagents=include_subagents)
            with mock.patch.object(cli.adapters, "get", side_effect=get), redirect_stdout(out), redirect_stderr(err):
                cli.main(["list", "--from", "kiro", "--all", *flags])
            return out.getvalue(), err.getvalue()
        out, err = run()
        self.assertIn("my own session", out)
        self.assertTrue(any("ran as a subagent" in l and l.rstrip().endswith("[subagent]") for l in out.splitlines()))
        self.assertFalse(any("my own session" in l and "[subagent]" in l for l in out.splitlines()))
        self.assertEqual(err, "")
        out, err = run("--no-subagents")
        self.assertNotIn("ran as a subagent", out)
        self.assertIn("1 subagent session(s) hidden by --no-subagents", err)
        out, _ = run("--subagents")  # the old flag is still accepted and changes nothing
        self.assertIn("ran as a subagent", out)

    def test_ambiguous_prefix(self):
        claude_fixture(self.tmp, "aaaa1111")
        claude_fixture(self.tmp, "aaaa2222")
        with self.assertRaises(AmbiguousSession):
            ClaudeCodeAdapter(self.tmp).resolve("aaaa")


class OutputTests(unittest.TestCase):
    def setUp(self):
        block_real_codex(self)
        block_real_kiro(self)
        self.tmp = Path(tempfile.mkdtemp())
        claude_fixture(self.tmp / "c")
        self.session = ClaudeCodeAdapter(self.tmp / "c").load("aaaa")

    def test_brief_contents(self):
        b = build_brief(self.session)
        self.assertIn("add a login page", b)
        self.assertIn("## Files edited", b)
        self.assertIn("- `login.py`", b)  # relativized to the session's cwd
        self.assertIn("does not exist on this machine", b.split("## How to continue")[1])  # fixture cwd is missing

    def test_redaction(self):
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", redact("key AKIAABCDEFGHIJKLMNOP"))
        self.assertNotIn("hunter22222", redact("password=hunter22222"))
        self.assertIn("password", redact("password=hunter22222"))

    def test_redaction_leaves_code_expressions(self):
        code = "Mem0(api_key=settings.mem0_api_key, token=access_token) x"
        self.assertEqual(redact(code), code)
        self.assertIn("[REDACTED]", redact("api_key='abcdef123456'"))
        self.assertIn("[REDACTED]", redact("TOKEN=Ab12Cd34Ef56"))

    def test_brief_keeps_newlines_and_long_final_answer(self):
        from agent_baton.model import Message, Session
        final = "## Hard Breaks\n- one\n- two\n" + ("detail line\n" * 300) + "LAST_LINE"
        s = Session(self.session.info, [Message("user", "text", "q"), Message("assistant", "text", "checking"),
                                        Message("assistant", "text", final)])
        b = build_brief(s)
        self.assertIn("> ## Hard Breaks\n> - one", b)
        self.assertIn("LAST_LINE", b)  # ~3.6k chars, under the 6000 final-answer budget
        self.assertEqual(b.count("[truncated]"), 0)
        s.messages[-1] = Message("assistant", "text", "x" * 7000)
        self.assertEqual(build_brief(s).count("[truncated]"), 1)

    def _session(self, msgs, cwd="/proj"):
        from agent_baton.model import Session, SessionInfo
        return Session(SessionInfo("kiro", "id123456", cwd, "t", 0), msgs)

    def test_full_is_capped_keeping_recent_tail_with_warning(self):
        from agent_baton.model import Message
        msgs = [Message("user" if i % 2 == 0 else "assistant", "text", f"message-{i} " + "pad " * 200) for i in range(200)]
        s = self._session(msgs)
        text, omitted = build_transcript(s, max_chars=20_000)
        self.assertGreater(omitted, 100)
        self.assertLess(len(text), 21_000)
        self.assertIn("message-199", text)
        self.assertNotIn("message-0 ", text)
        self.assertIn(f"{omitted} earlier messages omitted", text)
        cwd = self.tmp / "w"
        cwd.mkdir()
        plan = plan_move(s, CodexAdapter(self.tmp / "x"), "full", cwd, max_chars=20_000)
        self.assertIn("capped", plan.warnings[0])
        self.assertEqual(plan_move(s, CodexAdapter(self.tmp / "x"), "full", cwd, max_chars=10**7).warnings, [])

    def test_plan_from_kiro_todo_list_claude_and_codex(self):
        from agent_baton.model import Message
        from agent_baton.summarize import extract_plan
        def call(name, args):
            return Message("assistant", "tool_call", name, {"name": name, "args": args})
        kiro = self._session([
            call("todo_list", {"command": "create", "tasks": [{"task_description": "old"}]}),
            call("todo_list", {"command": "create", "tasks": [{"task_description": "bump"}, {"task_description": "wire config"}]}),
            call("todo_list", {"command": "complete", "completed_task_ids": ["1"], "context_update": "bumped pins"}),
        ])
        self.assertEqual(extract_plan(kiro), ([("bump", True), ("wire config", False)], "bumped pins"))
        self.assertEqual(extract_plan(self._session([call("TodoWrite", {"todos": [{"content": "a", "status": "completed"}, {"content": "b", "status": "pending"}]})]))[0],
                         [("a", True), ("b", False)])
        self.assertEqual(extract_plan(self._session([call("update_plan", {"plan": [{"step": "s", "status": "in_progress"}]})]))[0], [("s", False)])
        brief = build_brief(kiro)
        self.assertIn("Task plan (latest; 1/2 done)", brief)
        self.assertIn("- [ ] wire config", brief)
        self.assertIn("bumped pins", brief)

    def test_placeholders_filtered_and_current_task_gets_context(self):
        from agent_baton.model import Message
        s = self._session([
            Message("user", "text", "analyze the release notes"),
            Message("assistant", "text", "Proposal: implement items 1 and 2 behind a flag."),
            Message("user", "text", "yes do it"),
            Message("assistant", "text", "Tool uses were interrupted, waiting for the next user prompt"),
        ])
        b = build_brief(s)
        self.assertNotIn("Tool uses were interrupted", b)
        self.assertIn("## Current task (latest user request)\nyes do it", b)
        self.assertIn("> Proposal: implement items 1 and 2", b)  # what "yes do it" replied to
        self.assertIn("## Session started with\nanalyze the release notes", b)
        self.assertNotIn("Tool uses were interrupted", build_transcript(s)[0])

    def test_files_split_relative_nested_and_sensitive_omitted(self):
        from agent_baton.model import Message
        from agent_baton.summarize import files_touched
        def call(name, args):
            return Message("assistant", "tool_call", name, {"name": name, "args": args})
        d = self.tmp / "dir"
        d.mkdir()
        s = self._session([
            call("read", {"operations": [{"mode": "Line", "path": "/proj/a.py"}, {"path": "/proj/pca/.env"}]}),
            call("write", {"command": "strReplace", "path": "/proj/b.py"}),
            call("read", {"operations": [{"path": "/proj/b.py"}]}),   # read after edit: stays "edited"
            call("glob", {"path": str(d)}),                           # directory: skipped
            call("Read", {"file_path": "/elsewhere/prom.yml"}),
            call("Edit", {"file_path": "/proj/c.py"}),
        ])
        use = files_touched(s)
        self.assertEqual(use.edited, ["c.py", "b.py"])  # most recent first
        self.assertEqual(use.read, ["/elsewhere/prom.yml", "a.py"])
        self.assertEqual(use.omitted, 1)
        out = build_brief(s)
        self.assertNotIn(".env", out.replace("e.g. .env", ""))
        self.assertIn("1 secret-looking", out)

    def test_files_skip_dependency_dirs_and_scope_to_current_plan(self):
        from agent_baton.model import Message
        from agent_baton.summarize import files_touched
        def call(name, args):
            return Message("assistant", "tool_call", name, {"name": name, "args": args})
        s = self._session([
            call("write", {"path": "/proj/old.py"}),
            call("read", {"operations": [{"path": "/proj/old_read.py"}]}),
            call("read", {"operations": [{"path": "/proj/.venv/lib/python3.14/site-packages/pkg/mod.py"}]}),
            call("todo_list", {"command": "create", "tasks": [{"task_description": "new work"}]}),
            call("write", {"path": "/proj/new.py"}),
            call("read", {"operations": [{"path": "/proj/node_modules/x/index.js"}, {"path": "/proj/new_read.py"}]}),
            call("write", {"path": "/proj/old.py"}),  # old file touched again after the plan began: current
        ])
        use = files_touched(s)
        self.assertTrue(use.scoped)
        self.assertEqual(use.edited, ["old.py", "new.py"])
        self.assertEqual(use.read, ["new_read.py"])
        self.assertEqual(use.earlier_edited, 0)
        s.messages.insert(4, call("write", {"path": "/proj/gone.py"}))  # edited only before the plan restarts
        s.messages.insert(5, call("todo_list", {"command": "create", "tasks": [{"task_description": "second plan"}]}))
        use = files_touched(s)
        self.assertEqual(use.earlier_edited, 1)  # gone.py... counted, not listed
        out = build_brief(s)
        self.assertIn("since the current plan started", out)
        self.assertIn("1 file(s) edited earlier", out)
        self.assertNotIn("site-packages", out)
        self.assertNotIn("node_modules", out)

    def test_redaction_short_bare_passwords(self):
        self.assertNotIn("admin", redact("GF_SECURITY_ADMIN_PASSWORD=admin"))
        self.assertNotIn("hunter", redact("db_password: hunter"))
        self.assertNotIn("s3cr3t", redact('password = "s3cr3t"'))
        for safe in ("password=None", "password: ${GRAFANA_PASSWORD}", "password=get_pw()", "max_tokens=4096",
                     "password=settings.db_password", "token_manager=Manager"):
            self.assertEqual(redact(safe), safe)

    def test_claude_captures_ask_user_question_answers(self):
        write_jsonl(self.tmp / "-proj" / "askk0000.jsonl", [
            {"type": "user", "cwd": "/proj", "message": {"role": "user", "content": "start the project"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "q1", "name": "AskUserQuestion", "input": {"questions": []}}]}},
            {"type": "user", "toolUseResult": {"questions": [], "answers": {
                "Which test?": "Real move test", "Which agents?": ["Kiro", "Codex"]}},
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "q1", "content": "Your questions have been answered: ..."}]}},
        ])
        s = ClaudeCodeAdapter(self.tmp).load("askk")
        self.assertEqual([(m.role, m.kind) for m in s.messages], [("user", "text"), ("assistant", "tool_call"), ("user", "text")])
        answer = s.messages[-1].text
        self.assertIn("- Which test? → Real move test", answer)
        self.assertIn("- Which agents? → Kiro, Codex", answer)
        self.assertIn("Which test? → Real move test", build_brief(s).split("## Current task")[1])

    def test_short_reply_lists_last_substantive_request(self):
        from agent_baton.model import Message
        long_req = "please fix the redaction so that short bare passwords such as admin are caught but code is not"
        s = self._session([
            Message("user", "text", long_req),
            Message("assistant", "text", "Done. Want me to also run the tests?"),
            Message("user", "text", "yes do it"),
        ])
        cur = build_brief(s).split("## Current task (latest user request)")[1].split("##")[0]
        self.assertIn("yes do it", cur)
        self.assertIn("Last substantive user request:\n> " + long_req, cur)
        self.assertIn("> Done. Want me to also run the tests?", cur)
        long_last = self._session([Message("user", "text", "short"), Message("user", "text", long_req)])
        self.assertNotIn("Last substantive", build_brief(long_last))  # latest is not a short reply

    def test_footer_depends_on_git(self):
        from agent_baton.model import Message, Session, SessionInfo
        def footer(cwd):
            s = Session(SessionInfo("kiro", "id123456", str(cwd), "t", 0), [Message("user", "text", "hello there")])
            return build_brief(s).split("## How to continue")[1]
        repo, plain = self.tmp / "repo", self.tmp / "plain"
        (repo / ".git").mkdir(parents=True)
        (repo / "sub").mkdir()
        plain.mkdir()
        self.assertIn("git status", footer(repo))
        self.assertIn("git status", footer(repo / "sub"))  # .git found in a parent
        self.assertIn("not a git repository", footer(plain))
        self.assertNotIn("git status", footer(plain))
        self.assertIn("does not exist on this machine", footer(self.tmp / "gone"))

    def test_header_flags_missing_cwd(self):
        self.assertIn("does not exist", build_brief(self.session))  # fixture cwd '/proj'

    def test_full_to_claude_writes_native_resume(self):
        target = ClaudeCodeAdapter(self.tmp / "t")
        cwd = self.tmp / "work"
        cwd.mkdir()
        plan = plan_move(self.session, target, "full", cwd)
        apply_plan(plan, self.session, target, "full", cwd, keep_files=True)
        self.assertTrue(plan.handoff_file.exists())
        self.assertIn("claude --resume", plan.command)
        rows = [json.loads(l) for f in (self.tmp / "t").rglob("*.jsonl") for l in f.read_text().splitlines()]
        rows = [r for r in rows if r["type"] in ("user", "assistant")]  # skip the custom-title record
        self.assertEqual(rows[0]["type"], "user")
        self.assertEqual(rows[1]["parentUuid"], rows[0]["uuid"])

    def test_native_claude_session_is_capped_and_seeded_with_brief(self):
        from agent_baton.model import Message, Session
        msgs = [Message("user" if i % 2 == 0 else "assistant", "text", f"turn-{i} " + "pad " * 300) for i in range(300)]
        s = Session(self.session.info, msgs)
        target = ClaudeCodeAdapter(self.tmp / "t")
        cwd = self.tmp / "work"
        cwd.mkdir()
        plan = plan_move(s, target, "full", cwd, max_chars=30_000)
        apply_plan(plan, s, target, "full", cwd, keep_files=True)
        (f,) = list((self.tmp / "t").rglob("*.jsonl"))
        self.assertLess(f.stat().st_size, 45_000)
        rows = [json.loads(l) for l in f.read_text().splitlines()]
        rows = [r for r in rows if r["type"] in ("user", "assistant")]
        self.assertIn("Session handoff (summary)", rows[0]["message"]["content"])  # seeded with the brief
        self.assertIn("turn-299", f.read_text())
        self.assertNotIn("turn-1 ", f.read_text())
        self.assertTrue(any("earlier omitted" in n for n in plan.notes))
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        self.assertEqual(plan.handoff_file.stat().st_mode & 0o777, 0o600)
        first = json.loads(f.read_text().splitlines()[0])
        self.assertEqual(first["type"], "custom-title")
        self.assertTrue(first["customTitle"].startswith("Moved from claude: "))
        sessions = ClaudeCodeAdapter(self.tmp / "t").list_sessions()
        self.assertTrue(sessions[0].title.startswith("Moved from claude: "))

    def test_codex_native_session_calls_app_server_in_order(self):
        from agent_baton.model import Message, Session
        t = FakeTransport()
        target = CodexAdapter(self.tmp / "x", server=fake_server(t))
        msgs = [Message("user", "text", "fix it"), Message("assistant", "tool_call", "Edit(x)", {"name": "Edit", "args": {}}),
                Message("tool", "tool_result", "ok"), Message("assistant", "text", "done, key AKIAABCDEFGHIJKLMNOP")]
        made = target.write_native(Session(self.session.info, msgs), self.tmp / "work")
        self.assertEqual(t.methods(), ["initialize", "initialized", "thread/start", "thread/inject_items", "thread/name/set"])
        self.assertEqual(t.sent[2]["params"], {"cwd": str(self.tmp / "work"), "ephemeral": False})
        inject = t.sent[3]["params"]
        self.assertEqual(inject["threadId"], THREAD_ID)
        self.assertEqual([(i["role"], i["content"][0]["type"]) for i in inject["items"]],
                         [("user", "input_text"), ("assistant", "output_text")])  # tool turns merged into the assistant turn
        body = inject["items"][1]["content"][0]["text"]
        self.assertIn("[tool_call] Edit(x)", body)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", body)  # redacted before it leaves baton
        self.assertTrue(t.sent[4]["params"]["name"].startswith("Moved from claude: "))
        self.assertEqual((made.session_id, made.command), (THREAD_ID, f"codex resume {THREAD_ID}"))
        self.assertTrue(t.closed)

    def test_summarize_to_codex_creates_session_with_brief(self):
        t = FakeTransport()
        target = CodexAdapter(self.tmp / "x", server=fake_server(t))
        cwd = self.tmp / "work"
        cwd.mkdir()
        plan = plan_move(self.session, target, "summarize", cwd)
        apply_plan(plan, self.session, target, "summarize", cwd, keep_files=True)
        self.assertEqual((plan.native, plan.session_id, plan.command), (True, THREAD_ID, f"codex resume {THREAD_ID}"))
        items = next(m for m in t.sent if m.get("method") == "thread/inject_items")["params"]["items"]
        self.assertIn("Session handoff (summary)", items[0]["content"][0]["text"])
        self.assertEqual(items[1]["role"], "assistant")  # acknowledgement
        self.assertTrue(plan.handoff_file.exists())
        self.assertTrue(any("resume picker" in n for n in plan.notes))

    def test_codex_native_failure_falls_back_and_keeps_handoff_file(self):
        for failing in ("initialize", "thread/start", "thread/inject_items", "thread/name/set"):
            target = CodexAdapter(self.tmp / "x", server=fake_server(FakeTransport(fail_on=failing)))
            cwd = self.tmp / f"work-{failing.replace('/', '-')}"
            cwd.mkdir()
            plan = plan_move(self.session, target, "summarize", cwd)
            apply_plan(plan, self.session, target, "summarize", cwd)
            self.assertFalse(plan.native, failing)
            self.assertIsNone(plan.session_id)
            self.assertTrue(plan.command.startswith("codex '"), failing)
            self.assertTrue(plan.handoff_file.exists())
            self.assertTrue(any("could not create a Codex session" in n for n in plan.notes), failing)
        with self.assertRaises(NativeError):
            CodexAdapter(self.tmp / "x", server=fake_server(FakeTransport(silent=True))).write_native(self.session, self.tmp)

    def test_app_server_timeout_and_missing_codex(self):
        with self.assertRaises(CodexRpcError) as e:
            with AppServer(FakeTransport(silent=True), timeout=0.1):
                pass
        self.assertIn("no response", str(e.exception))
        with self.assertRaises(CodexRpcError) as e:
            StdioTransport(("baton-no-such-command-xyz",))
        self.assertIn("not found", str(e.exception))

    def test_stdio_transport_framing_with_a_real_child_process(self):
        child = ("import sys, json\n"
                 "for line in sys.stdin:\n"
                 "    d = json.loads(line)\n"
                 "    if 'id' in d:\n"
                 "        print(json.dumps({'method': 'noise'}), flush=True)\n"
                 "        print('not json', flush=True)\n"
                 "        print(json.dumps({'id': d['id'], 'result': {'echo': d['method']}}), flush=True)\n")
        with AppServer(StdioTransport((sys.executable, "-c", child)), timeout=10) as srv:
            self.assertEqual(srv.call("thread/list", {}), {"echo": "thread/list"})

    def test_handoff_file_removed_after_native_session_but_only_that_file(self):
        t = FakeTransport()
        target = CodexAdapter(self.tmp / "x", server=fake_server(t))
        cwd = self.tmp / "work"
        cwd.mkdir()
        plan = plan_move(self.session, target, "summarize", cwd)
        apply_plan(plan, self.session, target, "summarize", cwd)
        self.assertTrue(plan.handoff_removed)
        self.assertFalse(plan.handoff_file.exists())
        self.assertFalse((cwd / ".baton").exists())  # empty folder removed too
        self.assertTrue(any("removed the handoff file" in n for n in plan.notes))
        self.assertTrue(cwd.exists())
        # an unrelated file in .baton is never touched, and keeps the folder
        other = cwd / ".baton" / "someone-elses.md"
        other.parent.mkdir()
        other.write_text("keep me")
        plan = plan_move(self.session, target, "summarize", cwd)
        apply_plan(plan, self.session, target, "summarize", cwd)
        self.assertFalse(plan.handoff_file.exists())
        self.assertEqual(other.read_text(), "keep me")

    def test_handoff_file_kept_with_keep_files_on_failure_and_for_targets_without_native_sessions(self):
        from agent_baton.adapters import KiroAdapter
        cwd = self.tmp / "work"
        cwd.mkdir()
        ok = CodexAdapter(self.tmp / "x", server=fake_server(FakeTransport()))
        plan = plan_move(self.session, ok, "summarize", cwd)
        apply_plan(plan, self.session, ok, "summarize", cwd, keep_files=True)
        self.assertTrue(plan.handoff_file.exists())
        self.assertFalse(plan.handoff_removed)
        bad = CodexAdapter(self.tmp / "x", server=fake_server(FakeTransport(fail_on="thread/inject_items")))
        plan = plan_move(self.session, bad, "summarize", cwd)
        apply_plan(plan, self.session, bad, "summarize", cwd)  # session creation failed: file is the fallback
        self.assertTrue(plan.handoff_file.exists())
        kiro = KiroAdapter(db=self.tmp / "none.db", sessions_dir=self.tmp / "none")
        plan = plan_move(self.session, kiro, "summarize", cwd)
        apply_plan(plan, self.session, kiro, "summarize", cwd)  # Kiro has no native session: the file is the handoff
        self.assertTrue(plan.handoff_file.exists())
        self.assertFalse(plan.handoff_removed)

    def test_cli_removes_handoff_by_default_and_keep_files_flag(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from agent_baton import cli
        claude_fixture(self.tmp / "c")
        src = ClaudeCodeAdapter(self.tmp / "c")
        work = self.tmp / "work"
        work.mkdir()
        info = src.list_sessions()[0]
        info.cwd = str(work)
        def run(*extra):
            target = CodexAdapter(self.tmp / "x", server=fake_server(FakeTransport()))
            out = io.StringIO()
            with mock.patch.object(cli.adapters, "get", side_effect=lambda n, *_, **__: src if n == "claude" else target), \
                    mock.patch.object(src, "resolve", return_value=info), redirect_stdout(out):
                cli.main(["move", "--from", "claude", "--to", "codex", "--session", "aaaa", "-y", *extra])
            return out.getvalue()
        out = run()
        self.assertIn("removed once that session exists", out)
        self.assertIn("removed the handoff file", out)
        self.assertEqual(list(work.rglob("*.md")), [])
        out = run("--keep-files")
        self.assertNotIn("removed once", out)
        self.assertEqual(len(list(work.rglob("*.md"))), 1)

    # ---- Codex's official importer (Claude -> Codex --full) ----

    def _import_setup(self, outcome="success", listed=True, detect_error=False, src_cwd=None):
        """A Claude session, and a Codex adapter whose app-server is the fake importer."""
        transports = []
        info = self.session.info
        if src_cwd:
            info.cwd = str(src_cwd)
        sessions = [{"cwd": "/other", "path": "/elsewhere/other.jsonl", "title": "o"}]
        if listed:
            sessions.insert(0, {"cwd": info.cwd, "path": info.source_path, "title": "t"})
        target = CodexAdapter(self.tmp / "x", server=importer_server(transports, sessions, outcome=outcome, detect_error=detect_error))
        target.import_timeout = 0.8
        cwd = self.tmp / "work"
        cwd.mkdir(exist_ok=True)
        return target, transports, cwd

    def test_claude_to_codex_full_imports_only_the_selected_session(self):
        work = self.tmp / "proj-exists"
        work.mkdir()
        target, transports, cwd = self._import_setup(src_cwd=work)
        plan = plan_move(self.session, target, "full", cwd)
        apply_plan(plan, self.session, target, "full", cwd)
        (t,) = transports
        self.assertEqual(t.methods(), ["initialize", "initialized", "externalAgentConfig/detect", "externalAgentConfig/import"])
        detect = t.sent[2]["params"]
        self.assertEqual((detect["includeHome"], detect["cwds"]), (True, [str(work)]))
        sent = t.sent[3]["params"]
        self.assertEqual(sent["source"], "baton")
        (item,) = sent["migrationItems"]  # only the SESSIONS item: the MCP config in the detect result is not sent
        self.assertEqual(item["itemType"], "SESSIONS")
        self.assertEqual(list(item["details"]), ["sessions"])
        self.assertEqual([x["path"] for x in item["details"]["sessions"]], [self.session.info.source_path])  # not the other one
        self.assertEqual((plan.native, plan.session_id, plan.command), (True, IMPORTED_THREAD, f"codex resume {IMPORTED_THREAD}"))
        self.assertTrue(any("official importer" in n for n in plan.notes))
        self.assertNotIn("thread/start", t.methods())  # baton's own injection was not used
        self.assertFalse(plan.handoff_file.exists())  # the import brought everything in, so the file is removed
        self.assertTrue(t.closed)

    def test_import_keeps_handoff_file_with_keep_files_and_omits_cwds_when_the_folder_is_gone(self):
        target, transports, cwd = self._import_setup()  # fixture folder /proj does not exist
        plan = plan_move(self.session, target, "full", cwd)
        apply_plan(plan, self.session, target, "full", cwd, keep_files=True)
        self.assertNotIn("cwds", transports[0].sent[2]["params"])
        self.assertTrue(plan.handoff_file.exists())

    def test_import_completion_arriving_before_the_response_is_not_lost(self):
        target, transports, cwd = self._import_setup(outcome="early")
        plan = plan_move(self.session, target, "full", cwd)
        apply_plan(plan, self.session, target, "full", cwd)
        self.assertEqual(plan.session_id, IMPORTED_THREAD)

    def test_import_falls_back_to_baton_injection_and_says_why(self):
        cases = [
            ("not listed", dict(listed=False), "did not list this session"),
            ("failure reported", dict(outcome="failure"), "reported a failure"),
            ("never completes", dict(outcome="silent"), "not received within"),
            ("detect errors", dict(detect_error=True), "invalid MCP config"),
        ]
        for label, kw, expected in cases:
            target, transports, cwd = self._import_setup(**kw)
            plan = plan_move(self.session, target, "full", cwd)
            apply_plan(plan, self.session, target, "full", cwd)
            self.assertTrue(any(expected in n for n in plan.notes), f"{label}: {plan.notes}")
            self.assertTrue(any("creating the session from the transcript instead" in n for n in plan.notes), label)
            self.assertEqual((plan.native, plan.session_id), (True, THREAD_ID), label)  # created by thread/start + inject_items
            self.assertEqual(transports[-1].methods()[-3:], ["thread/start", "thread/inject_items", "thread/name/set"], label)

    def test_importer_is_only_tried_for_full_moves_from_claude(self):
        target, transports, cwd = self._import_setup()
        plan = plan_move(self.session, target, "summarize", cwd)
        apply_plan(plan, self.session, target, "summarize", cwd)
        self.assertFalse(any("externalAgentConfig" in m for t in transports for m in t.methods()))  # summary: baton's brief
        from agent_baton.model import Session, SessionInfo
        kiro = Session(SessionInfo("kiro", "k1", "/p", "t", 0, "/some/kiro/file"), self.session.messages)
        plan = plan_move(kiro, target, "full", cwd)
        apply_plan(plan, kiro, target, "full", cwd)
        self.assertFalse(any("externalAgentConfig" in m for t in transports for m in t.methods()))  # not a Claude session
        no_path = SessionInfo("claude", "c1", "/p", "t", 0)
        self.assertIsNone(target.import_native(no_path, cwd))  # nothing to import from
        self.assertEqual(CursorAdapter(self.tmp).import_sources, frozenset())  # other targets have no importer

    def test_cli_announces_the_importer_only_for_claude_to_codex_full(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from agent_baton import cli
        claude_fixture(self.tmp / "c2")
        src = ClaudeCodeAdapter(self.tmp / "c2")
        info = src.list_sessions()[0]
        info.cwd = str(self.tmp)
        target = CodexAdapter(self.tmp / "x")
        def run(*mode):
            out = io.StringIO()
            with mock.patch.object(cli.adapters, "get", side_effect=lambda n, *_, **__: src if n == "claude" else target), \
                    mock.patch.object(src, "resolve", return_value=info), redirect_stdout(out):
                cli.main(["move", "--from", "claude", "--to", "codex", "--session", "aaaa", "--dry-run", *mode])
            return out.getvalue()
        self.assertIn("official importer", run("--full"))
        self.assertNotIn("official importer", run("--summarize"))

    def test_app_server_wait_for_sees_notifications_received_earlier(self):
        with AppServer(FakeTransport(silent=False), timeout=0.5) as srv:
            self.assertEqual(srv.wait_for("mcpServer/startupStatus/updated"), {})  # arrived during initialize
            with self.assertRaises(CodexRpcError) as e:
                srv.wait_for("never/sent", timeout=0.3)
            self.assertIn("not received within", str(e.exception))

    def test_claude_sessions_record_their_file_path(self):
        claude_fixture(self.tmp / "c3")
        a = ClaudeCodeAdapter(self.tmp / "c3")
        expected = str(self.tmp / "c3" / "-proj" / "aaaa1111-0000.jsonl")
        self.assertEqual(a.list_sessions()[0].source_path, expected)
        self.assertEqual(a.load("aaaa").info.source_path, expected)

    # ---- --no-native: only the handoff file and a launch command ----

    def test_no_native_writes_only_the_handoff_file_for_every_target(self):
        transports = []
        codex = CodexAdapter(self.tmp / "x", server=importer_server(transports, []))
        claude = ClaudeCodeAdapter(self.tmp / "t")
        for target, launch in ((claude, "claude '"), (codex, "codex '")):
            for mode in ("summarize", "full"):
                cwd = self.tmp / f"w-{target.name}-{mode}"
                cwd.mkdir()
                plan = plan_move(self.session, target, mode, cwd)
                apply_plan(plan, self.session, target, mode, cwd, no_native=True)
                self.assertTrue(plan.command.startswith(launch), (target.name, mode))
                self.assertEqual((plan.native, plan.session_id), (False, None))
                self.assertTrue(plan.handoff_file.exists())               # the file is the whole handoff, so it stays
                self.assertFalse(plan.handoff_removed)
                if mode == "full":
                    self.assertTrue(any("--no-native" in n for n in plan.notes))
        self.assertFalse((self.tmp / "t").exists())                        # no Claude session file was written
        self.assertEqual(transports, [])                                   # Codex's app-server was never started, importer included

    def test_cli_no_native_flag(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from agent_baton import cli
        claude_fixture(self.tmp / "c4")
        src = ClaudeCodeAdapter(self.tmp / "c4")
        work = self.tmp / "work4"
        work.mkdir()
        info = src.list_sessions()[0]
        info.cwd = str(work)
        transports = []
        target = CodexAdapter(self.tmp / "x", server=importer_server(transports, []))
        def run(*flags):
            out = io.StringIO()
            with mock.patch.object(cli.adapters, "get", side_effect=lambda n, *_, **__: src if n == "claude" else target), \
                    mock.patch.object(src, "resolve", return_value=info), redirect_stdout(out):
                cli.main(["move", "--from", "claude", "--to", "codex", "--session", "aaaa", "-y", *flags])
            return out.getvalue()
        out = run("--full", "--no-native")
        self.assertIn("--no-native: no codex session will be created", out)
        self.assertNotIn("will also create", out)
        self.assertNotIn("official importer", out)
        self.assertIn("no session id yet", out)
        self.assertIn("codex '", out)
        self.assertEqual(transports, [])
        self.assertEqual(len(list(work.rglob("*.md"))), 1)                 # the handoff file stays

    def test_summarize_to_claude_creates_session_with_id(self):
        target = ClaudeCodeAdapter(self.tmp / "t")
        cwd = self.tmp / "work"
        cwd.mkdir()
        plan = plan_move(self.session, target, "summarize", cwd)
        apply_plan(plan, self.session, target, "summarize", cwd)
        (f,) = list((self.tmp / "t").rglob("*.jsonl"))
        self.assertEqual(plan.session_id, f.stem)
        self.assertEqual(plan.command, f"claude --resume {f.stem}")
        rows = [json.loads(l) for l in f.read_text().splitlines() if '"custom-title"' not in l]
        self.assertEqual([r["type"] for r in rows], ["user", "assistant"])  # brief, then an acknowledgement
        self.assertIn("Session handoff (summary)", rows[0]["message"]["content"])
        self.assertEqual(ClaudeCodeAdapter(self.tmp / "t").load(f.stem[:8]).messages[0].role, "user")  # loadable

    def test_cli_prints_new_session_id_only_when_one_exists(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from agent_baton import cli
        claude_fixture(self.tmp / "c")
        src = ClaudeCodeAdapter(self.tmp / "c")
        work = self.tmp / "work"
        work.mkdir()
        info = src.list_sessions()[0]
        info.cwd = str(work)
        def run(target):
            out = io.StringIO()
            with mock.patch.object(cli.adapters, "get", side_effect=lambda n, *_, **__: src if n == "claude" else target), \
                    mock.patch.object(src, "resolve", return_value=info), redirect_stdout(out):
                cli.main(["move", "--from", "claude", "--to", "codex" if isinstance(target, CodexAdapter) else "kiro",
                          "--session", "aaaa", "-y"])
            return out.getvalue()
        codex_out = run(CodexAdapter(self.tmp / "x", server=fake_server(FakeTransport())))
        self.assertIn("will also create a new codex session", codex_out)
        self.assertIn(f"new codex session id: {THREAD_ID}", codex_out)
        self.assertIn(f"codex resume {THREAD_ID}", codex_out)
        self.assertNotIn("no session id yet", codex_out)
        failed = run(CodexAdapter(self.tmp / "x", server=fake_server(FakeTransport(fail_on="thread/start"))))
        self.assertIn("no session id yet", failed)  # falls back to the launch command
        self.assertIn("could not create a Codex session", failed)

    def test_move_into_cursor_gives_handoff_file_and_launch_command(self):
        target = CursorAdapter(self.tmp / "nochats")
        cwd = self.tmp / "work"
        cwd.mkdir()
        plan = plan_move(self.session, target, "summarize", cwd)
        self.assertTrue(plan.command.startswith("cursor-agent '"))
        apply_plan(plan, self.session, target, "summarize", cwd)
        self.assertFalse(plan.native)
        self.assertIsNone(plan.session_id)
        self.assertTrue(plan.handoff_file.exists())       # no native session, so the file is the handoff
        plan = plan_move(self.session, target, "full", cwd)
        apply_plan(plan, self.session, target, "full", cwd)
        self.assertTrue(any("no importable session format" in n for n in plan.notes))

    def test_summarize_to_codex_falls_back_to_brief_and_launch_command(self):
        target = CodexAdapter(self.tmp / "x", server=fake_server(FakeTransport(fail_on="thread/start")))
        cwd = self.tmp / "work"
        cwd.mkdir()
        plan = plan_move(self.session, target, "summarize", cwd)
        apply_plan(plan, self.session, target, "summarize", cwd)
        self.assertTrue(plan.command.startswith("codex "))
        self.assertFalse((self.tmp / "x").exists())
        text, omitted = build_transcript(self.session)
        self.assertIn("login", text)
        self.assertEqual(omitted, 0)


class GeminiTests(unittest.TestCase):
    def setUp(self):
        block_real_codex(self)
        block_real_kiro(self)
        self.tmp = Path(tempfile.mkdtemp())

    def _session_for_gemini(self):
        """A real-looking source session (Claude's), to move into Gemini."""
        claude_fixture(self.tmp / "c")
        return ClaudeCodeAdapter(self.tmp / "c").load("aaaa")

    def _needs_oracle(self):
        if not gemini_oracle():
            self.skipTest("Gemini CLI and Node are not installed: Gemini's own loader cannot be run")

    # ---- reading: Gemini's replay rules ----

    def test_replay_applies_patches_removals_reordering_and_rewinds(self):
        paths = gemini_fixtures(self.tmp / "f")
        def ids(name):
            return [m["id"] for m in read_session_file(paths[name])[1]]
        self.assertEqual(ids("patch updates remove order"), ["a", "d", "b"])   # c removed, d and b moved to the end in that order
        self.assertEqual(read_session_file(paths["patch updates remove order"])[1][2]["content"], "B patched")
        self.assertEqual(ids("rewind found"), ["u1", "g1", "u3"])               # u2 and everything after it dropped
        self.assertEqual(ids("rewind unknown clears all"), ["u9"])              # an unknown id clears everything before it
        self.assertEqual(ids("same id again replaces in place"), ["u1", "g1"])
        self.assertEqual(read_session_file(paths["same id again replaces in place"])[1][0]["content"], "v2")
        meta, _ = read_session_file(paths["set metadata and info messages"])
        self.assertEqual(meta["summary"], "A tidy summary")
        self.assertEqual(len(read_session_file(paths["blank and garbage lines"])[1]), 2)
        self.assertEqual(ids("legacy json"), ["m1", "m2"])
        calls = read_session_file(paths["tool result patch"])[1][1]["toolCalls"]
        self.assertIn("result", calls[0])
        self.assertNotIn("result", calls[1])  # only the call named in the patch got a result

    def test_real_session_shape_title_folder_and_injected_context(self):
        """A session as Gemini 0.65.0 really writes it: a named project folder with .project_root, an injected
        <session_context> first message, and the user's own text second."""
        root = self.tmp / "gemini-tmp"
        project = self.tmp / "my-project"
        project.mkdir()
        chats = root / "my-project" / "chats"                                           # the project's name, not a hash
        chats.mkdir(parents=True)
        (root / "my-project" / ".project_root").write_text(str(project) + "\n")
        paths = gemini_fixtures(self.tmp / "f")
        (chats / "session-2026-10-27T16-42-00c7375b.jsonl").write_text(paths["shaped like a real 0.65.0 session"].read_text())
        a = GeminiAdapter(root)
        (info,) = a.list_sessions()                                                     # run from a different folder: no hash match
        self.assertEqual(info.cwd, str(project))                                        # from .project_root
        self.assertEqual(info.title, "hello")                                           # not the injected context
        self.assertEqual(info.id, "00000000-0000-4000-8000-0000000000aa")
        s = a.load("0000")
        self.assertEqual([(m.role, m.text) for m in s.messages], [("user", "hello")])   # the injected block is not conversation
        from agent_baton.summarize import build_brief
        brief = build_brief(s)
        self.assertIn("## Current task (latest user request)\nhello", brief)
        self.assertNotIn("session_context", brief)

    def test_replay_matches_geminis_own_loader_on_every_fixture(self):
        self._needs_oracle()
        paths = gemini_fixtures(self.tmp / "f")
        oracle = run_gemini_loader(list(paths.values()))
        for name, path in paths.items():
            theirs = oracle[str(path)]
            self.assertIsNotNone(theirs, name)
            ours = read_session_file(path)[1]
            self.assertEqual([(m["id"], m["type"], m["content"]) for m in ours],
                             [(m["id"], m["type"], m["content"]) for m in theirs["messages"]], name)
            self.assertEqual([[t.get("result") for t in (m.get("toolCalls") or [])] for m in ours],
                             [[t.get("result") for t in (m.get("toolCalls") or [])] for m in theirs["messages"]], name)
        self.assertEqual(read_session_file(paths["set metadata and info messages"])[0]["summary"],
                         oracle[str(paths["set metadata and info messages"])]["summary"])

    def test_list_and_load_sessions(self):
        root = self.tmp / "gemini-tmp"
        proj, other = self.tmp / "project", self.tmp / "other-project"
        proj.mkdir(); other.mkdir()
        here = project_hash(os.path.realpath(proj))
        def put(folder, name, records, mtime):
            d = root / folder / "chats"
            d.mkdir(parents=True, exist_ok=True)
            f = d / name
            f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
            os.utime(f, (mtime, mtime))
        put(here, "session-100-aaaa1111.jsonl", [
            gemini_meta("aaaa1111-0000-4000-8000-000000000001", projectHash=here),
            gm("u1", "user", "fix the parser"),
            gm("g1", "gemini", "reading it", toolCalls=[{"id": "t1", "name": "read_file", "args": {"absolute_path": "/p/a.py"}}]),
            {"$patch": {"id": "g1", "toolCalls": [{"id": "t1", "result": [{"functionResponse": {"response": {"output": "file body"}}}]}]}},
            gm("i1", "info", "not part of the conversation"),
            gm("u2", "user", [{"text": "now"}, {"text": "fix the tests"}]),
            gm("g2", "gemini", "done"),
            {"$set": {"summary": "Fix the parser"}}], 1_790_000_300)
        put("by-directories", "session-200-bbbb2222.jsonl", [
            gemini_meta("bbbb2222-0000-4000-8000-000000000002", projectHash=project_hash(other), directories=[str(other)]),
            gm("u1", "user", "a question in another project")], 1_790_000_200)
        put("unknown-project", "session-300-cccc3333.jsonl", [
            gemini_meta("cccc3333-0000-4000-8000-000000000003", projectHash="f" * 64),
            gm("u1", "user", "a question nobody can place")], 1_790_000_100)
        put(here, "abcd.jsonl", [gemini_meta("dddd4444-0000-4000-8000-000000000004"), gm("u1", "user", "a subagent's own chat")], 1_790_000_050)
        (root / here / "chats" / "session-400-eeee5555.json").write_text(json.dumps(
            {**gemini_meta("eeee5555-0000-4000-8000-000000000005", projectHash=here), "messages": [gm("m1", "user", "older single-file format")]}))
        os.utime(root / here / "chats" / "session-400-eeee5555.json", (1_790_000_000, 1_790_000_000))
        from unittest import mock
        with mock.patch.object(gemini_module.os, "getcwd", return_value=str(proj)):
            a = GeminiAdapter(root)
            listed = a.list_sessions()
        self.assertEqual([s.id[:4] for s in listed], ["aaaa", "bbbb", "cccc", "eeee"])  # newest first; the subagent file is not a session
        by = {s.id[:4]: s for s in listed}
        self.assertEqual(by["aaaa"].title, "Fix the parser")                          # the saved summary wins
        self.assertEqual(by["eeee"].title, "older single-file format")                # otherwise the first user message
        self.assertEqual(by["aaaa"].cwd, os.path.realpath(proj))                      # found by hashing the current folder
        self.assertEqual(by["bbbb"].cwd, str(other))                                  # found through the recorded directories
        self.assertEqual(by["cccc"].cwd, "")                                          # no way to name this project
        self.assertTrue(by["aaaa"].source_path.endswith("session-100-aaaa1111.jsonl"))
        s = a.load("aaaa")
        self.assertEqual([(m.role, m.kind) for m in s.messages], [
            ("user", "text"), ("assistant", "text"), ("assistant", "tool_call"), ("tool", "tool_result"),
            ("user", "text"), ("assistant", "text")])                                 # the info message is dropped
        self.assertEqual(s.messages[3].text, "file body")                             # functionResponse output
        self.assertEqual(s.messages[2].meta["args"], {"absolute_path": "/p/a.py"})
        self.assertEqual(s.messages[4].text, "now\nfix the tests")                    # parts joined into one text
        from agent_baton.summarize import files_touched
        self.assertEqual(files_touched(s).read, ["/p/a.py"])                          # Gemini's absolute_path key

    # ---- writing: a session file for `gemini --session-file` ----

    def test_target_writes_a_session_file_and_prints_the_import_command(self):
        session = self._session_for_gemini()
        target = GeminiAdapter(self.tmp / "g")
        cwd = self.tmp / "work"
        cwd.mkdir()
        plan = plan_move(session, target, "summarize", cwd)
        self.assertEqual(plan.handoff_file, cwd / ".baton" / "claude-aaaa1111-summarize.gemini.json")
        self.assertEqual(plan.command, "gemini --session-file .baton/claude-aaaa1111-summarize.gemini.json")
        apply_plan(plan, session, target, "summarize", cwd)
        self.assertEqual((plan.native, plan.session_id, plan.handoff_removed), (False, None, False))  # no id until Gemini imports it
        self.assertTrue(plan.handoff_file.exists())                                   # kept: the command reads it later
        self.assertEqual(plan.handoff_file.stat().st_mode & 0o777, 0o600)
        self.assertTrue(any("--session-file" in n for n in plan.notes))
        rec = json.loads(plan.handoff_file.read_text())
        self.assertEqual(rec["projectHash"], project_hash(os.path.realpath(cwd)))
        self.assertRegex(rec["sessionId"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertTrue(rec["summary"].startswith("Moved from claude: "))
        self.assertEqual([m["type"] for m in rec["messages"]], ["user", "gemini"])    # the brief, then an acknowledgement
        self.assertIn("Session handoff (summary)", rec["messages"][0]["content"])
        self.assertIn("Handoff received", rec["messages"][1]["content"])

    def test_target_file_in_full_mode_and_redaction(self):
        from agent_baton.model import Message, Session
        msgs = [Message("user" if i % 2 == 0 else "assistant", "text", f"turn-{i} " + "pad " * 200) for i in range(120)]
        msgs.append(Message("assistant", "text", "the key is AKIAABCDEFGHIJKLMNOP"))
        session = Session(self._session_for_gemini().info, msgs)
        target = GeminiAdapter(self.tmp / "g")
        cwd = self.tmp / "work"
        cwd.mkdir()
        plan = plan_move(session, target, "full", cwd, max_chars=20_000)
        self.assertTrue(any("session file capped" in w and "earlier messages omitted" in w for w in plan.warnings))
        rec = json.loads(plan.content)
        types = [m["type"] for m in rec["messages"]]
        self.assertEqual(types[0], "user")
        self.assertTrue(all(a != b for a, b in zip(types, types[1:])))               # strictly alternating
        self.assertIn("Session handoff (summary)", rec["messages"][0]["content"])     # the brief first, then the recent tail
        text = plan.content
        self.assertIn("turn-119", text)
        self.assertNotIn("turn-1 ", text)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", text)                                # redacted before it is written
        self.assertLess(len(text), 40_000)

    def test_session_file_is_accepted_by_geminis_own_loader(self):
        self._needs_oracle()
        session = self._session_for_gemini()
        target = GeminiAdapter(self.tmp / "g")
        cwd = self.tmp / "work"
        cwd.mkdir()
        for mode in ("summarize", "full"):
            plan = plan_move(session, target, mode, cwd)
            apply_plan(plan, session, target, mode, cwd)
            theirs = run_gemini_loader([plan.handoff_file])[str(plan.handoff_file)]
            self.assertIsNotNone(theirs, mode)
            ours = json.loads(plan.content)
            self.assertEqual([(m["type"], m["content"]) for m in theirs["messages"]],
                             [(m["type"], m["content"]) for m in ours["messages"]], mode)
            self.assertEqual(theirs["summary"], ours["summary"], mode)
            types = [m["type"] for m in theirs["messages"]]
            self.assertEqual(types[:2], ["user", "gemini"], mode)                       # what --session-file keeps: user and gemini turns
            self.assertTrue(all(a != b for a, b in zip(types, types[1:])), mode)

    def test_cli_moves_into_gemini_and_keeps_the_file(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from agent_baton import cli
        claude_fixture(self.tmp / "c2")
        src = ClaudeCodeAdapter(self.tmp / "c2")
        work = self.tmp / "work2"
        work.mkdir()
        info = src.list_sessions()[0]
        info.cwd = str(work)
        target = GeminiAdapter(self.tmp / "g")
        for extra in ([], ["--no-native"], ["--full"]):
            out = io.StringIO()
            with mock.patch.object(cli.adapters, "get", side_effect=lambda n, *_, **__: src if n == "claude" else target), \
                    mock.patch.object(src, "resolve", return_value=info), redirect_stdout(out):
                cli.main(["move", "--from", "claude", "--to", "gemini", "--session", "aaaa", "-y", *extra])
            text = out.getvalue()
            self.assertIn("no session id yet: gemini creates one when you start it", text, extra)
            self.assertIn("gemini --session-file .baton/claude-aaaa1111-", text, extra)
            self.assertIn("is kept, because gemini reads it", text, extra)
            self.assertNotIn("will also create", text, extra)
        self.assertEqual(len(list((work / ".baton").glob("*.gemini.json"))), 2)       # summarize and full; --no-native reused the first

    def test_gemini_is_registered_and_listed_by_the_cli(self):
        from agent_baton import adapters
        self.assertIn("gemini", adapters.ADAPTERS)
        a = GeminiAdapter(self.tmp / "none")
        self.assertEqual((a.native, a.session_file, a.import_sources), (False, True, frozenset()))
        self.assertEqual(a.list_sessions(), [])                                       # no Gemini folder at all is fine

    def test_gemini_tool_names_in_the_brief(self):
        from agent_baton.model import Message, Session, SessionInfo
        from agent_baton.summarize import extract_plan, files_touched
        def call(name, args):
            return Message("assistant", "tool_call", name, {"name": name, "args": args})
        s = Session(SessionInfo("gemini", "g1", "/proj", "t", 0), [
            call("write_todos", {"todos": [{"description": "wire it", "status": "completed"}, {"description": "test it", "status": "pending"}]}),
            call("write_file", {"file_path": "/proj/new.py", "content": "x"}),
            call("replace", {"file_path": "/proj/old.py", "old_string": "a", "new_string": "b"}),
            call("read_file", {"absolute_path": "/proj/readme.md"})])
        use = files_touched(s)
        self.assertEqual(sorted(use.edited), ["new.py", "old.py"])
        self.assertEqual(use.read, ["readme.md"])
        self.assertEqual(extract_plan(s)[0], [("wire it", True), ("test it", False)])


class ReleaseTests(unittest.TestCase):
    def test_the_version_is_the_same_everywhere(self):
        import re
        import agent_baton
        from agent_baton.codex_rpc import CLIENT_INFO
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        if not pyproject.exists():
            self.skipTest("pyproject.toml is not next to the tests (running from an installed package)")
        declared = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.M).group(1)
        self.assertEqual(agent_baton.__version__, declared)
        self.assertEqual(CLIENT_INFO["version"], declared)
        self.assertRegex(declared, r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
