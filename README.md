# codex-subagent-for-claude

English | [简体中文](README.zh-CN.md)

Use Codex as a subagent inside Claude Code: Claude delegates a task, Codex works on it in the background, and the result comes back into Claude's conversation on its own.

Two Python files, no third-party dependencies. Currently verified on Windows 11 only.

## Features

- **Async dispatch**: `codex_spawn` returns immediately and Claude carries on. Multiple tasks run in parallel inside one long-lived Codex process, no cold start per task
- **Results delivered automatically**: when Codex finishes, the result shows up in the conversation on Claude's next action, no polling. Claude collects any outstanding results before ending its turn
- **Mid-flight control**: `codex_steer` sends a follow-up instruction and Codex changes course after its current step; `codex_interrupt` stops a task and keeps what's done; pass `thread_id` to continue a conversation
- **Code review**: `codex_review` runs Codex's built-in review mode — does not modify files, defect-focused, with file and line numbers
- **Per-window isolation**: each Claude Code window gets its own Codex process
- **Independent model settings**: subagents default to `gpt-6.1-sol` / reasoning effort `high` / standard service tier, unaffected by the Codex desktop chat settings; change the defaults via environment variables or override per task (GPT-6.1 Sol / GPT-6 Astra / GPT-6 Sol / GPT-6 Luna)

## How it works

```
Claude Code ──MCP──> server.py ──JSON-RPC──> codex app-server (long-lived child process)
Claude Code ──hook──> hook.py: injects finished results back into the session that dispatched them
```

## Requirements

- Windows 11, Python 3.10+ (`python` on PATH), Claude Code
- A logged-in Codex. By default the `codex.exe` bundled with the Codex desktop app is used (under `%LOCALAPPDATA%\OpenAI\Codex\bin\`, updated with the app); the app itself does not need to be running. The npm package `@openai/codex` also works, or point `CODEX_SUB_BIN` at any binary. Both share the login and config in `~/.codex`

## Install

Clone anywhere; `<install-dir>` below stands for that path. You can also just hand these steps to Claude or Codex.

1. Register the MCP server:

```bash
claude mcp add codex-sub -s user -- python "<install-dir>\server.py"
```

2. Merge the hooks into `~/.claude/settings.json` (escape backslashes as `\\` in JSON):

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python \"<install-dir>\\hook.py\" UserPromptSubmit", "timeout": 10}]}],
    "PostToolUse":      [{"hooks": [{"type": "command", "command": "python \"<install-dir>\\hook.py\" PostToolUse", "timeout": 10}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "python \"<install-dir>\\hook.py\" Stop", "timeout": 10}]}]
  }
}
```

3. Optional: add `"mcp__codex-sub"` to `permissions.allow`, otherwise every call asks for confirmation.

Restart your Claude Code session. To try it without touching global config: `claude --plugin-dir "<install-dir>"`.

## Usage

Just talk to Claude; it knows when to delegate:

> Hand the auth module refactor to Codex. Meanwhile fix the API tests yourself, then merge when it's done.

> Have Codex review what I just changed.

> Run two Codex tasks in parallel: one fixes `add`, one fixes `mul`, each in its own worktree.

| Tool | Purpose |
|---|---|
| `codex_spawn` | Dispatch a task. `wait_s` waits a while first, `thread_id` continues a thread, `worktree` runs in a separate branch, `model` / `effort` override per task |
| `codex_review` | Code review: uncommitted changes / against a base branch / a commit / custom instructions |
| `codex_wait` | Wait for results; a timeout only returns the current state, the task keeps running |
| `codex_status` | List tasks, or show one task's details and full output |
| `codex_steer` | Send a follow-up instruction to a running task |
| `codex_interrupt` | Interrupt, keeping what has been produced |

- Tasks are ephemeral by default and don't appear in Codex history; subagents can use the skills and plugins in `~/.codex`
- For fire-and-forget work pass `detach=true`; Claude won't wait for it when ending its turn

## Permissions

Subagents run **without a sandbox** (`sandbox=danger-full-access`) and **without approvals** (`approvalPolicy=never`) by default: they can edit any file and run any command without stopping to ask, and the occasional question that would need a human answer is skipped so the task never hangs waiting for one. The reason: Codex's Windows sandbox runs commands under a restricted account that cannot see tools installed in the user profile (Python, pnpm, uv, …), so a sandboxed subagent can't even run tests.

`codex_review` uses the same default: review mode itself does not modify files, but it needs to run `git` and similar commands. There is no per-task sandbox option: `read-only` enables the same restricted sandbox, inside which no command can start, so Codex cannot even read files. The default can be changed with `CODEX_SUB_SANDBOX`.

## Configuration

| Environment variable | Default |
|---|---|
| `CODEX_SUB_BIN` | auto-detected |
| `CODEX_SUB_SANDBOX` | `danger-full-access` |
| `CODEX_SUB_MODEL` / `CODEX_SUB_EFFORT` / `CODEX_SUB_SERVICE_TIER` | `gpt-6.1-sol` / `high` / `default` |
| `CODEX_SUB_STATE_DIR` | `%LOCALAPPDATA%\codex-sub` (logs and session state; server and hook must use the same value) |

## Notes

- Relies on some experimental `codex app-server` APIs; Codex updates may change the protocol. Verified with Codex 0.163
- Each Claude session keeps one app-server process alive (~150 MB); it exits with the session

Test: `python tests/smoke.py` (calls Codex for real, about 3 minutes).

Uninstall: `claude mcp remove codex-sub -s user`, then remove the three hooks from settings.json.

## License

MIT
