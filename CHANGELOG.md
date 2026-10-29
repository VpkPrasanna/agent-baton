# Changelog

All notable changes to baton. Versions follow semantic versioning: while the version is 0.x, a minor bump (0.2.0)
adds features and a patch bump (0.2.1) fixes bugs.

## 0.2.2 - 2026-10-29

### Added
- **Kiro IDE as a source** (`--from kiro-ide`): reads the IDE's newer `~/.kiro/sessions/<hash>/sess_*/` chats (with tool
  calls and results) and its older `workspace-sessions` chats, read-only. Workflow and runtime turns the IDE puts in the
  user's side are left out. It is source only: `--to kiro-ide` is rejected, because the IDE has no way to resume a session.

### Fixed
- Codex: a thread baton made showed an empty transcript, because history added through `thread/inject_items` is
  model-only. Baton now stages the conversation as a temporary Claude-format file in `~/.claude/projects/`, imports it
  with Codex's importer, and removes the file, so Codex shows the messages and the model still has them. It falls back to
  the old path (and says so) when the project folder is gone or the importer does not take the file.

## 0.2.1 - 2026-10-28

### Added
- `--latest` on `move`: take the newest session in this directory (or in any directory with `--all`) without the picker.
- `--version`.
- `--no-kiro-cli` on `list` and `move`, and the `BATON_NO_KIRO_CLI=1` variable: skip Kiro's own 2 to 4 second listing and
  read Kiro's files only.
- A warning when a kept handoff file is inside a git repository that does not ignore `.baton/`, with the one-line fix.

### Changed
- Handoff files never overwrite an earlier one: a repeat move writes `...-2`, `...-3`, and the printed launch command names
  the new file.
- The launch prompt only tells the target to check the repo with git when the folder is a git repository.
- Text a harness injected is now recognised by shape (a message made only of snake_case tagged blocks such as
  `<recommended_plugins>…</recommended_plugins>`), not only by a fixed list of prefixes. This fixes Codex sessions whose title
  and brief started with such a block. A message that mixes a block with the person's own words is kept.

## 0.2.0 - 2026-10-27

### Added
- **Gemini CLI** as a source and as a target, which makes 20 move directions across Kiro CLI, Claude Code, Codex,
  Cursor CLI and Gemini CLI.
  - Reading replays Gemini's session records the way Gemini's own loader does (message patches, removals,
    reordering, both kinds of rewind, metadata updates), and also reads the older single-file format.
  - As a target, baton writes a JSON session file and prints `gemini --session-file <file>`. Gemini's own importer then
    creates the new session, so baton never writes into Gemini's store. The file is kept, because the command reads it.
- Gemini's tools (`write_file`, `replace`, `read_file`, `write_todos`) now count in the brief's edited files, read files
  and task plan.
- A test that the version in `pyproject.toml`, `agent_baton.__version__` and the Codex client info agree.

### Fixed
- Gemini: the `<session_context>` block Gemini injects as the first user message of every session is no longer used as
  the session's title or the brief's current task.
- Gemini: the project folder is read from Gemini's `.project_root` file (Gemini 0.65.0 names project folders after the
  project, not a hash), so a session lists its folder from any directory.

### Documentation
- README: a table of all 20 directions with how well each was tested (Live + model, Live, Dry run, Tests only), what was
  verified for Gemini and what was not, an updated architecture diagram, and the correct package name in the diagram footer.

### Notes
- Every direction involving Gemini is labelled Dry run or Tests only. A real `gemini --session-file` import was run and
  produced a native Gemini session, but Gemini's model never answered from the imported history on the test machine, so
  that part is unproven.

## 0.1.0 - 2026-10-26

First release, published as `baton-agents`.

- Reads one session from Kiro CLI, Claude Code, Codex or Cursor CLI and moves it into any other of them.
- Creates a real, resumable session in Claude Code (a session file) and in Codex (a thread through `codex app-server`),
  and prints its id and resume command. Claude Code to Codex with `--full` uses Codex's official importer first.
- Kiro and Cursor get a handoff file and a launch command, because neither has a safe way to import a session.
- Redacts secrets, leaves out `.env`-style files, never modifies a source session, and asks before writing.
- `--summarize` (default) builds a brief; `--full` sends a capped transcript. `--dry-run`, `--keep-files`,
  `--no-native`, `--no-subagents`, `--max-chars`.
- Kiro: uses `kiro-cli chat --list-sessions` first and reads the files too; subagent sessions are shown and labelled.
