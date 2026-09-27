# Wisdomtooth MCP

Wisdomtooth is an MCP server that gives a coding agent (Kilo Code, Cursor,
Cline, Claude Code, or one you wrote yourself) someone to ask when it gets
stuck. The agent sends one self-contained question to a frontier model and
gets advice back; nothing carries over between calls.

**Claude is the default advisor.** You can connect others alongside it:
ChatGPT (the OpenAI API), Gemini (the Google AI Studio API), OpenRouter, a
local model in LM Studio or Ollama, anything else that speaks the OpenAI
chat-completions protocol, or another vendor's coding-agent CLI you have
installed. The agent picks an advisor per consult with `advisor=`, or asks two
or three at once with `multi_advisor`, either the same question to compare
answers or a different question to each, matched to what each model is good
at. See [Other advisors](#other-advisors).

Claude consults use an **Anthropic API key** (`ANTHROPIC_API_KEY`, billed per
token). Wisdomtooth also **works with your own Claude Code install**: with no
key set, it runs the `claude` CLI you have already signed in to. Every answer
ends with a note saying which account paid for it.

Wisdomtooth is meant as a last resort, not a first stop. The agent should
reach for it when it is struggling to implement something or to understand a
framework, platform or OS, **and** its other options (Context7, docs, web
search, its own attempts) have not helped. Agents don't follow a policy just
because it exists, so it is enforced in three places:

1. **Tool descriptions.** Every tool's MCP description opens with explicit
   WHEN TO USE and DO NOT USE criteria, which agents always see.
2. **A required `attempts_so_far` argument.** `ask_wisdomtooth` can't be
   called without saying what was already tried and what Context7 or the docs
   returned.
3. **A rules file.** `.kilocode/rules/wisdomtooth.md` gives the agent the full
   escalation policy as standing instructions.

**It's also a reviewer, not only a lifeline.** Wisdomtooth works just as well
as a second pair of eyes on work that is going fine. `review_code` asks an
advisor to critique code the agent has just written, with an optional focus
such as security, performance or async correctness. `compare_approaches` gets
an outside opinion before the agent commits to a design, and `multi_advisor`
collects reviews from two or three different models at once. Out of the box,
the tool descriptions steer the agent towards reviews only when it has real
doubts about subtle or security-sensitive code. If you want feedback more
often, say so: ask the agent to "have Wisdomtooth review this before you
finish", or add a line to your rules file, such as "run `review_code` on every
non-trivial change before calling the task done".

**New here?** Start with `USAGE.md`, which covers setup, tuning for local
models and troubleshooting. This README is the reference.

## Tools

Tools marked *free* never call a model, so they cost nothing to run.

| Tool | What it's for |
|---|---|
| `ask_wisdomtooth(question, context, attempts_so_far, [model, effort, max_tokens, context_files, follow_up_of, advisor, confirm_over_limit])` | The agent is stuck on an implementation or on framework, platform or OS behavior, and docs plus its own attempts have failed. `context_files` has the server read files itself, `follow_up_of` continues a saved consult, and `advisor` picks who answers |
| `review_code(code, concern, [model, effort, max_tokens, context_files, advisor, confirm_over_limit])` | A second opinion on subtle or security-sensitive code the agent just wrote |
| `compare_approaches(problem, options, criteria, [model, effort, max_tokens, advisor, confirm_over_limit])` | Two or more viable approaches whose tradeoffs are still unclear after the agent's own analysis. **One** advisor weighs the options |
| `multi_advisor([question, context, attempts_so_far, advisors, targeted_questions, model, effort, context_files, confirm_over_limit])` | **Two or three advisors in parallel**: the same question to each (`advisors`), or a different question to each (`targeted_questions`) |
| `advisor_connect(name, provider, [api_key, model, base_url, command, allowance_tokens, allowance_window, notes, make_default])` | *Free.* Connects ChatGPT, Gemini, OpenRouter, a local model, or a CLI you installed (Codex, Antigravity, Kilo, Copilot and others), checks what it can, and saves it. No restart needed |
| `advisor_disconnect(name)` | *Free.* Forgets an advisor added with `advisor_connect`, along with its key |
| `advisor_models()` | *Free.* Lists the model tiers, which efforts each model supports, and the token ceiling |
| `advisor_configure(model, effort, max_tokens, answer_budget, reset, advisor)` | *Free.* Changes the defaults for this server process without a restart. `advisor` switches the default advisor |
| `advisor_status()` | *Free.* The active backend and who it bills, the current defaults, every advisor and whether it's ready, what each account has left, and a one-line usage summary |
| `advisor_usage(days)` | *Free.* Consults, tokens and API-rate cost for the last 1 h, 5 h, 24 h and 7 d, broken down by model and by advisor, plus account balances and caps |
| `advisor_auth_check()` | *Free.* Diagnoses credentials and sign-in when a consult fails |

## Install

```bash
uv tool install wisdomtooth-mcp          # recommended
# or: pipx install wisdomtooth-mcp
```

This puts a `wisdomtooth-mcp` command (a stdio MCP server) on your PATH. It
needs Python 3.10 or newer; `uv` will fetch one if you don't have it. If you'd
rather use Docker, skip this step and see [Docker](#docker).

Next, give it a way to reach Claude. Either:

- **An Anthropic API key** (the default). Create one at
  [console.anthropic.com](https://console.anthropic.com) and set it as
  `ANTHROPIC_API_KEY` in the server's environment. Consults are billed per
  token.
- **Your own Claude Code install.** If
  [Claude Code](https://claude.com/claude-code) is installed and signed in (run
  `claude` once), Wisdomtooth uses it whenever no API key is set. It runs your
  install exactly as you signed it in; Wisdomtooth never signs in to anything
  itself.

Run `wisdomtooth-mcp doctor` to see what it found and get a client config you
can paste straight in.

## Add to a client

**Claude Code**

```bash
claude mcp add wisdomtooth --env ANTHROPIC_API_KEY=sk-ant-... -- wisdomtooth-mcp
```

**Kilo Code:** merge `kilo-configs/kilo.jsonc` into your project's
`kilo.jsonc` under the `mcp` key. If you're using your Claude Code install
instead of a key, use `kilo.claude-code.jsonc`.

**Cursor, Windsurf, Cline, Claude Desktop:** use the standard `mcpServers`
JSON with the command `wisdomtooth-mcp`. See `kilo-configs/mcp.json`, or run
`wisdomtooth-mcp doctor --client cursor`.

**The escalation policy:** copy `.kilocode/rules/wisdomtooth.md` into your
project's `.kilocode/rules/` (or wherever your client reads rules), so the
agent knows to try Context7 and its own fixes first.

Tip: keep `ask_wisdomtooth` **off** any auto-approve or `alwaysAllow` list at
first. Approving each request yourself shows you whether the agent is
respecting the policy. Once it behaves, you can auto-approve it.

## Who gets billed

`ADVISOR_BACKEND` decides, and `advisor_status` shows the current answer.

| Value | Behavior |
|---|---|
| `auto` (default) | With `ANTHROPIC_API_KEY` (or an Anthropic SDK profile), the **Anthropic API**, billed per token. Without one, your own **Claude Code install**, if it's signed in. If neither is available, every consult fails with instructions instead of guessing. |
| `api` | Always the Anthropic API. |
| `claude-code` | Always your Claude Code install. `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are removed from its environment, so it uses the install's own sign-in. |

The server never switches you from one to the other on its own. If Claude Code
reports that its usage limit has been reached, the consult fails and says so.

## Docker

The image uses the API backend, since a container has no Claude Code sign-in:

```bash
docker run -i --rm -e ANTHROPIC_API_KEY ghcr.io/h4l0b3nd3r/wisdomtooth-mcp
```

That's a stdio server, so an MCP client can launch it directly; see
`kilo-configs/kilo.docker.jsonc`. To run one long-lived server shared by
several clients, use the HTTP transport instead. It requires a bearer token as
soon as it listens beyond localhost:

```bash
docker run -d --name wisdomtooth -p 127.0.0.1:8484:8484 \
  -e ANTHROPIC_API_KEY -e ADVISOR_TRANSPORT=http -e ADVISOR_HOST=0.0.0.0 \
  -e ADVISOR_HTTP_TOKEN=<a long random string> \
  -v wisdomtooth-state:/home/wisdomtooth/.wisdomtooth \
  ghcr.io/h4l0b3nd3r/wisdomtooth-mcp
```

The volume keeps transcripts and the usage ledger across restarts. Advisors
reached over HTTP (ChatGPT, Gemini, OpenRouter) work inside the container, but
CLI advisors and `context_files` need to run on the host.

## Other advisors

Claude is always available, under the name `claude`, and is the default. Every
other advisor is a named entry built on a **provider** preset, which fills in
the endpoint, the key variable, the model tiers and what that provider
accepts:

| Provider | Endpoint | Key | Default tiers (`fast` / `balanced` / `deep`) |
|---|---|---|---|
| `openai` (ChatGPT) | `https://api.openai.com/v1` | `OPENAI_API_KEY` | `gpt-5.6-luna` / `gpt-5.6-terra` / `gpt-6-astra` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` | `gemini-3.5-flash-lite` / `gemini-3.8-flash` / `gemini-3.1-pro-preview` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | none; set `model` |
| `lmstudio` | `http://localhost:1234/v1` | none | none; set `model` |
| `ollama` | `http://localhost:11434/v1` | none | none; set `model` |
| `openai-compatible` | set `base_url` | optional | none; set `model` |

These model IDs are the ones each provider documented in September 2026.
Model names change over time, so you can override them per advisor with
`model` and `tiers`. If a provider answers "model not found", the error lists
the models that endpoint does offer.

**CLI advisors.** An advisor can also be another vendor's coding-agent CLI
that you have installed and signed in to yourself. The server runs that
install headless, with whatever sign-in or key you gave it, and never signs in
to anything itself:

| Provider | Runs | Read-only mode used | Sign-in (done by you) |
|---|---|---|---|
| `codex` | `codex exec` (OpenAI) | `--sandbox read-only` | run `codex` once (ChatGPT plan or API key) |
| `antigravity` | `agy` (Google) | `--sandbox`, no auto-approval | run `agy` once |
| `gemini-cli` | `gemini` (Google) | `--approval-mode plan` | `GEMINI_API_KEY` (Google no longer accepts the personal login here) |
| `kilo` | `kilo run` | the `ask` agent | `kilo auth login` |
| `opencode` | `opencode run` | the `plan` agent | `opencode auth login` |
| `qwen` | `qwen` (Qwen Code) | `--approval-mode plan`, read tools excluded | `qwen auth` |
| `copilot` | `copilot` (GitHub) | shell and write tools denied | `copilot login` |
| `cli` | any `command` you name | whatever its `args` say | its own |

For each consult the server sends the prompt on stdin, runs the CLI in an
empty folder, and reads its streamed output, so a CLI that stalls is stopped
by the idle limit. It records the token counts the CLI reports. MCP servers
the CLI loads can't loop back: a Wisdomtooth server started under an advisor
CLI refuses to consult.

Bear in mind that these CLIs are agents in their own right, and their
read-only modes are their own, not this server's. The Claude backend turns
tools off entirely, but a CLI advisor may still read files if its read-only
mode allows it.

```json
{"advisors": {"codex": {"provider": "codex"},
              "agy":   {"provider": "antigravity"},
              "mine":  {"provider": "cli", "command": "/opt/bin/my-agent",
                        "args": ["--print"]}}}
```

**Connecting an advisor.** You ask the agent, and the agent calls
`advisor_connect`:

```
advisor_connect(name="gemini", provider="gemini", api_key="…")
advisor_connect(name="local", provider="lmstudio", model="qwen/qwen3.8-27b")
```

It lists the endpoint's models (free; no model is called), refuses a key the
endpoint rejects, and warns if the endpoint doesn't offer the model you named.
It then saves the advisor to `~/.wisdomtooth/advisors.json`, which only you can
read because it holds the key. The advisor works right away, with no restart.
`advisor_disconnect` removes it again.

**Or configure advisors directly**, in the config file or in
`ADVISOR_ADVISORS_JSON`:

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

When the same name appears in more than one place, `ADVISOR_ADVISORS_JSON`
wins over the config file, which wins over advisors saved with
`advisor_connect`. Each entry can also set:

- `api_key` or `api_key_env`, `base_url`, `model` and `tiers`;
- `efforts`: the reasoning-effort levels to send;
- `max_tokens_param` and `send_max_tokens`: how the answer limit is sent;
- `prices`: `[input, output]` in USD per million tokens, for cost estimates;
- `allowance_tokens` and `allowance_window` (see
  [Account balances](#account-balances-and-consults-held-for-the-user));
- `notes`, which `advisor_status` shows so the agent knows what each advisor
  is for.

The `claude` entry accepts only the allowance and `notes`. Who pays for Claude
is still decided by `ADVISOR_BACKEND`.

**Using them.** Pass `advisor="gemini"` to `ask_wisdomtooth`, `review_code`
or `compare_approaches` to send that consult to Gemini. `model` tiers map to
each advisor's own models, and `effort` is sent as `reasoning_effort`, clamped
to what the provider accepts (local models get none). Everything else works
exactly as it does for Claude: secret redaction, the answer budget,
transcripts, the usage ledger, the repeat guard and the caps. To make another
advisor the default, call `advisor_configure(advisor="gemini")` or set
`default_advisor`.

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

Before sending anything, it checks every name, that each advisor is ready and
has a question, and every account's balance. Once the consults are under way,
one advisor failing costs only its own section. To pick a model for a single
advisor, write it as `"chatgpt:gpt-6-astra"`. The result has one section per
advisor, a transcript link for each, and a note telling the agent how to weigh
agreement and disagreement. To choose between options with a single advisor,
use `compare_approaches`; `multi_advisor` is for asking several.

## Where the answer ends up

The advisor's answer comes back as an MCP tool result inside your agent's chat,
and what happens to it there is out of this server's hands. Editors collapse
the block, small local models paraphrase it into a summary, and a context trim
eventually deletes it. An answer you paid for can be surprisingly hard to find
again.

So every consult is also saved as a Markdown file:

```
~/.wisdomtooth/consults/20260209-142233-ask-wisdomtooth-why-does-the-datagrid-flicker.md
```

Each file holds exactly what was sent (after secret redaction), exactly what
came back, and which account paid. You get the path in two ways:

- **In the answer's footer**, as plain text. Every client shows it, and the
  tool description tells the agent to pass the path on to you.
- **As a `resource_link` content block** marked `audience: ["user"]`, for
  clients that can turn it into something you can click.

The newest 200 files are kept and older ones are deleted. Set
`ADVISOR_SAVE_CONSULTS=0` to turn saving off, or `ADVISOR_CONSULT_DIR` to save
somewhere else. `advisor_status` shows the folder in use.

Why a file? MCP gives a server no other way to put something in front of a
person. There is no "open this panel" call, and `notifications/message` only
moves the problem into the client's log pane. A file is the one channel that
outlives the chat window.

## Usage, spend and caps

Every consult is logged to `~/.wisdomtooth/usage.jsonl` with its tokens, how
long it took and what it would cost at API rates. The answer footer shows the
same figures:

```
[usage: 14,210 tokens in · 2,130 out · 48s · ≈$0.12 at API rates]
```

`advisor_usage` totals the last hour, 5 hours, 24 hours and 7 days (the
windows Claude plans are metered over) and breaks the week down by model;
`advisor_status` includes a one-line version. Consults through Claude Code
aren't billed per token, so for them the dollar figure is a stand-in for how
much of your plan's allowance a consult used. Claude Code also reports the
plan's own meter; see below.

Optional caps stop an agent that escalates too often before it runs up your
bill or drains your plan: `ADVISOR_MAX_CONSULTS_PER_HOUR`, `_PER_5H`,
`_PER_WEEK` and `ADVISOR_MAX_USD_PER_DAY`. A capped consult is refused before
anything is sent, and the error says when the next slot opens. If the agent
repeats an identical consult within `ADVISOR_REPEAT_WINDOW` minutes (30 by
default), it gets the saved answer back instead of paying twice.

### Account balances, and consults held for the user

`advisor_status` and `advisor_usage` show what each connected account has
left, using whichever source actually knows:

| Account | Source | Shown as |
|---|---|---|
| Claude Code | Claude Code streams a `rate_limit_event` with the plan's own 5-hour and 7-day utilization | `5 h 7% used (resets 14:00), 7 d 18% used` |
| OpenRouter | `GET /key` on the key | `$4.20 credit left of $10.00` |
| OpenAI and most hosted APIs | `x-ratelimit-*` response headers | `29,000 of 30,000 tokens/min left` |
| Any advisor | an allowance you declare (`allowance_tokens` plus an `allowance_window` of `hour`, `5h`, `day`, `week` or `month`), measured against the usage ledger | `≈180,000 of 2,000,000 tokens left this week` |

The plan meter reports a share of the plan, not a token count, so the server
also learns what one percentage point costs, by comparing each consult's
API-equivalent cost with how far it moved the meter. Anything else using the
plan at the same time inflates that figure, which errs on the side of asking
you rather than overspending. OpenAI and Gemini have no API for checking your
credit balance, so declare an allowance if you want to track them.

Answers carry a short line with the same information, for example
`[plan: 5 h 31% used · 7 d 44% used]` or
`[balance: ≈8,460 of 100,000 tokens left this day (8% left) -- LOW; tell the user]`.

**A consult is held for you if, and only if, its estimated cost is more than
the account has left.** Before sending, the server estimates the request
(about 4 characters per input token; the answer budget plus thinking for
output) and checks it against the declared allowance, the plan meter (a full
window, or one whose learned rate says the request won't fit) and any
OpenRouter credit balance. If it would go over, nothing is sent. The agent
gets a `HELD` error naming the account, the estimate and what's left, and is
told to ask you. If you agree, it repeats the call with
`confirm_over_limit=true`. A balance that is low but still covers the request
only adds a warning to the footer. Rate-limit headers never hold a consult,
because they refill within a minute. `multi_advisor` checks every account
first and holds the whole call, so nothing is left half-spent.

## Files and follow-ups

With `context_files`, the agent passes file paths instead of pasting file
contents, so a 40,000-character file never has to pass through a small
model's context window. The server only reads inside `ADVISOR_FILE_ROOTS`, or
inside its working directory if that isn't your home folder or a drive root.
It refuses credential-like files (`.env`, keys, anything under `.ssh/` or
`.git/`), skips binaries, and redacts secrets just as it does for pasted
context.

`follow_up_of` continues an earlier consult. Pass the file name from its
`[saved: ...]` line and the advisor sees its earlier question and answer
again.

## Checking the setup

`wisdomtooth-mcp doctor` shows which account would be billed, whether it can
find an API key and a signed-in `claude` CLI, and any environment variables
that would change that, all without spending anything. It then prints a
ready-to-paste config for Kilo, OpenCode, Claude Code, Claude Desktop, Cursor,
Cline and Codex, pointing at the executable that is actually installed. Add
`--client kilo` to print just one, or `--preset small` to include the preset
for a small local model.

## Configuration

Settings come from five layers. Higher layers win:

1. **Per-call tool arguments:** `model`, `effort`, `max_tokens`
2. **Runtime overrides:** the `advisor_configure` tool, with no restart
3. **Environment variables:** set by the MCP client for each server entry
4. **A JSON config file:** `ADVISOR_CONFIG`, or `~/.wisdomtooth/config.json`
5. **Built-in defaults**

For strict cost control, `ADVISOR_LOCK=1` freezes layers 3–5 and rejects
changes from layers 1 and 2.

| Env var | Config key | Default | Meaning |
|---|---|---|---|
| `ADVISOR_PRESET` | `preset` | `medium` | A preset sized to the calling model's context window: `small` (up to 32k; 600-word answers, minimal tools), `medium` (2,000-word answers), `large` (200k and up; 64,000-word answers). Anything you set explicitly overrides it |
| `ADVISOR_BACKEND` | `backend` | `auto` | `auto`, `claude-code` or `api`, or a registered provider |
| `ADVISOR_MODEL` | `model` | `balanced` | A tier name or a full model ID |
| `ADVISOR_EFFORT` | `effort` | (the API's default) | `low`, `medium`, `high`, `xhigh` or `max` |
| `ADVISOR_ANSWER_BUDGET` | `answer_budget` | the preset's (`2000`) | Maximum answer length in words, given to the advisor as a ceiling, not a target. Works on **both** backends, and is the only length control the Claude Code backend has. `0` turns it off |
| `ADVISOR_TRIM_ANSWERS` | `trim_answers` | `1` | When an answer runs past 1.5× the budget, the agent gets the opening part and a pointer to the saved transcript, which keeps the whole answer. Needs saved consults. `0` turns it off |
| `ADVISOR_MINIMAL_TOOLS` | `minimal_tools` | the preset's (`0`) | `1` advertises only `ask_wisdomtooth` and `advisor_status`, cutting the tool descriptions sent every turn from about 6,200 tokens to about 1,300 |
| `ADVISOR_MAX_TOKENS` | `max_tokens` | `64000` | Cap on answer tokens, up to 128000. Ignored by Claude Code and CLI advisors, which have no such setting |
| `ADVISOR_SAVE_CONSULTS` | `save_consults` | `1` | Save every answer to a Markdown file you can open. `0` turns it off |
| `ADVISOR_CONSULT_DIR` | `consult_dir` | `~/.wisdomtooth/consults` | Where those files go |
| `ADVISOR_CONSULT_KEEP` | `consult_keep` | `200` | How many of the newest transcripts to keep. `0` keeps them all |
| `ADVISOR_USAGE_LOG` | `usage_log` | `1` | Log each consult's tokens, cost and duration to the usage ledger. `0` turns it off |
| `ADVISOR_USAGE_FILE` | `usage_file` | `~/.wisdomtooth/usage.jsonl` | Where the ledger goes. Entries older than 35 days are dropped |
| `ADVISOR_MAX_CONSULTS_PER_HOUR` / `_PER_5H` / `_PER_WEEK` | `max_consults_per_hour` / `_5h` / `_week` | `0` | Refuse consults beyond this many in the window. `0` means no cap |
| `ADVISOR_MAX_USD_PER_DAY` | `max_usd_per_day` | `0` | Refuse consults once the last 24 hours reach this cost at API rates. `0` means no cap |
| `ADVISOR_REPEAT_WINDOW` | `repeat_window` | `30` | Minutes during which an identical consult returns the saved answer for free. `0` turns it off |
| `ADVISOR_FILE_ROOTS` | `file_roots` | (the working directory) | Folders `context_files` may read, separated by `;` on Windows and `:` elsewhere. If unset, the working directory, unless that is your home folder or a drive root |
| `ADVISOR_SHOW_SUPPORT` | `show_support` | `1` | `0` hides the donation line in the startup banner and transcripts |
| `ADVISOR_TIMEOUT` | `timeout` | `300` | Base seconds before a consult is stopped. Each consult adds about 10 s per 1,000 characters sent and about 0.06 s per answer-budget word, then multiplies the total by 1.5, 2 or 2.5 at effort `high`, `xhigh` or `max` |
| `ADVISOR_TIMEOUT_MAX` | `timeout_max` | `3600` | Upper limit on that calculated timeout |
| `ADVISOR_TIMEOUT_SCALE` | `timeout_scale` | `1` | Multiplier on the size-based extra time. `0` gives a flat `ADVISOR_TIMEOUT` |
| `ADVISOR_IDLE_TIMEOUT` | `idle_timeout` | `300` | Seconds an advisor may go without streaming anything before the consult is stopped. A working consult streams every few seconds, even while thinking, so this catches a hang long before `ADVISOR_TIMEOUT_MAX`. `0` turns it off |
| `ADVISOR_PROGRESS_INTERVAL` | `progress_interval` | `15` | Seconds between keep-alive progress notifications during a consult, which stop the client's own request timeout from firing |
| `ADVISOR_LOCK` | `lock` | `0` | `1` pins model, effort and token settings |
| `ADVISOR_TIERS_JSON` | `tiers` | none | Remap or add tiers, e.g. `{"deep":"claude-fable-5-1"}` |
| `ADVISOR_SYSTEM_PROMPT` | `system_prompt` | none | Replace the advisor's built-in persona |
| `ADVISOR_SYSTEM_PROMPT_FILE` | `system_prompt_file` | none | The same, read from a file |
| `ADVISOR_SYSTEM_PROMPT_EXTRA` | `system_prompt_extra` | none | Append your own house rules to the built-in persona |
| `ADVISOR_MAX_BUDGET_USD` | `max_budget_usd` | none | Hard spending cap per consult (Claude Code backend) |
| `ADVISOR_MAX_CONTEXT_CHARS` | `max_context_chars` | `60000` | `context` longer than this is truncated |
| `ADVISOR_NSFW_SCRUB` | `nsfw_scrub` | `0` | `1` replaces profanity in outgoing text with mild substitutes |
| `ADVISOR_CLAUDE_BIN` | `claude_bin` | (found on PATH) | Absolute path to `claude` |
| `ADVISOR_TRANSPORT` | `transport` | `stdio` | `stdio` or `http` |
| `ADVISOR_HOST` / `ADVISOR_PORT` | `host` / `port` | `127.0.0.1` / `8484` | Where the HTTP transport listens. Anything beyond loopback needs `ADVISOR_HTTP_TOKEN` |
| `ADVISOR_HTTP_TOKEN` | `http_token` | none | Bearer token every HTTP request must carry (`Authorization: Bearer <token>`) |
| `ADVISOR_HTTP_NO_AUTH` | `http_no_auth` | `0` | `1` allows listening beyond loopback without a token, for when a proxy or firewall already guards the port |
| `ADVISOR_ADVISORS_JSON` | `advisors` | none | Advisors besides Claude, as a JSON object of `name → {provider, …}` (see [Other advisors](#other-advisors)) |
| `ADVISOR_DEFAULT_ADVISOR` | `default_advisor` | `claude` | The advisor a consult goes to when it doesn't name one |
| `ADVISOR_ADVISORS_FILE` | `advisors_file` | `~/.wisdomtooth/advisors.json` | Where `advisor_connect` saves advisors and their keys (readable only by you) |
| `ADVISOR_ACCOUNTS_FILE` | `accounts_file` | `~/.wisdomtooth/accounts.json` | The latest meter and balance reading for each account |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` | none | none | The keys the `openai`, `gemini` and `openrouter` presets read, unless an advisor sets its own `api_key` or `api_key_env` |
| `ANTHROPIC_API_KEY` | none | none | Used only by the API backend |
| `CLAUDE_CODE_OAUTH_TOKEN` | none | none | Claude Code's own headless sign-in, if you use one. Passed through to it unchanged |

An example `~/.wisdomtooth/config.json`:

```json
{
  "model": "balanced",
  "effort": "high",
  "max_tokens": 24000,
  "system_prompt_extra": "This team writes Rust. Prefer std over crates."
}
```

## Choosing a model and effort

Every consult tool takes optional `model`, `effort` and `max_tokens`
arguments, so **the calling agent decides how much firepower a question
needs**. `advisor_models` lists what's currently available.

| Tier | Model | Use it for |
|---|---|---|
| `fast` | `claude-haiku-4-5` | Quick sanity checks and factual confirmations |
| `balanced` | `claude-sonnet-5` | The default. Everyday "stuck on the implementation" questions |
| `deep` | `claude-opus-5` | Architecture, subtle behavior across systems, and bugs that survived earlier attempts |

Effort scales the same way: `medium` for ordinary advice, `high` or `xhigh`
for hard problems, and `max` only when an `xhigh` answer wasn't enough.

You don't have to track what each model supports; the server handles it.
Effort is sent only to models that accept it (Haiku doesn't, so it's skipped)
and is clamped to the levels each model supports. Models with adaptive
thinking get `thinking: {"type": "adaptive"}`, since all of them reject
`budget_tokens` with a 400 error. On Opus 5 and Fable, the server also
requests the server-side refusal `fallbacks` option, so a consult the model
declines is rescued instead of coming back empty. Anything the
server doesn't recognize falls back to a plain request rather than an error.

## Development

```bash
uv venv && uv pip install -e . pytest pytest-asyncio anyio
.venv/Scripts/python -m pytest        # use bin/ instead of Scripts/ outside Windows
```

The tests need no credentials and spend nothing. They cover:

- request shaping against the Messages API;
- the CLI subprocess contract (arguments, stdin, environment, failure modes),
  against a fake `claude` binary;
- backend and billing selection, and credential handling;
- the OpenAI-compatible client, against a local fake endpoint;
- a real MCP stdio session.

CI runs them on Linux, Windows and macOS against both mcp 1.x and 2.x.

`docs/INTERNALS.md` explains the less obvious decisions and the bugs they
prevent; read it before changing how the CLI is run. `CHANGELOG.md` lists the
changes in each release.

## Design notes

- **Stateless.** Each call stands alone. The required arguments force the
  caller to pass the full context, which also discourages overuse.
- **The advisor knows who it's talking to.** Its system prompt says the caller
  is a stuck agent, so it questions the caller's framing and assumptions
  instead of just answering the literal question.
- **Consults can't touch your machine.** Claude Code consults run with
  `--tools ""`, `--strict-mcp-config`, `--no-session-persistence` and
  `--setting-sources ""` in a dedicated empty folder, so a consult can't touch
  your files, pick up your `CLAUDE.md`, start your other MCP servers, or call
  back into this advisor.
- **Prompts go on stdin.** The prompt travels on stdin and the system prompt
  via `--system-prompt-file`, because Windows truncates a long or multi-line
  argument when the CLI is an npm `.cmd` shim.
- **No declared output schema.** The consult tools return content blocks
  without one. Annotating the return type makes mcp 1.x derive a schema and
  send every block a second time as structured JSON, while mcp 2.x suppresses
  it; leaving the annotation off behaves the same on both.
- **Want the advisor to have its own tools** (web search, file access)? Wrap
  Claude Code instead, with `claude mcp serve`.

## Adding a provider

A provider with an OpenAI-compatible chat-completions endpoint needs no code:
it's an `openai-compatible` advisor with a `base_url` and a `model`. To give a
common one its own preset, add an entry to `PROVIDERS` in
`wisdomtooth/advisors.py` with its endpoint, key variable, tiers, the effort
levels it accepts, which max-tokens parameter it expects, and whether it
reports a balance.

Any coding-agent CLI can be used without code too, as a `cli` advisor with a
`command` and `args`. A preset for one goes in `PRESETS` in
`wisdomtooth/cli_advisors.py`: its binary, the arguments that run it
read-only, and a reader for its output format.

A provider that needs a different protocol altogether is a `Backend` in
`wisdomtooth/server.py`: a name, the account it bills, a readiness check, and
one `consult(system, user_content, model, effort, max_tokens) -> str`
function, registered with `register_backend(...)`. It reports tokens through
`_note_usage(...)`, so the ledger, caps and repeat guard cover it too.

## Support the project

Wisdomtooth is free and open source under the Apache-2.0 license (see
`LICENSE`). There are no donation links yet. When there are, they'll appear
here and behind the repository's Sponsor button. They only ever show up where
a person reads (this README, the startup banner and saved transcripts), never
in anything sent to your agent, and `ADVISOR_SHOW_SUPPORT=0` hides them.
