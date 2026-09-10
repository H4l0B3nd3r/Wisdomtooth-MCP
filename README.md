# Claude Advisor MCP

An MCP server that lets coding agents (Kilo Code, Cursor, Cline, Claude Code,
custom agents) **escalate to Claude for advice when they are stuck** — one
stateless call per question.

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
2. **Required `attempts_so_far` argument** — `ask_claude` cannot be called
   without stating what was already tried and what Context7/docs returned.
3. **Kilo rules file** — `.kilocode/rules/claude-advisor.md` gives the agent
   the full escalation policy as standing instructions.

**New here?** Read `USAGE.md` — setup, tuning for local models, and
troubleshooting. This file is the reference.

## Tools

| Tool | Purpose |
|---|---|
| `ask_claude(question, context, attempts_so_far, [model, effort, max_tokens])` | Stuck on implementation or framework/platform/OS behavior after docs + attempts failed |
| `review_code(code, concern, [model, effort, max_tokens])` | Residual doubt about subtle or security-sensitive code just written |
| `compare_approaches(problem, options, criteria, [model, effort, max_tokens])` | 2+ viable approaches, tradeoffs unclear after your own analysis |
| `advisor_login(force, wait_seconds)` | Free. **Connects the user's Claude subscription over OAuth** — opens the sign-in on their desktop, waits, and switches billing over with no restart |
| `advisor_set_token(token)` | Free. Headless alternative: stores a `claude setup-token` credential privately and applies it immediately |
| `advisor_logout()` | Free. Forgets the stored subscription token |
| `advisor_models()` | Free. Lists tiers, per-model effort support, token ceiling |
| `advisor_configure(model, effort, max_tokens, reset)` | Free. Changes defaults for this server process — no restart needed |
| `advisor_status()` | Free. Active backend, billing target, current defaults |
| `advisor_auth_check()` | Free. Login/credential diagnosis when a consult fails |

## Install

```bash
uv tool install /path/to/claude-advisor-mcp     # recommended
# or: pipx install /path/to/claude-advisor-mcp
```

This puts a `claude-advisor-mcp` command on your PATH (stdio MCP server).

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
stores it in `~/.claude-advisor/credentials.json` (owner-only) and injects it
into every consult, so it survives restarts and reduced GUI environments
without ever appearing in your MCP client's config.

## Add to a client

**Claude Code**

```bash
claude mcp add claude-advisor -- claude-advisor-mcp
```

**Kilo Code (current)** — merge `kilo-configs/kilo.subscription.jsonc` into your
project's `kilo.jsonc` under the `mcp` key, or use Settings → MCP → Add Server →
Local (stdio), command `claude-advisor-mcp`.

**Kilo Code (classic)** — copy `kilo-configs/mcp.json` to `.kilocode/mcp.json`.

**Cursor / Windsurf / Claude Desktop** — standard `mcpServers` JSON with command
`claude-advisor-mcp`; same shape as `kilo-configs/mcp.json`.

**Escalation policy** — copy `.kilocode/rules/claude-advisor.md` into your
project's `.kilocode/rules/`. Kilo loads these as standing instructions, so the
agent knows to try Context7 and its own fixes first.

Tip: leave `ask_claude` **off** any auto-approve/`alwaysAllow` list at first.
Seeing each escalation request tells you whether the agent is respecting the
policy; auto-approve later if it behaves.

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

## Configuration

Five layers, highest priority first:

1. **Per-call tool arguments** — `model`, `effort`, `max_tokens`
2. **Runtime overrides** — the `advisor_configure` tool, no restart required
3. **Environment variables** — set by the MCP client, per server entry
4. **A JSON config file** — `ADVISOR_CONFIG`, else `~/.claude-advisor/config.json`
5. **Built-in defaults**

`ADVISOR_LOCK=1` freezes layers 3–5 and rejects 1–2, for hard cost control.

| Env var | Config key | Default | Meaning |
|---|---|---|---|
| `ADVISOR_BACKEND` | `backend` | `auto` | `auto` / `claude-code` / `api` |
| `ADVISOR_MODEL` | `model` | `balanced` | Tier alias or full model ID |
| `ADVISOR_EFFORT` | `effort` | (API default) | `low`/`medium`/`high`/`xhigh`/`max` |
| `ADVISOR_ANSWER_BUDGET` | `answer_budget` | `600` | Target answer length in words. Works on **both** backends; `0` disables. The only length control the subscription backend has |
| `ADVISOR_MINIMAL_TOOLS` | `minimal_tools` | `0` | `1` advertises only `ask_claude` + `advisor_status`, cutting per-turn tool context from ~3340 to ~1090 tokens |
| `ADVISOR_MAX_TOKENS` | `max_tokens` | `16000` | Answer cap, **API backend only** (the CLI has no such flag), max 128000 |
| `ADVISOR_TIMEOUT` | `timeout` | `180` | Seconds before a consult is killed |
| `ADVISOR_LOCK` | `lock` | `0` | `1` pins model/effort/tokens |
| `ADVISOR_TIERS_JSON` | `tiers` | — | Remap/extend tiers, e.g. `{"deep":"claude-fable-5-1"}` |
| `ADVISOR_SYSTEM_PROMPT` | `system_prompt` | — | Replace the advisor persona |
| `ADVISOR_SYSTEM_PROMPT_FILE` | `system_prompt_file` | — | Same, from a file |
| `ADVISOR_SYSTEM_PROMPT_EXTRA` | `system_prompt_extra` | — | Append house rules to the built-in persona |
| `ADVISOR_MAX_BUDGET_USD` | `max_budget_usd` | — | Hard spend cap per consult (CLI backend) |
| `ADVISOR_FALLBACK_TO_API` | `fallback_to_api` | `1` | Allow the quota-exhausted fallback in `auto` |
| `ADVISOR_MAX_CONTEXT_CHARS` | `max_context_chars` | `60000` | Truncation cap for `context` |
| `ADVISOR_NSFW_SCRUB` | `nsfw_scrub` | `1` | `0` disables outbound word scrubbing |
| `ADVISOR_CLAUDE_BIN` | `claude_bin` | (PATH) | Absolute path to `claude` |
| `ADVISOR_TRANSPORT` | `transport` | `stdio` | `stdio` or `http` |
| `ADVISOR_HOST` / `ADVISOR_PORT` | `host` / `port` | `127.0.0.1` / `8484` | HTTP transport bind |
| `ANTHROPIC_API_KEY` | — | — | Only for the API backend |
| `CLAUDE_CODE_OAUTH_TOKEN` | — | — | Durable headless subscription auth. Usually unnecessary — `advisor_login` / `advisor_set_token` store this for you in `~/.claude-advisor/credentials.json` |

Example `~/.claude-advisor/config.json`:

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
.venv/Scripts/python -m pytest        # 196 tests, no credentials, no spend
```

The suite covers request shaping against the current Messages API, the CLI
subprocess contract (argv, stdin, environment, failure modes) against a fake
`claude` binary, backend/billing selection, credential handling, and a real
end-to-end MCP stdio session. It passes against mcp 1.x and 2.x, and
anthropic 0.x and 1.x.

It has also been driven end to end by a real local model (Qwen3.8-27B via
LM Studio) using the same prompt assembly Kilo performs: the model escalated
with `ask_claude`, populated all three required arguments, chose its own
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
- If you want the advisor to have its own tools (web search, file access), wrap
  Claude Code instead: `claude mcp serve`.
