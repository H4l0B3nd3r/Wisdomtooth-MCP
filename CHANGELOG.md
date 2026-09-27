# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/) (pre-1.0: a minor version may
change behaviour).

## [0.11.1] - 2026-09-27

### Security
- On Windows, a `claude` CLI installed through npm runs as `claude.cmd` via
  `cmd.exe`. The command line is now quoted as a whole, and a `model` argument
  containing shell syntax (`&`, `|`, `>`, `%`, spaces) is refused. Before, a
  model name such as `sonnet&calc` ran the second command.

### Fixed
- Consults failed on Windows whenever the npm shim or the user profile path
  contained a space (a username such as `Jane Doe`).
- `claude_bin` in the config file was ignored; only `ADVISOR_CLAUDE_BIN` was
  read.
- In `auto` mode a server that started without credentials kept reporting
  "no usable credentials" after the user signed in or set a key, until it was
  restarted.
- API backend failures (rate limit, overload, network, timeout, and a
  rejected key on the `fallbacks` request that `deep` makes) now reach the
  agent as a clear message instead of a bare "Error executing tool".

### Changed
- Dependencies are bounded below the next major version: `mcp<3`,
  `anthropic<2`.
- The Docker image runs as an unprivileged user.
- `AGENT-UPDATE.md` is replaced by this changelog, and `GOTCHAS.md` by
  `docs/INTERNALS.md` (maintainer notes) plus the troubleshooting section of
  `USAGE.md`.

## [0.11.0] - 2026-09-21

### Added
- Advisors besides Claude: ChatGPT (`openai`), Gemini (`gemini`),
  OpenRouter, LM Studio, Ollama, or any OpenAI-compatible endpoint. Connect
  one at runtime with `advisor_connect`, or configure `advisors` in the config
  file or `ADVISOR_ADVISORS_JSON`. `ask_wisdomtooth`, `review_code` and
  `compare_approaches` take `advisor=<name>`.
- `multi_advisor`: two or three advisors in parallel, with the same question
  (`advisors`) or a question of its own for each (`targeted_questions`).
- `advisor_disconnect`.
- Account monitoring in `advisor_status` and `advisor_usage`: the Claude
  plan's 5-hour and 7-day utilization, OpenRouter credit, `x-ratelimit-*`
  headers and any allowance the user declares.
- Held consults: a request whose estimated cost exceeds what the account has
  left is refused before anything is sent, with a `HELD` error. The agent must
  ask the user and may repeat the call with `confirm_over_limit=true`.
- Settings: `ADVISOR_ADVISORS_JSON`, `ADVISOR_DEFAULT_ADVISOR`,
  `ADVISOR_ADVISORS_FILE`, `ADVISOR_ACCOUNTS_FILE`.

## [0.10.0] - 2026-09-10

### Added
- The CLI is read as `stream-json`, so a consult that stops producing output
  for `ADVISOR_IDLE_TIMEOUT` seconds (300) is stopped as stalled instead of
  running to the one-hour wall clock. Progress heartbeats say what Claude is
  doing ("thinking", "writing, ~320 words so far").
- Answers far over the answer budget are trimmed for the caller; the full
  answer stays in the transcript. `ADVISOR_TRIM_ANSWERS=0` turns this off.
- `ADVISOR_HTTP_TOKEN`: bearer-token auth for the HTTP transport.
- `wisdomtooth-mcp doctor` and `wisdomtooth-mcp --version`.
- CI on Linux, Windows and macOS, Python 3.10 and 3.13, mcp 1.x and 2.x.

### Changed
- **Breaking (HTTP only):** binding beyond loopback without a token now
  refuses to start. Set `ADVISOR_HTTP_TOKEN`, or `ADVISOR_HTTP_NO_AUTH=1` when
  a proxy or firewall guards the port.
- `server.py` is split into modules by concern.

### Fixed
- The MCP handshake no longer waits for `claude auth status`.
- On mcp 1.x, the status tools no longer block the event loop.
- A malformed number in an environment variable or the config file falls
  back to its default with a warning instead of stopping the server.
- CLI error results report their `errors` text; a consult stopped by
  `ADVISOR_MAX_BUDGET_USD` says so.
- The Docker image builds again.

## [0.9.0]

### Added
- Usage ledger (`~/.wisdomtooth/usage.jsonl`), the `advisor_usage` tool, and
  optional caps: `ADVISOR_MAX_CONSULTS_PER_HOUR` / `_5H` / `_WEEK`,
  `ADVISOR_MAX_USD_PER_DAY`.
- Repeat guard: an identical consult within `ADVISOR_REPEAT_WINDOW` minutes
  (30) returns the saved answer at no cost.
- `context_files`: the server reads files itself, inside
  `ADVISOR_FILE_ROOTS` or its working directory.
- `follow_up_of`: continue a saved consult by file name.
- Caller presets: `ADVISOR_PRESET=small|medium|large`.
- Apache-2.0 license.

### Changed
- The default answer budget is 2,000 words (the `medium` preset).
- Profanity scrubbing is off by default; `ADVISOR_NSFW_SCRUB=1` enables it.

### Fixed
- Concurrent consults no longer share one system-prompt file.
- A consult the client cancels stops the `claude` process (or closes the API
  stream) instead of running on.
- A follow-up of a large consult keeps the earlier answer.
- Tokens from failed consults count in usage totals and the spend cap.

## [0.8.1]

### Fixed
- Large consults no longer die at a fixed 180 s. The timeout is sized to the
  request (`ADVISOR_TIMEOUT`, `ADVISOR_TIMEOUT_SCALE`, `ADVISOR_TIMEOUT_MAX`),
  and progress notifications every `ADVISOR_PROGRESS_INTERVAL` seconds keep
  the client from timing out first.

## [0.8.0]

### Changed
- Renamed from `claude-advisor-mcp` to Wisdomtooth: the package is
  `wisdomtooth-mcp` and the main tool is `ask_wisdomtooth`. The `ADVISOR_*`
  settings and `advisor_*` tools keep their names. An existing
  `~/.claude-advisor` directory is still used when `~/.wisdomtooth` does not
  exist, so a stored login survives the rename.

## [0.7.0]

### Added
- Every consult is saved as a Markdown transcript, and its path is returned
  in the answer footer and as a `resource_link`.

## [0.6.0]

### Added
- `ADVISOR_MINIMAL_TOOLS` and `ADVISOR_ANSWER_BUDGET`, for small local models
  as callers.

## [0.4.0]

### Added
- Support for mcp 2.x alongside 1.x.
- `advisor_login`, `advisor_set_token` and `advisor_logout`.

### Changed
- The default backend is `auto`: the logged-in Claude Code CLI first, API
  credits only as a fallback.
- The prompt travels on stdin and the system prompt in a file, so large or
  multi-line prompts survive Windows command-line limits.
- Tool errors keep their message (they are `ToolError`s).

## [0.3.x]

- Subscription billing through the Claude Code CLI (`ADVISOR_BACKEND`),
  billing footers, the HTTP transport, secret redaction, the context size
  cap, UTF-8 subprocess I/O on Windows, `--strict-mcp-config` isolation, and
  process-tree cleanup on timeout.

[0.11.1]: https://github.com/H4l0B3nd3r/Wisdomtooth-MCP/compare/19104c6...v0.11.1
[0.11.0]: https://github.com/H4l0B3nd3r/Wisdomtooth-MCP/commit/19104c6
[0.10.0]: https://github.com/H4l0B3nd3r/Wisdomtooth-MCP/commit/ed60c06
