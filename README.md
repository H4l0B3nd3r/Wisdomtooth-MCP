# Wisdomtooth MCP

An MCP server that lets coding agents (Kilo Code, Cursor, Cline, Claude Code,
custom agents) **escalate to a frontier model for advice when they are stuck**
— one stateless call per question.

**Claude is the model today, and stays the default.** The name is model-neutral
on purpose — the backend layer is the seam where another provider (ChatGPT,
Kimi, …) would slot in, and neither the tool surface nor the configuration
assumes Anthropic. To be clear about what you get if you install it now:
multi-provider support is *not implemented yet*, and every consult goes to
Claude.

By default it spends **your Claude subscription**, not API credits: consults go
through the local Claude Code CLI, and the server only falls back to
pay-per-token API credits if the subscription is not usable. Every answer is
footed with which account paid for it.

It is deliberately positioned as an escalation path: the agent should use it
when it is having difficulty implementing code or understanding a framework,
platform, or OS, **and** other options (Context7, docs, web search, its own
attempts) have not helped. That policy is enforced in three layers so the agent
actually follows it:

1. **Tool descriptions** — every tool's MCP description begins with explicit
   WHEN TO USE / DO NOT USE criteria (agents always see these).
2. **Required `attempts_so_far` argument** — `ask_wisdomtooth` cannot be called
   without stating what was already tried and what Context7/docs returned.
3. **Kilo rules file** — `.kilocode/rules/wisdomtooth.md` gives the agent
   the full escalation policy as standing instructions.

**New here?** Read `USAGE.md` — setup, tuning for local models, and
troubleshooting. This file is the reference.

## Tools

| Tool | Purpose |
|---|---|
| `ask_wisdomtooth(question, context, attempts_so_far, [model, effort, max_tokens, context_files, follow_up_of])` | Stuck on implementation or framework/platform/OS behavior after docs + attempts failed. `context_files` has the server read files itself; `follow_up_of` continues a saved consult |
| `review_code(code, concern, [model, effort, max_tokens, context_files])` | Residual doubt about subtle or security-sensitive code just written |
| `compare_approaches(problem, options, criteria, [model, effort, max_tokens])` | 2+ viable approaches, tradeoffs unclear after your own analysis |
| `advisor_login(force, wait_seconds)` | Free. **Connects the user's Claude subscription over OAuth** — opens the sign-in on their desktop, waits, and switches billing over with no restart |
| `advisor_set_token(token)` | Free. Headless alternative: stores a `claude setup-token` credential privately and applies it immediately |
| `advisor_logout()` | Free. Forgets the stored subscription token |
| `advisor_models()` | Free. Lists tiers, per-model effort support, token ceiling |
| `advisor_configure(model, effort, max_tokens, answer_budget, reset)` | Free. Changes defaults for this server process — no restart needed |
| `advisor_status()` | Free. Active backend, billing target, current defaults, a one-line usage summary |
| `advisor_usage(days)` | Free. Consults, tokens and API-rate cost for the last 1 h / 5 h / 24 h / 7 d and per model, plus caps |
| `advisor_auth_check()` | Free. Login/credential diagnosis when a consult fails |

## Install

```bash
uv tool install /path/to/wisdomtooth-mcp     # recommended
# or: pipx install /path/to/wisdomtooth-mcp
```

This puts a `wisdomtooth-mcp` command on your PATH (stdio MCP server).

**For subscription billing** (the default), install Claude Code:

```bash
# https://claude.com/claude-code
```

Then connect your account. The easy way is to **ask your agent to call the
`advisor_login` tool** — it opens the official Claude sign-in in a console
window, waits for you to finish in the browser, and switches the advisor onto
subscription billing straight away. No config file to edit, no server to
restart.

The equivalents, if you would rather do it by hand:

```bash
claude auth login --claudeai     # interactive; then re-run advisor_login to confirm
claude auth status               # should print "authMethod": "claude.ai"
```

On a headless box, in a container, or when your editor launches the MCP server
with a reduced environment, use the durable token instead:

```bash
claude setup-token               # prints a long-lived subscription token
```

…then have your agent pass that token to `advisor_set_token`. The advisor
stores it in `~/.wisdomtooth/credentials.json` (owner-only) and injects it
into every consult, so it survives restarts and reduced GUI environments
without ever appearing in your MCP client's config.

## Add to a client

**Claude Code**

```bash
claude mcp add wisdomtooth -- wisdomtooth-mcp
```

**Kilo Code (current)** — merge `kilo-configs/kilo.subscription.jsonc` into your
project's `kilo.jsonc` under the `mcp` key, or use Settings → MCP → Add Server →
Local (stdio), command `wisdomtooth-mcp`.

**Kilo Code (classic)** — copy `kilo-configs/mcp.json` to `.kilocode/mcp.json`.

**Cursor / Windsurf / Claude Desktop** — standard `mcpServers` JSON with command
`wisdomtooth-mcp`; same shape as `kilo-configs/mcp.json`.

**Escalation policy** — copy `.kilocode/rules/wisdomtooth.md` into your
project's `.kilocode/rules/`. Kilo loads these as standing instructions, so the
agent knows to try Context7 and its own fixes first.

Tip: leave `ask_wisdomtooth` **off** any auto-approve/`alwaysAllow` list at
first. Seeing each escalation request tells you whether the agent is respecting
the policy; auto-approve later if it behaves.

## Who gets billed

`ADVISOR_BACKEND` decides, and `advisor_status` reports the live answer.

| Value | Behaviour |
|---|---|
| `auto` (default) | Claude Code CLI if it is installed **and logged in** → your **subscription**. Otherwise `ANTHROPIC_API_KEY` → your **Console account**. If neither exists, every consult fails with instructions rather than guessing. |
| `claude-code` | Always the CLI. Never spends API credits, even if the plan's quota is exhausted. |
| `api` | Always the Anthropic API. Pay-per-token on the Console account, **not** the Pro/Max subscription. |

If `advisor_status` says the backend is `api` or `unavailable` and you have a
Pro/Max plan, call `advisor_login` — that is the whole point of it.

`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are stripped from the CLI
subprocess, because Claude Code prioritizes them over the subscription and would
silently bill the API account instead. The subscription token is injected in
their place, from `CLAUDE_CODE_OAUTH_TOKEN` if set, otherwise from the
advisor's own credential store.

Note that `loggedIn: true` is not sufficient on its own: a CLI signed in with
an API key still bills the Console account. `advisor_auth_check` reports the
`authMethod` and flags anything that is not `claude.ai`; `advisor_login` with
`force=true` switches it.

In `auto` mode only, a consult that fails because the plan's headless quota is
exhausted retries on API credits (if a key exists) and says so loudly in the
answer. Set `ADVISOR_FALLBACK_TO_API=0` to disable that.

## Where the answer ends up

Claude's answer comes back as an MCP tool result inside your agent's chat —
which is somewhere this server does not control. Editors collapse the block,
small local models paraphrase it into a summary, and a context trim eventually
deletes it, so an answer you paid for can be genuinely hard to read back.

So every consult is also written to a Markdown file:

```
~/.wisdomtooth/consults/20260209-142233-ask-wisdomtooth-why-does-the-datagrid-flicker.md
```

Each file holds exactly what was sent (after secret redaction), exactly what
came back, and which account paid. The path reaches you two ways:

- **In the answer footer**, as text — every client renders that, and the tool
  description tells the agent to pass the path on to you.
- **As a `resource_link` content block** marked `audience: ["user"]`, for
  clients that turn one into something you can click.

The newest 200 are kept and older ones are dropped. Turn the whole thing off
with `ADVISOR_SAVE_CONSULTS=0`, or point it somewhere else with
`ADVISOR_CONSULT_DIR`. `advisor_status` prints the directory in use.

MCP gives a server no other way to put something in front of a person — there
is no "open this panel" call, and `notifications/message` only moves the
problem into the client's log pane. A file is the one channel that outlives
the chat window.

## Usage, spend and caps

Every consult is logged to `~/.wisdomtooth/usage.jsonl` with its tokens, how
long it took and what it would cost at API rates, and the answer footer shows
the same figures:

```
[usage: 14,210 tokens in · 2,130 out · 48s · ≈$0.12 at API rates]
```

`advisor_usage` totals the last hour, 5 hours, 24 hours and 7 days (the windows
Claude plans meter) and breaks the week down by model; `advisor_status` carries
a one-line version. Subscription consults are not billed per token, so the
dollar figure is a proxy for how much of the plan's allowance a consult used.
The plan's own meter is not visible to an MCP server.

Optional caps stop an agent that escalates too often before it drains the plan:
`ADVISOR_MAX_CONSULTS_PER_HOUR`, `_PER_5H`, `_PER_WEEK` and
`ADVISOR_MAX_USD_PER_DAY`. A capped consult is refused before anything is
sent, and the error says when the next slot opens. An identical consult inside
`ADVISOR_REPEAT_WINDOW` minutes (default 30) returns the saved answer instead
of paying twice.

## Files and follow-ups

`context_files` lets the agent pass paths instead of pasting file contents, so
a 40k-character file never passes through a small model's own context window.
The server reads only inside `ADVISOR_FILE_ROOTS`, or its working directory
when that is not your home folder or a drive root. It refuses credential-shaped
files (`.env`, keys, anything under `.ssh/` or `.git/`), skips binaries, and
redacts secrets exactly as it does for pasted context.

`follow_up_of` continues an earlier consult: pass the file name from its
`[saved: ...]` line and the advisor sees its earlier question and answer again.

## Configuration

Five layers, highest priority first:

1. **Per-call tool arguments** — `model`, `effort`, `max_tokens`
2. **Runtime overrides** — the `advisor_configure` tool, no restart required
3. **Environment variables** — set by the MCP client, per server entry
4. **A JSON config file** — `ADVISOR_CONFIG`, else `~/.wisdomtooth/config.json`
5. **Built-in defaults**

`ADVISOR_LOCK=1` freezes layers 3–5 and rejects 1–2, for hard cost control.

| Env var | Config key | Default | Meaning |
|---|---|---|---|
| `ADVISOR_PRESET` | `preset` | `medium` | Caller preset by context window: `small` (≤32k — 600-word answers, minimal tools), `medium` (2,000 words), `large` (200k+ — 64,000 words). Explicit settings override it |
| `ADVISOR_BACKEND` | `backend` | `auto` | `auto` / `claude-code` / `api`, or a registered provider |
| `ADVISOR_MODEL` | `model` | `balanced` | Tier alias or full model ID |
| `ADVISOR_EFFORT` | `effort` | (API default) | `low`/`medium`/`high`/`xhigh`/`max` |
| `ADVISOR_ANSWER_BUDGET` | `answer_budget` | preset (`2000`) | Ceiling on answer length in words, presented to Claude as a ceiling, not a target. Works on **both** backends; `0` disables. The only length control the subscription backend has |
| `ADVISOR_MINIMAL_TOOLS` | `minimal_tools` | preset (`0`) | `1` advertises only `ask_wisdomtooth` + `advisor_status`, cutting per-turn tool context from ~3340 to ~1090 tokens |
| `ADVISOR_MAX_TOKENS` | `max_tokens` | `64000` | Answer cap, **API backend only** (the CLI has no such flag), max 128000 |
| `ADVISOR_SAVE_CONSULTS` | `save_consults` | `1` | Write every answer to a Markdown file the user can open. `0` disables |
| `ADVISOR_CONSULT_DIR` | `consult_dir` | `~/.wisdomtooth/consults` | Where those files go |
| `ADVISOR_CONSULT_KEEP` | `consult_keep` | `200` | Keep the newest N transcripts; `0` keeps everything |
| `ADVISOR_USAGE_LOG` | `usage_log` | `1` | Log every consult's tokens, cost and duration to the usage ledger. `0` disables |
| `ADVISOR_USAGE_FILE` | `usage_file` | `~/.wisdomtooth/usage.jsonl` | Where the ledger goes (entries older than 35 days are dropped) |
| `ADVISOR_MAX_CONSULTS_PER_HOUR` / `_PER_5H` / `_PER_WEEK` | `max_consults_per_hour` / `_5h` / `_week` | `0` | Refuse consults past this many in the window. `0` = no cap |
| `ADVISOR_MAX_USD_PER_DAY` | `max_usd_per_day` | `0` | Refuse consults once the last 24 h reach this API-rate cost. `0` = no cap |
| `ADVISOR_REPEAT_WINDOW` | `repeat_window` | `30` | Minutes during which an identical consult returns the saved answer for free. `0` disables |
| `ADVISOR_FILE_ROOTS` | `file_roots` | (working dir) | Folders `context_files` may read, separated by `;` on Windows and `:` elsewhere. Without it, the working directory — unless that is the home folder or a drive root |
| `ADVISOR_SHOW_SUPPORT` | `show_support` | `1` | `0` hides the donation line in the startup banner and transcripts |
| `ADVISOR_TIMEOUT` | `timeout` | `300` | Base seconds before a consult is killed; each consult adds ~10s per 1k chars sent and ~0.06s per answer-budget word, ×1.5/2/2.5 at effort high/xhigh/max |
| `ADVISOR_TIMEOUT_MAX` | `timeout_max` | `3600` | Upper bound on that sized timeout |
| `ADVISOR_TIMEOUT_SCALE` | `timeout_scale` | `1` | Multiplier on the size-based extra time; `0` = flat `ADVISOR_TIMEOUT` |
| `ADVISOR_PROGRESS_INTERVAL` | `progress_interval` | `15` | Seconds between keep-alive progress notifications during a consult; keeps the client's own request timeout from firing |
| `ADVISOR_LOCK` | `lock` | `0` | `1` pins model/effort/tokens |
| `ADVISOR_TIERS_JSON` | `tiers` | — | Remap/extend tiers, e.g. `{"deep":"claude-fable-5-1"}` |
| `ADVISOR_SYSTEM_PROMPT` | `system_prompt` | — | Replace the advisor persona |
| `ADVISOR_SYSTEM_PROMPT_FILE` | `system_prompt_file` | — | Same, from a file |
| `ADVISOR_SYSTEM_PROMPT_EXTRA` | `system_prompt_extra` | — | Append house rules to the built-in persona |
| `ADVISOR_MAX_BUDGET_USD` | `max_budget_usd` | — | Hard spend cap per consult (CLI backend) |
| `ADVISOR_FALLBACK_TO_API` | `fallback_to_api` | `1` | Allow the quota-exhausted fallback in `auto` |
| `ADVISOR_MAX_CONTEXT_CHARS` | `max_context_chars` | `60000` | Truncation cap for `context` |
| `ADVISOR_NSFW_SCRUB` | `nsfw_scrub` | `0` | `1` replaces profanity in outbound text with mild substitutes |
| `ADVISOR_CLAUDE_BIN` | `claude_bin` | (PATH) | Absolute path to `claude` |
| `ADVISOR_TRANSPORT` | `transport` | `stdio` | `stdio` or `http` |
| `ADVISOR_HOST` / `ADVISOR_PORT` | `host` / `port` | `127.0.0.1` / `8484` | HTTP transport bind |
| `ANTHROPIC_API_KEY` | — | — | Only for the API backend |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | — | Durable headless subscription auth. Usually unnecessary — `advisor_login` / `advisor_set_token` store this for you in `~/.wisdomtooth/credentials.json` |

Example `~/.wisdomtooth/config.json`:

```json
{
  "model": "balanced",
  "effort": "high",
  "max_tokens": 24000,
  "system_prompt_extra": "This team writes Rust. Prefer std over crates."
}
```

## Model & effort selection

Every consult tool takes optional `model`, `effort` and `max_tokens`, so **the
calling agent chooses based on question complexity**. Call `advisor_models` to
see the live catalogue.

| Tier | Model | Use for |
|---|---|---|
| `fast` | `claude-haiku-4-5` | Quick sanity checks, factual confirmations |
| `balanced` | `claude-sonnet-5` | Default. Ordinary stuck-on-implementation escalations |
| `deep` | `claude-opus-5` | Architecture, subtle cross-system behavior, debugging that resisted earlier attempts |

Effort scales the same way: `medium` for ordinary advice, `high`/`xhigh` for
hard problems, `max` only when a prior `xhigh` answer was insufficient.

Capability handling is automatic. Effort is only sent to models that accept it
(Haiku has none, so it is skipped) and is clamped to the levels a given model
supports. Adaptive-thinking models get `thinking: {"type": "adaptive"}` —
`budget_tokens` is a 400 on all of them. On Opus 5 and Fable, the server-side
refusal `fallbacks` parameter is requested so a declined consult is rescued
rather than returning nothing. Anything unrecognised degrades to a plain
request instead of erroring.

## Development

```bash
uv venv && uv pip install -e . pytest pytest-asyncio anyio
.venv/Scripts/python -m pytest        # no credentials, no spend
```

The suite covers request shaping against the current Messages API, the CLI
subprocess contract (argv, stdin, environment, failure modes) against a fake
`claude` binary, backend/billing selection, credential handling, and a real
end-to-end MCP stdio session. It passes against mcp 1.x and 2.x, and
anthropic 0.x and 1.x.

It has also been driven end to end by a real local model (Qwen3.8-27B via
LM Studio) using the same prompt assembly Kilo performs: the model escalated
with `ask_wisdomtooth`, populated all three required arguments, chose its own
model/effort tier, and received a usable in-budget answer.

## Design notes

- Stateless: each call is independent; the required args force the caller to
  pass full context, which doubles as the anti-overuse mechanism.
- The advisor's system prompt tells it the caller is a stuck agent, so it
  questions the caller's framing (wrong assumptions) instead of just answering
  the literal question.
- CLI consults run with `--tools ""`, `--strict-mcp-config`,
  `--no-session-persistence` and `--setting-sources ""` in a dedicated empty
  workdir, so a consult cannot touch your files, inherit your `CLAUDE.md`, boot
  your other MCP servers, or recurse into this advisor.
- The prompt travels on **stdin** and the system prompt via
  `--system-prompt-file`, because Windows truncates a long or multi-line argv
  element when the CLI is an npm `.cmd` shim.
- The consult tools return content blocks with no declared output schema.
  Annotating that return type makes mcp 1.x derive a schema and echo every
  block back a second time as structured JSON, while mcp 2.x suppresses it;
  leaving the annotation off behaves identically on both SDK majors.
- If you want the advisor to have its own tools (web search, file access), wrap
  Claude Code instead: `claude mcp serve`.

## Adding a provider

Launch ships Claude only, through the `claude-code` and `api` backends. Each is
a `Backend` in `wisdomtooth/server.py`: a name for `ADVISOR_BACKEND`, the
account it bills, a readiness check, and one
`consult(system, user_content, model, effort, max_tokens) -> str` function. A
ChatGPT or Kimi backend registers the same way with `register_backend(...)`,
reports its tokens through `_note_usage(...)` so the ledger, caps and repeat
guard cover it, and takes full model IDs (the `fast`/`balanced`/`deep` tiers
name Claude models).

## Support the project

Wisdomtooth is free and open source under the Apache-2.0 license (see
`LICENSE`). Donation links will be listed here and on the repository's Sponsor
button once they exist. They appear only where a person reads — this README,
the startup banner and saved transcripts — never in anything sent to your
agent, and `ADVISOR_SHOW_SUPPORT=0` hides them.
