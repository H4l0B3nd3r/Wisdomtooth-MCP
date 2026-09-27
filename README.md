# Wisdomtooth MCP

An MCP server that lets coding agents (Kilo Code, Cursor, Cline, Claude Code,
custom agents) **escalate to a frontier model for advice when they are stuck**
— one stateless call per question.

**Claude is the default advisor, and stays the default.** Other advisors can
be connected alongside it: ChatGPT (the OpenAI API), Gemini (the Google AI
Studio API), OpenRouter, or a local model in LM Studio or Ollama — anything that
speaks the OpenAI chat-completions protocol. The agent picks one per consult
with `advisor=`, or asks two or three at once with `multi_advisor`: the same
question, to compare the answers, or a targeted question to each, matched to
each model's strengths. See [Other advisors](#other-advisors).

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
| `ask_wisdomtooth(question, context, attempts_so_far, [model, effort, max_tokens, context_files, follow_up_of, advisor, confirm_over_limit])` | Stuck on implementation or framework/platform/OS behavior after docs + attempts failed. `context_files` has the server read files itself; `follow_up_of` continues a saved consult; `advisor` picks who answers |
| `review_code(code, concern, [model, effort, max_tokens, context_files, advisor, confirm_over_limit])` | Residual doubt about subtle or security-sensitive code just written |
| `compare_approaches(problem, options, criteria, [model, effort, max_tokens, advisor, confirm_over_limit])` | 2+ viable approaches, tradeoffs unclear after your own analysis. **One** advisor weighs the options |
| `multi_advisor([question, context, attempts_so_far, advisors, targeted_questions, model, effort, context_files, confirm_over_limit])` | **2 or 3 advisors in parallel**: the same question to each (`advisors`), or its own question to each (`targeted_questions`) |
| `advisor_connect(name, provider, [api_key, model, base_url, allowance_tokens, allowance_window, notes, make_default])` | Free. Connects ChatGPT, Gemini, OpenRouter, a local model, or a CLI you installed (Codex, Antigravity, Kilo, Copilot...), checks what it can, and saves it — no restart |
| `advisor_disconnect(name)` | Free. Forgets an advisor added with `advisor_connect`, and its key |
| `advisor_login(force, wait_seconds)` | Free. **Connects the user's Claude subscription over OAuth** — opens the sign-in on their desktop, waits, and switches billing over with no restart |
| `advisor_set_token(token)` | Free. Headless alternative: stores a `claude setup-token` credential privately and applies it immediately |
| `advisor_logout()` | Free. Forgets the stored subscription token |
| `advisor_models()` | Free. Lists tiers, per-model effort support, token ceiling |
| `advisor_configure(model, effort, max_tokens, answer_budget, reset, advisor)` | Free. Changes defaults for this server process — no restart needed; `advisor` switches the default advisor |
| `advisor_status()` | Free. Active backend, billing target, current defaults, every advisor and whether it is ready, what each account has left, a one-line usage summary |
| `advisor_usage(days)` | Free. Consults, tokens and API-rate cost for the last 1 h / 5 h / 24 h / 7 d, per model and per advisor, account balances, plus caps |
| `advisor_auth_check()` | Free. Login/credential diagnosis when a consult fails |

## Install

```bash
uv tool install git+https://github.com/H4l0B3nd3r/Wisdomtooth-MCP   # recommended
# or: pipx install git+https://github.com/H4l0B3nd3r/Wisdomtooth-MCP
```

This puts a `wisdomtooth-mcp` command on your PATH (stdio MCP server).
Python 3.10 or newer is required; `uv` provides one if you have none.

**For subscription billing** (the default), install
[Claude Code](https://claude.com/claude-code), then connect your account. The easy way is to **ask your agent to call the
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

## Other advisors

Claude is always there, as `claude`, and is the default. Any other advisor is
a named entry with a **provider** preset, which fills in the endpoint, the key
variable, the model tiers and what that provider accepts:

| Provider | Endpoint | Key | Default tiers (`fast` / `balanced` / `deep`) |
|---|---|---|---|
| `openai` (ChatGPT) | `https://api.openai.com/v1` | `OPENAI_API_KEY` | `gpt-5.6-luna` / `gpt-5.6-terra` / `gpt-6-astra` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` | `gemini-3.5-flash-lite` / `gemini-3.8-flash` / `gemini-3.1-pro-preview` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | none — set `model` |
| `lmstudio` | `http://localhost:1234/v1` | none | none — set `model` |
| `ollama` | `http://localhost:11434/v1` | none | none — set `model` |
| `openai-compatible` | set `base_url` | optional | none — set `model` |

The model IDs are the ones the providers documented in September 2026; they
drift, so `model` and `tiers` override them per advisor. When a provider
answers "model not found", the error lists the models the endpoint offers.

**CLI advisors.** An advisor can also be another vendor's coding-agent CLI,
installed and signed in by the user. The server runs that install headless,
with whatever sign-in or key the user gave it, and never signs in to anything
itself:

| Provider | Runs | Read-only mode used | Sign-in (done by the user) |
|---|---|---|---|
| `codex` | `codex exec` (OpenAI) | `--sandbox read-only` | `codex` once (ChatGPT plan or API key) |
| `antigravity` | `agy` (Google) | `--sandbox`, no auto-approval | `agy` once |
| `gemini-cli` | `gemini` (Google) | `--approval-mode plan` | `GEMINI_API_KEY` (Google no longer accepts the personal login here) |
| `kilo` | `kilo run` | the `ask` agent | `kilo auth login` |
| `opencode` | `opencode run` | the `plan` agent | `opencode auth login` |
| `qwen` | `qwen` (Qwen Code) | `--approval-mode plan`, read tools excluded | `qwen auth` |
| `copilot` | `copilot` (GitHub) | shell and write tools denied | `copilot login` |
| `cli` | any `command` you name | whatever its `args` say | its own |

Each consult sends the prompt on stdin, runs in an empty folder, reads the
CLI's streamed output (so a stalled CLI is stopped by the idle limit), and
records the tokens the CLI reports. MCP servers the CLI loads cannot loop back:
a Wisdomtooth server started under an advisor CLI refuses to consult. These
CLIs are agents, and their read-only modes are theirs, not this server's: the
Claude backend switches tools off entirely, while a CLI advisor may still read
files if its read-only mode allows it.

```json
{"advisors": {"codex": {"provider": "codex"},
              "agy":   {"provider": "antigravity"},
              "mine":  {"provider": "cli", "command": "/opt/bin/my-agent",
                        "args": ["--print"]}}}
```

**Connecting one.** The user asks the agent, and the agent calls
`advisor_connect`:

```
advisor_connect(name="gemini", provider="gemini", api_key="…")
advisor_connect(name="local", provider="lmstudio", model="qwen/qwen3.8-27b")
```

It lists the endpoint's models (free — no model call), refuses a key the
endpoint rejects, warns about a model the endpoint does not offer, and saves
the advisor to `~/.wisdomtooth/advisors.json` (owner-only, since it holds the
key). The advisor works immediately, with no restart. `advisor_disconnect`
removes it again.

**Or configure them**, in the config file or `ADVISOR_ADVISORS_JSON`:

```json
{
  "advisors": {
    "chatgpt": {"provider": "openai"},
    "gemini":  {"provider": "gemini", "notes": "strong on UI/UX and long context"},
    "local":   {"provider": "ollama", "model": "qwen3:32b"},
    "claude":  {"allowance_tokens": 2000000, "allowance_window": "week"}
  },
  "default_advisor": "claude"
}
```

Per name, `ADVISOR_ADVISORS_JSON` beats the config file, which beats the
store. Other fields: `api_key`, `api_key_env`, `base_url`, `model`, `tiers`,
`efforts` (the reasoning-effort levels to send), `max_tokens_param`,
`send_max_tokens`, `prices` (`[input, output]` USD per million tokens, for cost
estimates), `allowance_tokens` / `allowance_window` (see below) and `notes`,
which `advisor_status` shows so the agent knows what each one is for. The
`claude` entry takes only the allowance and `notes`; Claude's billing is
still `ADVISOR_BACKEND`'s.

**Using them.** `advisor="gemini"` on `ask_wisdomtooth`, `review_code` or
`compare_approaches` sends that consult to Gemini. `model` tiers map to each
advisor's own models, and `effort` is sent as `reasoning_effort`, clamped to
what the provider accepts (nothing is sent to a local model). Everything else
is the same as for Claude: secret redaction, the answer budget, transcripts,
the ledger, the repeat guard and the caps. `advisor_configure(advisor="gemini")`
or `default_advisor` makes one the default.

**`multi_advisor`** consults two or three advisors in parallel:

```
multi_advisor(question="Redis streams or SQS for this queue?",
              context="…", attempts_so_far="…",
              advisors=["claude", "chatgpt", "gemini"])

multi_advisor(context="…", targeted_questions={
    "claude":  "Is the lock-free queue in queue.rs correct?",
    "gemini":  "Is the settings page layout clear?",
    "chatgpt": "Review the whole storage module thoroughly."})
```

It checks every name, every advisor's readiness, a question for each and every
account's balance before sending anything. After that, one advisor failing
costs only its own section. `"chatgpt:gpt-6-astra"` picks a model for one
advisor. The result has one section per advisor, a transcript link for each,
and a note telling the agent how to weigh agreement and disagreement.
`compare_approaches` is the single-advisor tool for choosing between options;
`multi_advisor` is the one that asks several.

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
The plan's own meter, which Claude Code now reports, is covered below.

Optional caps stop an agent that escalates too often before it drains the plan:
`ADVISOR_MAX_CONSULTS_PER_HOUR`, `_PER_5H`, `_PER_WEEK` and
`ADVISOR_MAX_USD_PER_DAY`. A capped consult is refused before anything is
sent, and the error says when the next slot opens. An identical consult inside
`ADVISOR_REPEAT_WINDOW` minutes (default 30) returns the saved answer instead
of paying twice.

### Account balances, and consults held for the user

`advisor_status` and `advisor_usage` list what each connected account has
left, from whichever source is real for it:

| Account | Source | Shown as |
|---|---|---|
| Claude subscription | Claude Code streams a `rate_limit_event` with the plan's own 5-hour and 7-day utilization | `5 h 7% used (resets 14:00), 7 d 18% used` |
| OpenRouter | `GET /key` on the key | `$4.20 credit left of $10.00` |
| OpenAI and most hosted APIs | `x-ratelimit-*` response headers | `29,000 of 30,000 tokens/min left` |
| Any advisor | an allowance the user declares (`allowance_tokens` + `allowance_window` of `hour`, `5h`, `day`, `week` or `month`), measured by the ledger | `≈180,000 of 2,000,000 tokens left this week` |

The plan meter is a share of the plan, not a token count. So the server also
learns what a percentage point costs, by setting each consult's API-equivalent
cost against how far it moved the meter. Anything else using the plan at the
same time inflates that figure, which errs towards asking rather than
overspending. OpenAI and Gemini expose no credit-balance API; declare an
allowance to track them.

Answers carry a short line with the same data, such as
`[plan: 5 h 31% used · 7 d 44% used]` or
`[balance: ≈8,460 of 100,000 tokens left this day (8% left) -- LOW; tell the user]`.

**A consult is held for the user IF AND ONLY IF its estimated cost exceeds
what the account has left.** Before sending, the server estimates the request
(≈4 characters a token in, the answer budget plus thinking out) and checks it
against the declared allowance, the plan meter (a full window, or one whose
learned rate says the request will not fit) and an OpenRouter credit balance.
If it would go over, nothing is sent. The agent gets a `HELD` error that names
the account, the estimate and what is left, and tells it to ask the user; if
they agree, the agent repeats the call with `confirm_over_limit=true`. A
balance that is low but still covers the request is only a warning in the
footer. The rate-limit headers never hold a consult, because they refill
within a minute. `multi_advisor` checks every account first and holds the
whole call, so nothing is half-spent.

## Files and follow-ups

`context_files` lets the agent pass paths instead of pasting file contents, so
a 40k-character file never passes through a small model's own context window.
The server reads only inside `ADVISOR_FILE_ROOTS`, or its working directory
when that is not your home folder or a drive root. It refuses credential-shaped
files (`.env`, keys, anything under `.ssh/` or `.git/`), skips binaries, and
redacts secrets exactly as it does for pasted context.

`follow_up_of` continues an earlier consult: pass the file name from its
`[saved: ...]` line and the advisor sees its earlier question and answer again.

## Checking the setup

`wisdomtooth-mcp doctor` checks the Claude CLI and your login without spending
anything, then prints a ready-to-paste config for Kilo, OpenCode, Claude Code,
Claude Desktop, Cursor, Cline and Codex, pointed at the executable that is
actually installed. `--client kilo` prints just one; `--preset small` adds the
caller preset for a small local model.

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
| `ADVISOR_TRIM_ANSWERS` | `trim_answers` | `1` | When an answer runs past 1.5× the budget, the caller gets its lead and a pointer to the saved transcript, which keeps the whole answer. Needs saved consults; `0` disables |
| `ADVISOR_MINIMAL_TOOLS` | `minimal_tools` | preset (`0`) | `1` advertises only `ask_wisdomtooth` + `advisor_status`, cutting per-turn tool context from ~6,200 to ~1,300 tokens |
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
| `ADVISOR_IDLE_TIMEOUT` | `idle_timeout` | `300` | Seconds the `claude` CLI may go without streaming any output before the consult is stopped. A working consult streams every few seconds, even while thinking, so this catches a hang long before `ADVISOR_TIMEOUT_MAX`. `0` disables |
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
| `ADVISOR_HOST` / `ADVISOR_PORT` | `host` / `port` | `127.0.0.1` / `8484` | HTTP transport bind. Anything beyond loopback needs `ADVISOR_HTTP_TOKEN` |
| `ADVISOR_HTTP_TOKEN` | `http_token` | — | Bearer token every HTTP request must carry (`Authorization: Bearer <token>`) |
| `ADVISOR_HTTP_NO_AUTH` | `http_no_auth` | `0` | `1` allows a non-loopback bind without a token, when a proxy or firewall already guards the port |
| `ADVISOR_ADVISORS_JSON` | `advisors` | — | Advisors besides Claude, as a JSON object of `name → {provider, …}` (see [Other advisors](#other-advisors)) |
| `ADVISOR_DEFAULT_ADVISOR` | `default_advisor` | `claude` | Which advisor a consult without `advisor=` goes to |
| `ADVISOR_ADVISORS_FILE` | `advisors_file` | `~/.wisdomtooth/advisors.json` | Where `advisor_connect` saves advisors and their keys (owner-only) |
| `ADVISOR_ACCOUNTS_FILE` | `accounts_file` | `~/.wisdomtooth/accounts.json` | The last reading of each account's meter and balance |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | — | — | Keys the `openai` / `gemini` / `openrouter` presets read, unless an advisor has its own `api_key` or `api_key_env` |
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
.venv/Scripts/python -m pytest        # bin/ instead of Scripts/ outside Windows
```

The suite needs no credentials and spends nothing. It covers request shaping
against the Messages API, the CLI subprocess contract (argv, stdin,
environment, failure modes) against a fake `claude` binary, backend and
billing selection, credential handling, the OpenAI-compatible client against
a local fake endpoint, and a real MCP stdio session. CI runs it on Linux,
Windows and macOS against both mcp 1.x and 2.x.

`docs/INTERNALS.md` explains the non-obvious decisions and the bugs they
prevent; read it before changing how the CLI is run. Changes are listed in
`CHANGELOG.md`.

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

Any provider with an OpenAI-compatible chat-completions endpoint needs no code:
it is an `openai-compatible` advisor with a `base_url` and a `model`. A preset
for a common one is an entry in `PROVIDERS` in `wisdomtooth/advisors.py`: its
endpoint, key variable, tiers, the effort levels it accepts, which max-tokens
parameter it expects, and whether it reports a balance.

A provider that needs a different protocol, such as another CLI on the user's
own plan, is a `Backend` in `wisdomtooth/server.py`: a name, the account it
bills, a readiness check, and one
`consult(system, user_content, model, effort, max_tokens) -> str` function,
registered with `register_backend(...)`. It reports tokens through
`_note_usage(...)`, so the ledger, caps and repeat guard cover it.

## Support the project

Wisdomtooth is free and open source under the Apache-2.0 license (see
`LICENSE`). Donation links will be listed here and on the repository's Sponsor
button once they exist. They appear only where a person reads — this README,
the startup banner and saved transcripts — never in anything sent to your
agent, and `ADVISOR_SHOW_SUPPORT=0` hides them.
