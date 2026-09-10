# AGENT-UPDATE.md — Migration playbook

## 0.10.0 — hangs caught in minutes, trimmed answers, HTTP auth, `doctor`

**Nothing to change for a stdio config.** HTTP setups: see "HTTP auth".

- **A stalled consult stops after 5 minutes of silence**, not at the one-hour
  wall clock. The server reads the CLI's `stream-json` output line by line
  (`--verbose --include-partial-messages`). A working consult streams
  something every few seconds, even while it thinks, so
  `ADVISOR_IDLE_TIMEOUT` (300s) without output means a stall. The wall-clock
  limit stays as the backstop, and a CLI too old to stream falls back to
  `json` and the wall clock.
- **Heartbeats say what Claude is doing:** "Claude is still working (45s
  elapsed, writing, ~320 words so far)", or "thinking".
- **Answers far over the budget are trimmed for the caller.** Past 1.5× the
  answer budget the caller gets the lead — ending between paragraphs, code
  fences closed — and a note pointing to the transcript, which keeps the whole
  answer. `ADVISOR_TRIM_ANSWERS=0` turns it off; nothing is trimmed when
  transcripts are off.
- **HTTP auth.** `ADVISOR_HTTP_TOKEN` makes every HTTP request carry
  `Authorization: Bearer <token>`. **Breaking, HTTP only:** a bind beyond
  loopback (such as `ADVISOR_HOST=0.0.0.0` in Docker) without a token now
  refuses to start. Set a token and put it in the client's `headers` (see
  `kilo-configs/kilo.docker.jsonc`), or set `ADVISOR_HTTP_NO_AUTH=1` when a
  proxy or firewall guards the port.
- **`wisdomtooth-mcp doctor`** checks the CLI and login and prints a config
  for Kilo, OpenCode, Claude Code, Claude Desktop, Cursor, Cline or Codex.
  `wisdomtooth-mcp --version` prints the version.
- **Fixes:** the MCP handshake no longer waits for `claude auth status` (the
  startup banner ran it first, and Kilo gives a server 30s to connect); on
  mcp 1.x, `advisor_status`, `advisor_models`, `advisor_configure` and
  `advisor_usage` no longer block the event loop while they probe the CLI; a
  malformed number in an env var or the config file falls back to its default
  with a warning instead of stopping the server from starting; a CLI error
  result reports its `errors` text rather than raw JSON, and a consult stopped
  by `ADVISOR_MAX_BUDGET_USD` says so; the Docker image builds again (it was
  missing README.md and LICENSE).
- **Internals:** `server.py` is split into modules by concern — `config`,
  `models`, `prompts`, `safety`, `usage`, `transcripts`, `claude_cli`,
  `backends`, `httpauth`, `doctor` — and is now the composition root.
  Settings are one frozen `Settings` object from `config.load_settings`.
  Backends declare what they honour (`honours_max_tokens`, `fallback`, a
  footer `label`), and the footer, `advisor_status` and `advisor_models` are
  built from those declarations.
- **CI:** GitHub Actions runs the suite on Linux and Windows × Python
  3.10/3.13 × mcp 1.x/2.x, plus macOS.

---

## 0.9.0 — usage ledger, server-read files, follow-ups, presets

**Nothing to change for an existing config.** New behaviour, all server-side:

- **Usage ledger and caps.** Every consult is logged to
  `~/.wisdomtooth/usage.jsonl` (tokens, API-rate cost, duration), summarised
  by the new `advisor_usage` tool and a line in `advisor_status`; the answer
  footer gains a `[usage: ...]` line. Optional caps:
  `ADVISOR_MAX_CONSULTS_PER_HOUR` / `_5H` / `_WEEK`, `ADVISOR_MAX_USD_PER_DAY`.
  `ADVISOR_USAGE_LOG=0` turns the ledger off.
- **Repeat guard.** An identical consult within `ADVISOR_REPEAT_WINDOW`
  minutes (30) returns the saved answer for free.
- **`context_files`** on `ask_wisdomtooth` and `review_code`: the server reads
  the files itself, inside `ADVISOR_FILE_ROOTS` or its working directory.
- **`follow_up_of`** on `ask_wisdomtooth`: continue a saved consult by file name.
- **Caller presets.** `ADVISOR_PRESET=small|medium|large` sets answer budget
  and tool surface together. The default is `medium` (a 2,000-word ceiling),
  replacing 0.8.1's 64,000, which is now the `large` preset. A config that sets
  `ADVISOR_ANSWER_BUDGET` explicitly keeps its value.
- **Profanity scrubbing is off by default.** `ADVISOR_NSFW_SCRUB=1` turns it
  back on; the shipped rules file no longer tells agents to scrub.
- **Fixes:** each consult gets its own system-prompt file (concurrent consults
  shared one); a consult the client cancels now kills the `claude` process
  instead of letting it run on (on the API backend it closes the stream);
  `max_context_chars` and `nsfw_scrub` honour the config file; a follow-up of
  a large consult keeps the earlier answer instead of losing it to truncation;
  a failed `claude --help` probe is retried rather than cached;
  `advisor_login` and `advisor_auth_check` send progress heartbeats, so they
  survive a client's silence timeout like consults do; tokens from failed
  consults count in usage totals and the dollar cap; sub-cent costs show as
  `≈$0.0034`, not `≈$0.00`; the consult tools no longer claim `deep` is the
  default model.
- **Backend registry**, the seam for non-Claude providers — see README →
  Adding a provider.
- **License:** Apache-2.0.

`advisor_usage` is hidden in minimal mode like the other operator tools;
`advisor_status` carries the summary line.

---

## 0.8.1 — large consults no longer die at 180s

**Nothing to change in the client config. Reinstall and restart the server entry.**

A big planning consult (Opus, effort=high, `ADVISOR_ANSWER_BUDGET=10000`) was
killed at the flat 180s limit. Before 0.6.0 the budget variable did not exist,
so a config carrying `10000` got unbudgeted answers; since 0.6.0 the system
prompt invites answers up to that length, and generating them outruns 180s.
Two fixes, both server-side:

1. **The kill timeout is sized per consult.** `ADVISOR_TIMEOUT` (default now
   300s) is the floor; each consult adds ~10s per 1,000 characters sent and
   ~0.06s per word of answer budget, multiplied ×1.5/×2/×2.5 at effort
   high/xhigh/max, capped by `ADVISOR_TIMEOUT_MAX` (3600s). The failing shape
   now gets ~30 minutes; a short question at the defaults ~6.
   `ADVISOR_TIMEOUT_SCALE=0` restores the flat limit.
2. **Progress heartbeat.** The client's own per-request timeout (Kilo: the
   server entry's `timeout`, 300s) would otherwise fire first. Kilo restarts
   it on every MCP progress notification, and the server now sends one every
   `ADVISOR_PROGRESS_INTERVAL` (15s) while a consult runs. Clients that did not
   ask for progress are unaffected.

A timed-out consult's error now states the limit it was given and why, and
lists "the consult genuinely needs longer" as a cause with the knob to turn.

**Defaults raised for comprehensive answers:** `ADVISOR_ANSWER_BUDGET` 600 →
64000 words and `ADVISOR_MAX_TOKENS` (API backend) 16000 → 64000. The length
instruction now calls the budget a ceiling, not a target, and tells Claude to
size each answer to what the question needs, since every word counts against
the user's 5-hour and weekly limits. With a 64,000-word ceiling every consult's
kill timeout sits at `ADVISOR_TIMEOUT_MAX`, so a genuinely hung CLI takes up to
an hour to surface. **Small-context callers must now set the budget
explicitly** (the `kilo.local-models.jsonc` preset already uses 500).

---

## 0.8.0 — renamed to Wisdomtooth

**One breaking change: the `ask_claude` tool is now `ask_wisdomtooth`.**

The project was called Claude Advisor because Claude is what it consults. That
name is a dead end for a tool meant to be provider-agnostic, so the product is
now **Wisdomtooth MCP**. Claude remains the model and the default; only the
branding moved.

### What changed

| Was | Is |
|---|---|
| tool `ask_claude` | tool `ask_wisdomtooth` |
| package `claude_advisor` | package `wisdomtooth` |
| distribution / command `claude-advisor-mcp` | `wisdomtooth-mcp` |
| MCP server name `claude-advisor` | `wisdomtooth` |
| rules file `.kilocode/rules/claude-advisor.md` | `.kilocode/rules/wisdomtooth.md` |
| state directory `~/.claude-advisor/` | `~/.wisdomtooth/` |

### What did NOT change

- **Every `ADVISOR_*` environment variable.** `ADVISOR_BACKEND`,
  `ADVISOR_MODEL`, `ADVISOR_MINIMAL_TOOLS` and the rest keep their names. An
  existing MCP client config needs no env edits.
- **The seven `advisor_*` operator tools** — `advisor_login`, `advisor_status`,
  `advisor_models`, `advisor_configure`, `advisor_set_token`, `advisor_logout`,
  `advisor_auth_check`. "Advisor" describes the role, not the vendor.
- `review_code` and `compare_approaches`.
- The answer format, the footer, and the transcript file from 0.7.0.

### Migrating

1. Reinstall so the new command exists:
   `uv tool install --force /path/to/wisdomtooth-mcp` (the old
   `claude-advisor-mcp` command is not removed for you — `uv tool uninstall
   claude-advisor-mcp` does that).
2. In the MCP client config, change `"command"` to `wisdomtooth-mcp`, and
   rename the server key to `wisdomtooth` if you want the label to match.
3. If `ask_claude` appears in an `alwaysAllow` list, an agent rules file, or a
   custom prompt, change it to `ask_wisdomtooth`. **This is the one that bites
   silently:** a stale `alwaysAllow` entry does not error, it just starts
   prompting for approval again.

### Your stored login survives

The state directory moved to `~/.wisdomtooth/`, but `_state_dir()` still
returns the old `~/.claude-advisor/` when that is the only one present. An
upgrade therefore keeps the OAuth token, `config.json`, and existing consult
transcripts in place — a rebrand must not sign the user out. Move the directory
by hand if you want the new name on disk; nothing reads the old one once the
new one exists, so **do not create both**.

### On "provider-agnostic"

The name is the only part of that shipped in 0.8.0. Both backends still talk to
Claude — `_consult_claude_code` via the Claude Code CLI and `_consult_api` via
the Anthropic SDK. A second provider means a third backend behind the same
`_consult` contract; nothing in the tool surface or config layer blocks it, but
nothing implements it yet either. Do not tell users they can point this at
ChatGPT today.

---


## 0.7.0 — the answer now outlives the chat window

**Nothing to change; new behaviour is on by default.**

Claude's answer came back as an MCP tool result and stopped there. That is a
place this server does not control: editors collapse the block, a small local
model paraphrases it into two sentences, and a context trim eventually deletes
it. Users were reading answers out of their inference server's logs.

1. **Every consult is now written to a Markdown file** in
   `~/.wisdomtooth/consults/`, named
   `<timestamp>-<tool>-<question-slug>.md`. It holds what was sent (after the
   same secret redaction that guards the wire), what came back, and which
   account paid. The newest `ADVISOR_CONSULT_KEEP` (200) are kept.
2. **The path travels back two ways.** A `[saved: ...]` line in the answer
   footer, which every client renders because it is only text and which tells
   the agent to hand the path to the user; and a `resource_link` content block
   marked `audience: ["user"]`, for clients that render one as a link.
3. **New settings:** `ADVISOR_SAVE_CONSULTS` (`0` disables the whole feature),
   `ADVISOR_CONSULT_DIR`, `ADVISOR_CONSULT_KEEP`. `advisor_status` reports the
   directory in use.

### For agents calling this server

`ask_wisdomtooth`, `review_code` and `compare_approaches` now return **two
content blocks** rather than one, and no longer declare an output schema. The
answer text is unchanged and still the first block, so a client that reads text
results needs no change. When the footer carries a `[saved: ...]` path, give
that path to the user — it is the copy they can actually read.

### Why no output schema

Annotating the tools' return type as a content-block list makes mcp 1.x derive
an output schema and echo every block back a second time as structured JSON,
while mcp 2.x suppresses the schema entirely. An unannotated return behaves
identically on both, which is what the cross-major test run pins.

### Why a file at all

MCP gives a server no way to draw in its client's window. The tool result is
the only thing it can put in front of a person, and how that is rendered is the
client's choice; `notifications/message` only moves the problem into the
client's log pane. Surfacing an answer *inside* the chat UI as a first-class
panel would need a change in Kilo/Cline themselves. A file is the one channel a
server owns end to end.

---


## 0.6.0 — sized for the actual caller (a local model)

The consumer of this server is usually a small local model in a coding agent,
not a frontier model. Three changes follow from that; all are settings, none
remove capability.

1. **`ADVISOR_ANSWER_BUDGET` (new, default 600 words).** Claude's answer is
   inserted into the CALLING model's context, and an unbounded answer can
   exceed a small model's entire window. `ADVISOR_MAX_TOKENS` could not help
   here: it is an API-only parameter and the Claude Code CLI has no equivalent
   flag, so on the subscription backend answers were previously uncapped. The
   budget is expressed in the system prompt, which both backends honour. Set
   `0` to restore unlimited answers.
2. **`ADVISOR_MINIMAL_TOOLS` (new, default off).** Set `1` to advertise only
   `ask_wisdomtooth` and `advisor_status`. Tool schemas are charged against the
   caller's context on every turn: measured ~3,340 tokens for all ten tools
   versus ~1,090 for two — 41% versus 13% of an 8k window. Hidden tools are
   still ordinary functions; only the advertised schema shrinks.
3. **Default model is now `balanced` (Sonnet 5), was `deep` (Opus 5).** A local
   model escalates often, and Opus on every consult exhausts a Pro plan's
   headless quota quickly. `deep` remains one per-call argument away, and
   `ADVISOR_MODEL=deep` restores the old default.

`advisor_configure` gained `answer_budget`, so "keep answers shorter" no longer
needs a config edit and restart. `kilo-configs/kilo.local-models.jsonc` is a
ready-made preset. See `USAGE.md` for the full guide.

**Verified end to end against a real local model** (Qwen3.8-27B via LM Studio,
using the same prompt assembly Kilo performs): it escalated with
`ask_wisdomtooth`, filled all three required arguments, chose its own
model/effort tier, and got a
usable in-budget answer — in both full and minimal tool modes.

---


## 0.4.0 — REQUIRED: the previous version no longer installs cleanly

### Why you must upgrade
`mcp` 2.0 renamed `FastMCP` to `MCPServer` and moved it. The old pin
(`mcp>=1.0`) means a fresh `uv tool install` today pulls mcp 2.x, and 0.3.x
then dies at import with:

```
ModuleNotFoundError: No module named 'mcp.server.fastmcp'
```

0.4.0 imports whichever class the installed SDK provides and is tested against
both mcp 1.x and mcp 2.x.

### Install
```bash
uv tool install --force /path/to/wisdomtooth-mcp
```
Then **restart the MCP server entry in your client** and call `advisor_status`.
Its first line must read `version: 0.4.0`. If it does not, you are running old
code — stop and reinstall before debugging anything else. (An MCP stdio server
is a long-lived child process; the client keeps the old one alive until the
entry is toggled. On Windows, `uv tool install` fails with "Access is denied" if
an old instance is still running — stop the client's server entry first.)

### Breaking / behavioural changes

1. **`ADVISOR_BACKEND` now defaults to `auto`, not `api`.** `auto` prefers the
   local Claude Code CLI when it is logged in, so consults spend the user's
   **subscription** instead of silently billing the Console account per token.
   Set `ADVISOR_BACKEND=api` explicitly if you actually wanted API credits.
2. **Model tiers updated to the current lineup**: `fast` → `claude-haiku-4-5`,
   `balanced` → `claude-sonnet-5`, `deep` → `claude-opus-5`. The default is now
   `deep`, since every call to an escalation tool is by definition a hard
   problem. `ADVISOR_MODEL=balanced` restores the old default tier.
3. **`effort` now works on the subscription backend** (`--effort`), and
   **`xhigh` is a valid level**. Previously effort was silently dropped there
   and `xhigh` was rejected as unknown.
4. **`max_tokens` is a per-call argument** on all three consult tools, default
   16000 (was a fixed 8192 from env only). API backend only.
5. **Five new free tools**: `advisor_login` (connect a Claude subscription
   over OAuth), `advisor_set_token` / `advisor_logout` (headless credential
   store), `advisor_models` (catalogue of tiers, per-model effort support,
   token ceiling) and `advisor_configure` (change model / effort / max_tokens
   for the rest of the session without a restart).
5a. **Connecting an account no longer means editing config.** `advisor_login`
   opens `claude auth login --claudeai` in a console on the user's desktop,
   waits for the browser flow, and switches the advisor onto subscription
   billing immediately. For headless hosts and GUI-launched editors,
   `claude setup-token` + `advisor_set_token` stores the credential in
   `~/.wisdomtooth/credentials.json` (owner-only) and injects it into every
   consult — so `CLAUDE_CODE_OAUTH_TOKEN` in the MCP client's JSON is no longer
   needed, and the secret stays out of a committable file. Existing
   `CLAUDE_CODE_OAUTH_TOKEN` env settings still work and take precedence.
6. **A JSON config file** is read from `ADVISOR_CONFIG`, else
   `~/.wisdomtooth/config.json`. Environment variables override it.
7. **`advisor_auth_check` uses `claude auth status --json`** instead of
   sniffing for a credentials file, so it reports the real login state, auth
   method, and plan — and flags a CLI that is logged in with an API key
   (which would bill the Console account, not the subscription).

### Bugs fixed that changed observable behaviour

- `ADVISOR_MODEL` and `ADVISOR_LOCK` were **ignored entirely** on the
  claude-code backend; it read the raw per-call argument. Both work now.
- Long consults could fail to launch on Windows: the prompt was passed as an
  argv element against a ~32k command-line cap (~8k through `cmd.exe`) while
  the context cap is 60k. The prompt now travels on **stdin**.
- A multi-line `--system-prompt` argument was truncated at the first newline by
  npm `.cmd` shims, silently dropping every flag after it. Now
  `--system-prompt-file`.
- Tool use was discouraged by a system-prompt sentence the model could ignore;
  it is now enforced with `--tools ""`.
- Error messages were being replaced by a generic "Error executing tool
  ask_wisdomtooth" before reaching the agent, hiding the login instructions
  that are the whole point of an auth failure. Failures now raise `AdvisorError`
  (a `ToolError` subclass), whose text the SDK preserves.
- Requests are streamed, so a large `max_tokens` no longer risks an HTTP
  timeout; `stop_reason: "refusal"` is reported clearly instead of surfacing as
  a mysteriously empty answer.
- Adaptive thinking and `output_config.effort` were not being sent to Opus 5 /
  Sonnet 5 at all, because the capability table only listed older prefixes.

### Verifying
```bash
# no credentials needed, no spend
uv venv && uv pip install -e . pytest pytest-asyncio anyio
.venv/Scripts/python -m pytest       # 196 tests
```
Then in your client: `advisor_status` → `advisor_auth_check` → `advisor_login`
if it reports anything other than a claude.ai subscription → one real
`ask_wisdomtooth` with `model: "fast"`.

---

## Historical log (0.2.x - 0.3.5)

## 0.3.5 — REQUIRED: fixes your recurring smoke-test hang + auth handling

### Why it hung AGAIN despite the source having the fix
Two independent causes; check BOTH:

1. **STALE INSTALLED COPY (most likely).** `wisdomtooth-mcp.exe` is a
   uv/pipx shim running a COPY of the package frozen at install time.
   Verifying the source tree proves nothing about what's running, and
   `advisor_status` existed before the fix, so a healthy status doesn't
   either. FIX: reinstall with `uv tool install --force .` (or
   `pipx reinstall wisdomtooth-mcp`), restart the Kilo server entry, then
   call `advisor_status` — it now reports `version:` as its FIRST line.
   **If it does not say `version: 0.3.5`, you are running old code. Stop and
   reinstall. Do not debug anything else until the version matches.**
2. **Timeout wedge via grandchild process (real Windows bug, fixed).** On
   timeout, killing `cmd /c claude.cmd` left the grandchild node process
   alive holding the stdout pipe, so the server blocked forever waiting for
   pipe EOF — the timeout never surfaced. 0.3.5 kills the whole process
   TREE (`taskkill /F /T` on Windows, killpg on POSIX). Verified: a
   deliberate pipe-holding grandchild now errors out at the deadline.

### Auth / OAuth / credentials — read before the next smoke test
- **Headless OAuth login is impossible.** If the CLI needs login, no retry,
  flag, or code change fixes it — a HUMAN must authenticate. The server now
  detects auth-failure signatures and returns an error telling you exactly
  that; relay it to the user verbatim.
- Two valid ways for the user to authenticate the subscription backend:
  (a) interactive: run `claude` → `/login` → choose the claude.ai Pro/Max
  account → `/status` shows the subscription; or (b) durable headless:
  run `claude setup-token` once and put the token in the MCP server env as
  `CLAUDE_CODE_OAUTH_TOKEN` (survives shell/profile differences and is the
  recommended option for a server spawned by a GUI editor).
- The server now strips BOTH `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`
  from claude subprocesses (either would hijack billing/auth), while
  passing `CLAUDE_CODE_OAUTH_TOKEN` through. Never re-inject the stripped
  vars (`ADVISOR_KEEP_AUTH_ENV=1` exists for gateway users only — do not
  set it without the user's instruction).
- **New free tool `advisor_auth_check`** — run it BEFORE the smoke test and
  whenever a consult fails: reports CLI presence/version, credential-file /
  headless-token status (macOS keychain caveat included), and hijack-var
  warnings. Costs nothing.
- API backend: missing/invalid `ANTHROPIC_API_KEY` now raises a clear AUTH
  FAILURE error instead of an SDK stack trace. Fix the key, restart the
  server entry, don't retry blind.

### Manual pipe-testing rule (your shell tests were flawed)
Never close the server's stdin while a tool call is pending — EOF is the
shutdown signal and the server will exit (code 0) without answering. Send
one message, read its response, then send the next; close stdin only at the
end. Prefer testing through Kilo itself. A clean Kilo result overrides any
contradictory pipe-test result.

### Your updated verification sequence
1. Reinstall → restart server entry.
2. `advisor_status` → first line MUST be `version: 0.3.5`.
3. `advisor_auth_check` → resolve anything it flags (relay login steps to
   the user; you cannot do them).
4. Smoke test `ask_wisdomtooth` (model="fast") → expect seconds; a timeout now
   returns diagnostics instead of hanging.

## 0.3.3 addition — NSFW scrubbing (apply with everything below)

- YOU (the agent) must now scrub all content passed to advisor tools —
  files, logs, pasted text, history — replacing NSFW words with SFW
  alternatives before the call (whole words only; never alter substrings
  inside identifiers). See the updated .kilocode/rules/wisdomtooth.md.
- The server backstops this: word-boundary, case-preserving NSFW→SFW
  replacement on question + context. Configure via ADVISOR_NSFW_EXTRA_JSON
  (extend wordlist) / ADVISOR_NSFW_SCRUB=0 (disable). Verify version 0.3.3.

## 0.3.2 additions (apply together with the 0.3.1 hotfix below)

- Tool handlers are async: long consults no longer block the server's event
  loop (before: server went unresponsive to protocol traffic mid-consult).
- Windows: subprocess I/O forced to UTF-8 (cp1252 could garble/crash output).
- Spawned `claude` runs with `--strict-mcp-config` (auto-fallback on old
  CLIs): prevents booting all user-scope MCP servers per consult and blocks
  advisor→advisor recursion.
- Obvious secrets in `context` are redacted server-side; context is capped
  at 60k chars with a truncation marker (`ADVISOR_MAX_CONTEXT_CHARS`).
- Adaptive-thinking models at effort high/max get raised max_tokens so the
  answer isn't consumed by thinking; empty-answer guard added.
- READ `GOTCHAS.md` (new) and the updated `.kilocode/rules/wisdomtooth.md`
  — they contain operational rules you (the agent) must follow: timeout
  alignment, absolute paths on Windows, restart-after-config-change,
  no backend switching on limit errors, advice-is-not-instructions,
  two-strikes escalation stop.
- Verify version after install: `pip show wisdomtooth-mcp` → 0.3.2.

## 🔥 HOTFIX 0.3.1 — fixes the hanging smoke test you observed

Root cause of the ~2-minute hang on `ask_wisdomtooth`: the `claude -p`
subprocess inherited the MCP server's stdin — which is the client's JSON-RPC
pipe, held open for the whole session. `claude -p` treats piped stdin as input
and waits for EOF before answering, so it blocked forever. Your
piped-all-at-once test worked precisely because closing the pipe delivered that
EOF. This was a bug
in the server's subprocess call, not in Kilo, not in Windows stdio framing.

0.3.1 changes:
- `stdin=subprocess.DEVNULL` on the claude subprocess (the actual fix).
- Runs claude in an isolated empty workdir (`~/.wisdomtooth/workdir`) so
  it never loads CLAUDE.md/project state from the MCP client's cwd and
  avoids first-run trust prompts.
- Windows: `claude.cmd`/`.bat` shims are invoked via `cmd /c`; no console
  window is flashed.
- Fail-fast: `ADVISOR_TIMEOUT` default lowered to 120s; on timeout you get a
  diagnostic error (login / trust-prompt / network) instead of a silent hang.
  Do NOT retry timeouts in a loop — surface them to the user.
- The API backend also gets a bounded SDK timeout (same env var).

TO APPLY: replace `wisdomtooth/server.py` and `pyproject.toml` with the
0.3.1 versions, reinstall (`uv tool install --force .` / `pipx install
--force .` / `pip install -e .`), restart the MCP server entry in Kilo, then
re-run your smoke test: `ask_wisdomtooth(question="what is 2+2? one word",
context="smoke test", attempts_so_far="smoke test", model="fast")`. Expect an
answer in seconds with a `billed to ...` footer. If it now *errors* about
login, run `claude` → `/login` interactively once — that blocking login was
previously invisible behind the hang.

---

# Original migration playbook (0.2.x → 0.3.x)

AUDIENCE: This file is written for the AI agent that has already built and
tested a Docker container from the 0.2.x sources. Follow it top to bottom.
Steps marked ⛔ STOP require asking the user before proceeding.

## 0. What changed (why you are updating)

Your deployed 0.2.x version bills EVERY advisor call to the user's Anthropic
**developer API account**. Version 0.3.0 adds:

1. `ADVISOR_BACKEND=claude-code` — routes calls through the host's Claude
   Code CLI so they bill the user's **Pro/Max subscription** instead. It
   strips `ANTHROPIC_API_KEY` from the subprocess so billing can never
   silently fall back to the API account.
2. `advisor_status` tool (free, no model call) — reports backend, billing
   target, defaults.
3. Billing footer on every answer (`billed to SUBSCRIPTION` / `billed to
   API ACCOUNT`) and a billing banner on stderr at startup.
4. `ADVISOR_TRANSPORT=http` (+ `ADVISOR_HOST`, `ADVISOR_PORT`, default 8484)
   for a persistent container instead of per-session stdio spawns.
5. New env vars: `ADVISOR_BACKEND`, `ADVISOR_EFFORT`, `ADVISOR_LOCK`,
   `ADVISOR_CLAUDE_BIN`, `ADVISOR_TIMEOUT`, `ADVISOR_TRANSPORT`,
   `ADVISOR_HOST`, `ADVISOR_PORT`. Default `ADVISOR_MAX_TOKENS` is now 8192.
6. `pyproject.toml` version is `0.3.0`. Verify after install:
   `pip show wisdomtooth-mcp` (or check the stderr banner exists).

## 1. ⛔ STOP — ask the user ONE question before touching anything

> "The advisor can bill either your developer API account (current setup,
> works in the Docker container) or your Claude Pro/Max subscription
> (requires running on the host where Claude Code is logged in — NOT in
> Docker). Which do you want?"

The user has previously indicated they prefer the **subscription**. If they
confirm → follow Path A. If they explicitly choose API billing → Path B.
Do not guess; this is a money decision.

## 2. Replace the sources (both paths)

Overwrite the old project directory with the 0.3.0 tree from
`wisdomtooth-mcp.zip` (or apply these files: `wisdomtooth/server.py`,
`pyproject.toml`, `Dockerfile`, `kilo-configs/*`, `.kilocode/rules/*`,
`README.md`). Do NOT keep the old single-file `server.py` at the repo root
if one exists — 0.3.0 lives in the `wisdomtooth/` package.

## 3A. Path A — SUBSCRIPTION billing (user-preferred)

The container you built CANNOT do this: the `claude-code` backend needs the
`claude` CLI plus the user's OAuth login state, which live on the host.

1. Verify prerequisites on the HOST (not in Docker):
   - `claude --version` succeeds. If not: ⛔ STOP, tell the user to install
     Claude Code and run `claude` → `/login` with their claude.ai account.
   - Ask the user to run `/status` inside `claude` and confirm it shows
     their subscription, not an API key. If it shows an API key, tell them
     to `unset ANTHROPIC_API_KEY` in their shell profile first.
2. Install on the host: `uv tool install /path/to/wisdomtooth-mcp`
   (fallbacks: `pipx install`, or `pip install` in a dedicated venv and use
   the venv's bin path in the config).
3. Update Kilo config: replace the advisor's docker-based entry with the
   contents of `kilo-configs/kilo.subscription.jsonc` — command
   `["wisdomtooth-mcp"]`, environment `{"ADVISOR_BACKEND": "claude-code"}`,
   timeout `300000`. Do NOT put `ANTHROPIC_API_KEY` in this entry.
4. Decommission or repurpose the container:
   - Default: stop and remove it (`docker rm -f <name>`); it billed the API
     account and is now redundant.
   - Only if the user asks to keep an API-billed fallback: keep it as a
     second server entry named `wisdomtooth-api`, disabled by default.

## 3B. Path B — API billing (keep your container)

1. Rebuild the image from the new sources:
   `docker build -t wisdomtooth-mcp:0.3.0 .`
2. Choose transport:
   - Keep stdio (simplest): Kilo command
     `["docker","run","-i","--rm","-e","ANTHROPIC_API_KEY","wisdomtooth-mcp:0.3.0"]`.
   - Or persistent HTTP: run once
     `docker run -d --name wisdomtooth -p 8484:8484 -e ANTHROPIC_API_KEY=... -e ADVISOR_TRANSPORT=http -e ADVISOR_HOST=0.0.0.0 wisdomtooth-mcp:0.3.0`
     and set the Kilo entry to `{"type":"remote","url":"http://localhost:8484/mcp"}`.
3. Remove any container/image built from 0.2.x sources to avoid version
   confusion (`docker rmi` the old tag).

## 4. Verify (both paths) — all four checks must pass

1. Kilo shows the server connected with FOUR tools:
   `ask_wisdomtooth`, `review_code`, `compare_approaches`, `advisor_status`.
   If `advisor_status` is missing, you are still running 0.2.x — recheck
   step 2/3.
2. Call `advisor_status` (free). Its `billing:` line must match the user's
   choice from step 1. If it says "DEVELOPER API account" but the user chose
   subscription (or vice versa), ⛔ STOP and fix the config before any
   advice call is made.
3. Make ONE cheap real call: `ask_wisdomtooth` with `model="fast"` and a trivial
   question, filling `attempts_so_far` honestly (e.g. "migration smoke
   test"). Confirm the answer footer says the expected billing target.
4. Report to the user: backend, billing target, transport, and the footer
   from the smoke test. Then delete nothing else and stop.

## 5. Rules file

Ensure `.kilocode/rules/wisdomtooth.md` from this repo is present in the
project. It is unchanged in spirit but you should re-copy it to be safe.
Continue to honor it: the advisor is an escalation path — Context7 and your
own attempts come first, and `attempts_so_far` must be filled truthfully.

## Known constraints to respect (do not "fix" these)

- `effort` is ignored on the claude-code backend (CLI doesn't expose it);
  the footer says so. This is expected, not a bug.
- The `claude-code` backend intentionally strips `ANTHROPIC_API_KEY` from
  its subprocess. Never re-inject it.
- Subscription headless calls draw from the plan's separate non-interactive
  credit pool (June 2026 billing change), not the interactive window. If
  calls start failing with limit errors, surface that to the user rather
  than switching backends silently.
