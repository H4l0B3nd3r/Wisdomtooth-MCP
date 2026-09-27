# Using Wisdomtooth

A practical guide: what this is for, how to set it up, how to tune it, and what
to do when it misbehaves. For the full reference see `README.md`.

---

## What it is

Your local coding agent — a Qwen, Gemma, or similar model in Kilo Code, Cursor,
or Cline — gets stuck. Instead of guessing from training data that may be a year
old, it asks a frontier model one focused question and gets an expert answer
back. That model is Claude by default. You can connect others alongside it
(ChatGPT, Gemini, OpenRouter, or a local model) and let the agent ask one of
them, or two or three at once. See "Other advisors" below.

**It spends your Claude subscription, not API credits.** Consults run through
your logged-in Claude Code CLI, so a Pro or Max plan covers them at no
per-token cost.

It is deliberately an **escalation path**, not a chat window. A local model that
called Claude for every question would be slow, would burn your plan's headless
quota, and would stop thinking for itself. Three mechanisms hold that line:

- Every tool description opens with explicit WHEN TO USE / DO NOT USE criteria.
- `ask_wisdomtooth` cannot be called without an `attempts_so_far` argument
  saying what was already tried and what the docs returned.
- `.kilocode/rules/wisdomtooth.md` states the policy as a standing rule.

---

## Setup in three steps

### 1. Install

```bash
uv tool install git+https://github.com/H4l0B3nd3r/Wisdomtooth-MCP
```

You also need the Claude Code CLI on PATH — that is what talks to your
subscription. Get it from <https://claude.com/claude-code>.

### 2. Connect your Claude account

Ask your agent to call **`advisor_login`**. A console window opens running the
official Claude sign-in; finish it in your browser and the advisor switches to
subscription billing immediately — no config file, no restart.

If the machine has no desktop (a container, a remote box, SSH), run this in a
terminal instead:

```bash
claude setup-token
```

…and give the printed token to **`advisor_set_token`**. It is saved to
`~/.wisdomtooth/credentials.json` with owner-only permissions and injected
into every consult, so it survives restarts and the reduced environment a
GUI-launched editor hands to its MCP servers.

### 3. Add it to your client

**Kilo Code** — merge into `kilo.jsonc` under `mcp`:

```jsonc
{
  "mcp": {
    "wisdomtooth": {
      "type": "local",
      "command": ["wisdomtooth-mcp"],
      "environment": {
        "ADVISOR_BACKEND": "claude-code",
        "ADVISOR_PRESET": "small"
      },
      "enabled": true,
      "timeout": 300000
    }
  }
}
```

**Claude Code** — `claude mcp add wisdomtooth -- wisdomtooth-mcp`

**Cursor / Windsurf / Cline / Claude Desktop** — standard `mcpServers` JSON with
command `wisdomtooth-mcp`.

Then copy `.kilocode/rules/wisdomtooth.md` into your project's
`.kilocode/rules/` so the agent gets the escalation policy as a standing rule.

Verify with **`advisor_status`**. You want `active backend: claude-code` and
`billing: Claude SUBSCRIPTION`.

---

## Settings that matter for local models

`ADVISOR_PRESET` in the config above sets the length and tool-surface levers
together, sized by your model's context window:

| Preset | For | Answer ceiling | Tool surface |
|---|---|---|---|
| `small` | up to ~32k context (7B–30B local models) | 600 words | minimal |
| `medium` (default) | ~32k–200k (large local and most hosted models) | 2,000 words | full |
| `large` | 200k+ (Claude Code, Codex and similar) | 64,000 words | full |

Anything you set explicitly still wins. The sections below explain each lever.

### `ADVISOR_MINIMAL_TOOLS=1` — shrink the tool surface

Every tool schema is charged against your local model's context **on every
turn**, not just when it escalates. Measured on this server:

| Mode | Tools | Per-turn cost | Of an 8k window |
|---|---|---|---|
| Full | 14 | ~6,200 tokens | 76% |
| Minimal | 2 | ~1,300 tokens | 16% |

Minimal mode exposes only `ask_wisdomtooth` and `advisor_status`. The operator
tools still work — they are hidden from the model, not removed — you just turn
the flag off for a session when you need them. A shorter tool list also
measurably improves tool-selection accuracy in small models.

**Recommended for any model under ~30B, or any context window under 32k.**

### `ADVISOR_ANSWER_BUDGET` — cap the reply length

Claude's answer lands **inside your local model's context**. An unbounded Opus
answer can be larger than an 8B model's entire window. This sets a ceiling in
words (from the preset: 600 / 2,000 / 64,000) and is enforced through the
system prompt, which is the only lever that works on the subscription backend
— the Claude Code CLI has no `max_tokens` flag. Claude is told it is a
ceiling, not a target, and to size each answer to the question.

- `300` — tight contexts, quick factual escalations
- `600` — the `small` preset; ordinary advice on a small local model
- `2000` — the `medium` default; room for a real design answer
- `64000` — the `large` preset; frontier callers with large windows
- `0` — no limit

`max_tokens` also exists but is **API-backend only**; on the subscription
backend the footer will tell you it was ignored.

### `ADVISOR_MODEL` — which Claude answers

Default is `balanced` (Sonnet). A local model escalates often, and putting every
one of those on Opus exhausts a Pro plan's headless quota quickly.

| Tier | Model | Use for |
|---|---|---|
| `fast` | Haiku 4.5 | Quick factual confirmations |
| `balanced` | Sonnet 5 | Default — ordinary stuck-on-implementation |
| `deep` | Opus 5 | Architecture, subtle bugs, second escalations |

The agent can override per call, so `deep` is always one argument away.

### `ADVISOR_EFFORT` — how hard Claude thinks

`low` / `medium` / `high` / `xhigh` / `max`. Leave unset for the API default;
raise it for genuinely hard problems. Ignored on Haiku, which has no effort
setting. Higher effort means slower answers, not just better ones.

---

## Day-to-day use

### Changing settings without a restart

Editing your MCP client's config requires restarting the server entry. This does
not:

```
advisor_configure(model="deep", effort="high")
advisor_configure(answer_budget=300)
advisor_configure(reset=true)
```

Use it when you say things like "use Opus for this one" or "answers are too
long". Per-call arguments still win over it.

### Which Claude models are available

`advisor_models` lists the tiers, which models accept an `effort` setting, and
the current token ceiling. Free — no model call.

### Reading the footer

Every answer ends with a line like:

```
[advisor: claude-code/sonnet · billed to SUBSCRIPTION · effort=high]
[saved: C:\Users\you\.wisdomtooth\consults\20260209-142233-ask-wisdomtooth-why-does-the-datagrid-flicker.md — ...]
```

The first line is your audit trail. If it says `billed to API ACCOUNT`, you are
spending per-token credits — check `advisor_status`.

### Reading the answer yourself

The second line is there because the answer arrives inside your *agent's* chat,
not yours. Kilo and Cline do render an MCP tool result, but collapsed and easy
to miss; a small local model will often paraphrase it into two sentences and
move on; and once the conversation is trimmed the full text is gone. Digging it
back out of your inference server's logs is not a workflow.

So each consult is also written to a Markdown file holding what was sent (after
secret redaction), what came back, and who paid:

```
~/.wisdomtooth/consults/
```

Open the newest file to read the whole answer. Clients that render a
`resource_link` — the second content block every consult returns — show it as
something you can click instead.

| Want to | Do |
|---|---|
| Find the directory | `advisor_status` prints it |
| Move it | `ADVISOR_CONSULT_DIR=/some/path` |
| Keep more or fewer | `ADVISOR_CONSULT_KEEP=1000` (`0` = keep all) |
| Turn it off | `ADVISOR_SAVE_CONSULTS=0` |

If your agent buries the answer, ask it for the `[saved: ...]` path — the tool
result tells it to hand that over.

### Watching usage

The footer's `[usage: ...]` line shows what each consult used. For totals, call
`advisor_usage`: consults, tokens and API-rate cost for the last hour, 5 hours,
24 hours and 7 days, plus a per-model breakdown. The raw records are in
`~/.wisdomtooth/usage.jsonl`.

To stop an over-eager agent before it drains your plan, set a cap in the
server's `environment`:

```jsonc
"ADVISOR_MAX_CONSULTS_PER_5H": "10",
"ADVISOR_MAX_CONSULTS_PER_WEEK": "60"
```

A capped consult is refused before anything is sent. Word-for-word repeats
within 30 minutes are answered from the saved copy and never count.

### Letting the server read files

Your agent can pass `context_files: ["src/app.py", "src/db.py"]` instead of
pasting code, which keeps those characters out of its own window. The server
reads only inside `ADVISOR_FILE_ROOTS` (or its working directory, if that is a
project folder rather than your home folder) and refuses `.env`, key files,
`.ssh/` and `.git/`. If `advisor_status` shows no file roots, set
`ADVISOR_FILE_ROOTS` to the project path.

### Follow-up questions

Pass the file name from a previous `[saved: ...]` line as `follow_up_of`, and
the advisor sees its earlier question and answer again — useful when the first
answer raised a question of its own.

### Other advisors

Claude stays the default. To add another advisor, ask your agent in plain
words, for example "connect my Gemini key AIza…" or "add my LM Studio model
as an advisor". It calls `advisor_connect`, which checks the key and the
endpoint and saves the advisor. The advisor works at once, with no restart.

| Provider | What you need |
|---|---|
| `openai` (ChatGPT) | an OpenAI API key (platform.openai.com). A ChatGPT Plus subscription is not an API key |
| `gemini` | a Gemini API key from Google AI Studio. The Gemini CLI's free Google-account login no longer works for third-party clients |
| `openrouter` | an OpenRouter key; its credit balance is shown in `advisor_status` |
| `lmstudio` / `ollama` | the local server running with a model loaded. Check the port: LM Studio can run on a port other than 1234 |
| `codex`, `antigravity`, `kilo`, `opencode`, `qwen`, `copilot`, `gemini-cli` | that CLI installed and signed in by you. The advisor runs your own install in its read-only mode; nothing else to configure |

Your agent can then:

- **ask one advisor**: `advisor="gemini"` on `ask_wisdomtooth`, `review_code`
  or `compare_approaches`;
- **ask two or three at once** with `multi_advisor`, either the same question
  to each, to compare the answers, or a different question to each
  (`targeted_questions`), for example a technical question to Claude, a UI/UX
  question to Gemini and a code review to ChatGPT.

Each advisor bills its own account, and the footer names which one paid.
`advisor_configure(advisor="gemini")` makes another advisor the default for
this session; `default_advisor` in the config file makes it permanent.

### How much is left, and when it asks you first

`advisor_status` shows what each account has left. For your Claude plan, that
is the plan's own 5-hour and 7-day meter, as Claude Code reports it. For
OpenRouter, it is the key's credit. For OpenAI and Gemini, which do not
publish a balance, declare an allowance yourself, for example
`"allowance_tokens": 2000000, "allowance_window": "month"` on that advisor.

If a request would cost more than the account has left, it is **held**:
nothing is sent, and your agent must ask you whether to go ahead. If you say
yes, it sends it again with `confirm_over_limit=true`. A balance that is merely
low does not interrupt you; the answer's footer says `LOW` instead.

---

## When something goes wrong

**Start with `advisor_auth_check`.** It is free, makes no model call, and
diagnoses the whole credential chain: CLI present, version, login state, auth
method, stored token, and whether anything is hijacking billing.

| Symptom | Cause | Fix |
|---|---|---|
| "No usable Claude credentials" | Never connected | `advisor_login` |
| `billing: DEVELOPER API account` unexpectedly | `ANTHROPIC_API_KEY` set, or CLI logged in with `--console` | Unset the key, or `advisor_login(force=true)` |
| AUTH FAILURE mid-session | Logged out or token revoked | `advisor_login` — a human must complete OAuth; retrying never helps |
| "usage limit is exhausted" | Plan's headless quota spent | Wait for reset, or add an API key and use `ADVISOR_BACKEND=auto` |
| Consult times out | Not logged in, or a first-run prompt is blocking | Run `claude` interactively once, then `advisor_auth_check` |
| "Consult cap reached" | A cap you set in `ADVISOR_MAX_CONSULTS_PER_*` | Wait for the time given, or raise the cap |
| "context_files is unavailable" | The server runs from your home folder | Set `ADVISOR_FILE_ROOTS` to the project path |
| The same answer again, marked `[repeat: ...]` | The agent re-asked word for word | Expected — add new context to get a new answer |
| Answers overflow the agent's context | Budget too high | Lower `ADVISOR_ANSWER_BUDGET` |
| Can't find what Claude actually said | The agent summarised it | Open the newest file in `~/.wisdomtooth/consults` (`advisor_status` prints the path) |
| Agent escalates constantly | Rules file not installed | Copy `.kilocode/rules/wisdomtooth.md` into the project |
| Agent never escalates | It has not tried yet | Expected — the policy says escalate *after* its own attempts fail |
| Command not found from a GUI editor | Reduced PATH | Use absolute paths, and set `ADVISOR_CLAUDE_BIN` |

**After changing the installed package**, restart the MCP server entry in your
client — an MCP stdio server is a long-lived process and keeps running the old
code until it does. On Windows, `uv tool install` fails with "Access is denied"
while it is still alive; stop the server entry first.

---

## Cost and safety

- **Subscription consults cost no money**, but they do draw on your plan's
  headless (non-interactive) quota, which is separate from interactive use.
- **`ADVISOR_LOCK=1`** pins model, effort and token budget so the agent cannot
  raise them. Use on shared machines or when handing this to an agent you do not
  yet trust.
- **`ADVISOR_MAX_BUDGET_USD`** caps spend per consult on the CLI backend.
- Consults run with `--tools ""`, `--strict-mcp-config`,
  `--no-session-persistence` and `--setting-sources ""` in a dedicated empty
  directory. The advisor cannot read your files, load your `CLAUDE.md`, start
  your other MCP servers, or recurse into itself.
- Obvious secrets (API keys, tokens, private key blocks, `password=`) are
  redacted from outbound content, and `context` is capped at 60k characters.
  That is a seatbelt, not a guarantee — do not paste `.env` files.
- Keep `advisor_login` off any auto-approve list. It opens a window on your
  desktop, and a confused model should not be able to do that unprompted.

---

## Getting good answers

The advisor is **stateless**. It sees only what the agent sends — no repo, no
history, no previous consult. Answer quality tracks input quality:

- **`question`** — the specific blocker, not "help me".
- **`context`** — the exact error text, relevant code, versions, constraints.
- **`attempts_so_far`** — what was tried, how it failed, what the docs said.
  This is required for a reason: it stops the advisor repeating a failed
  suggestion, and it stops the agent escalating without thinking first.

Because it is told the caller is a stuck agent, the advisor will question the
framing rather than answer the literal question when the premise looks wrong —
often the most valuable thing it does.

Two escalations on the same problem without progress means stop and ask a human.
