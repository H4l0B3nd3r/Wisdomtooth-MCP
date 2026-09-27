# Internals

Notes for anyone changing the server: the non-obvious decisions, and the bugs
each one prevents. Most have a test pinning them; if a change here makes a
test fail, read the test's docstring before changing the test.

## Running the Claude Code CLI

- **The prompt goes on stdin, the system prompt in a file.** Windows caps a
  command line at about 32k characters (8k through `cmd.exe`), while the
  context cap alone is 60k. A multi-line argument is also cut at the first
  newline when an npm `.cmd` shim runs, which drops every flag after it.
  Hence `-p` with the prompt piped to stdin, and `--system-prompt-file`.
- **One system-prompt file per consult**, under `~/.wisdomtooth/prompts/`,
  deleted when the CLI exits. A shared file was rewritten by concurrent
  consults.
- **npm shims run through `cmd.exe`**, and `wrap_for_windows` builds that
  command line itself: `cmd.exe /d /s /c "<every argument quoted>"`. A list
  passed to `subprocess` goes through `list2cmdline`, which leaves `&`
  unquoted and whose quotes `cmd.exe` mangles once a path contains a space.
  Inside quotes `cmd.exe` still expands `%VAR%`, so anything the calling
  agent controls must be validated before it reaches argv; today that is only
  the model name (`_CLI_MODEL`).
- **Kill the process tree, not the process.** With `cmd /c claude.cmd`, the
  node grandchild survives a plain `kill()`, holds the stdout pipe open, and
  the "timeout" hangs. `kill_tree` uses `taskkill /T` on Windows and a
  process group on POSIX.
- **Isolation is by flag.** `--tools ""`, `--strict-mcp-config`,
  `--no-session-persistence`, `--disable-slash-commands` and
  `--setting-sources ""`, run in the empty `~/.wisdomtooth/workdir`. Without
  `--strict-mcp-config` every consult boots the user's global MCP servers and,
  if this server is registered globally, recurses into itself.
- **Flags are probed, not assumed.** Claude Code updates itself, so
  `_cli_features` reads `--help` once per binary. Only a probe that found
  flags is cached; a timed-out probe would otherwise pin the process to a bare
  command line.
- **The output is read as `stream-json`.** Print mode needs `--verbose` for
  it, and `--include-partial-messages` makes a long answer arrive in pieces
  instead of one block at the end. Silence for `ADVISOR_IDLE_TIMEOUT` seconds
  is the stall signal; against Claude Code 2.1 the longest gap between lines
  was about 1.3 s, thinking included. The last line is the same `result`
  object `--output-format json` prints, so both formats share one parser.
  Error results carry `errors`, not `result`.
- **The plan meter comes from `rate_limit_event`**, which Claude Code 2.1
  streams with the 5-hour and 7-day utilization. Only the streaming path sees
  it.
- **Subprocess I/O is UTF-8** with `errors="replace"`; the Windows default
  code page garbles Claude's output.

## Billing

- **`auto` prefers API credentials**, detected the way the Anthropic SDK
  finds them, and uses the user's own Claude Code install only when there are
  none and it is signed in.
- **The server signs in to nothing and stores no Claude credential.** Anthropic
  does not allow third-party products to offer claude.ai login or use its
  plan limits without approval, so the Claude Code backend runs the user's
  install exactly as they set it up. Do not add a login flow or a token store.
- **`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are removed from the CLI's
  environment**, so an explicit `claude-code` backend uses the install's own
  sign-in. `CLAUDE_CODE_OAUTH_TOKEN`, if the user set one, passes through.
- **Nothing moves the user onto paid credits.** Claude Code at its usage
  limit fails the consult with a message; it never falls back to the API.
- **The resolved backend is cached, except "unavailable"**, so credentials
  that appear after startup are noticed without a restart.
- **A consult is held only on evidence**: a declared allowance, a full plan
  window, a learned cost per plan percentage point, or OpenRouter credit.
  Rate-limit headers never hold a consult; they refill within a minute.

## The MCP layer

- **Both SDK majors are supported.** mcp 2.x renamed `FastMCP` to
  `MCPServer` and moved it; the server imports whichever exists. The suite
  must pass in both environments (`.venv` with 2.x, `.venv-mcp1` with 1.x).
- **Errors are `ToolError`s.** `AdvisorError` subclasses the SDK's
  `ToolError`, the channel that keeps the message intact. Anything else
  reaches the agent as a generic failure, which would hide the login steps
  that are the point of an auth error.
- **Consult tools have no return annotation.** Annotating a content-block
  list makes mcp 1.x derive an output schema and send every block twice;
  mcp 2.x does not. `tests/test_consult_log.py` pins this.
- **Sync tools run in a worker thread.** mcp 1.x calls a plain `def` tool on
  the event loop, so `tool()` wraps those in `asyncio.to_thread`.
- **Slow tools send progress notifications.** Clients abort a silent call at
  their own timeout; Kilo restarts its timer on each notification.
  `advisor_auth_check` uses the same heartbeat as consults.
- **The startup banner resolves the backend in a thread**, so the handshake
  never waits on `claude auth status`.
- **Cancellation and usage travel in context variables.** `asyncio.to_thread`
  copies them into the worker thread; `loop.run_in_executor` does not, and
  swapping it in silently breaks both.

## State on disk

- **Everything lives under `config.state_dir()`**: `~/.wisdomtooth`, or the
  pre-0.8 `~/.claude-advisor` when only that exists. Code must never call
  `expanduser` for state directly, or the two directories drift apart and a
  stored login seems to vanish.
- **Transcripts store what was sent, after redaction**, so a key the server
  refused to transmit is not written to disk either.
- **Files holding keys are created owner-only** before anything is written.
- **Accounting never costs the answer.** Ledger and transcript writes swallow
  I/O errors, and the ledger reader skips torn lines.
- **Settings parse leniently.** A malformed number falls back to its default
  with a warning; an exception at import shows up in a client only as "failed
  to connect".

## Other advisors

- **A tier an advisor does not define means its default model**, never a
  model literally named `fast`.
- **A leading `<think>` block is stripped**: local reasoning models often
  send their reasoning inline.
- **`multi_advisor` checks everything before sending anything**, then runs
  each advisor in its own thread with its own context copy, so one failure
  costs only its own section.
- **Gemini goes through the AI Studio API.** The Gemini CLI's personal login
  rejects third-party clients.

## CLI advisors

- **Every vendor CLI gets its prompt on stdin**, for the same reasons as
  Claude's. Each has its own way: `codex exec -`; `gemini`/`qwen` read stdin
  and append `-p`; `kilo run` and `copilot` read piped stdin; `agy` only via
  `--input-format stream-json` with `{"event": "user", "message":
  {"content": ...}}` and `--print=`.
- **Read-only is each CLI's own mode**, so it is weaker than Claude's
  `--tools ""`: plan or ask modes, a read-only sandbox, denied tools. Qwen's
  read tools are excluded one by one. Never add an auto-approve flag.
- **Qwen reports some failures as a successful result** whose text starts
  `[API Error`; the reader treats that as an error.
- **`WISDOMTOOTH_NESTED`**: every CLI child (Claude included) runs with it,
  and a server that starts with it refuses to consult. Kilo, Qwen and others
  load the user's MCP servers, this one included.
- `tests/fake_cli.py` speaks each CLI's output format as probed; when a CLI
  changes its format, update the fake and the reader together.

## Testing

- The suite needs no credentials and spends nothing. `tests/conftest.py`
  provides a fake `claude` CLI (a `.bat` on Windows, so the `cmd.exe` path is
  exercised) and a fake OpenAI-compatible endpoint, and clears provider keys
  from the environment.
- Run it in both environments before a release:
  `.venv/Scripts/python -m pytest` and `.venv-mcp1/Scripts/python -m pytest`
  (`bin/` instead of `Scripts/` outside Windows).
- **Testing by hand over stdio:** closing stdin is the shutdown signal. A
  script that writes every message and closes the pipe makes the server exit,
  possibly before an in-flight consult answers. Send one request, read its
  response, then send the next.
- **Reinstalling:** `uv tool install --force` reuses a wheel cached by
  version, so an unbumped version reinstalls the old code. Use
  `uv tool install --force --reinstall --no-cache .`, and on Windows stop the
  running server first; it locks its own install directory.
