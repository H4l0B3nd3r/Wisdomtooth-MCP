# GOTCHAS.md — known pain points and how this project handles them

Ordered roughly by how likely they are to bite. "Fixed in code" items are
handled automatically; the rest need awareness or config.

## Fixed in code (0.4.0) — know they exist, don't re-break them

0. **mcp 2.x renamed FastMCP.** `from mcp.server.fastmcp import FastMCP` raises
   `ModuleNotFoundError` on mcp >= 2.0, where the class is
   `mcp.server.mcpserver.MCPServer`. The server imports whichever exists, so it
   runs on both. A `mcp>=1.0` dependency pin means a fresh install pulls 2.x —
   if you ever loosen the pin back, the server stops starting entirely.

0a. **Tool errors must be `ToolError`.** The MCP SDK treats any other exception
   as a server crash and replaces its message with a generic
   "Error executing tool ask_claude" — which would hide every diagnostic this
   server produces, including the login steps that are the entire value of an
   auth failure. All user-facing failures raise `AdvisorError`, which subclasses
   the SDK's `ToolError`. Do not "simplify" it back to `RuntimeError`.

0b. **The prompt goes on stdin, the system prompt in a file.** Windows caps a
   command line at ~32k characters, and ~8k through `cmd.exe` — while the
   context cap alone is 60k. Worse, a **multi-line** argv element is truncated
   at the first newline by an npm `.cmd` shim, which silently drops every flag
   after it. Hence `-p` with the prompt piped to stdin, and
   `--system-prompt-file` rather than an inline prompt.

0c. **Tools are disabled by a flag, not a request.** The advisor runs with
   `--tools ""`. The previous "do not use any tools" system-prompt line was
   advisory and the model could ignore it, costing latency and quota.

0d. **Subscription is the default payer.** `ADVISOR_BACKEND` defaults to `auto`,
   which prefers the logged-in Claude Code CLI. It used to default to `api`,
   which silently billed the Console account per token.

## Fixed earlier (0.3.2) — still true

1. **Event-loop blocking.** Tool handlers are now `async` and run consults in
   a worker thread. Before, a 60s Opus consult made the server deaf to
   protocol traffic mid-call, which some clients treat as a dead server.
2. **Windows encoding.** Subprocess I/O is forced to UTF-8 with
   `errors="replace"`. Default Windows cp1252 could crash or garble any
   answer containing non-ASCII (arrows, box-drawing, emoji — common in
   Claude output).
3. **Global MCP recursion / slow consults.** The spawned `claude` runs with
   `--strict-mcp-config`, so it ignores user-scope MCP servers. Without
   this, EVERY consult boots ALL your global MCP servers (multi-second
   startup tax), and if you ever register this advisor at user scope, a
   consult would recursively spawn the advisor. Old CLIs without the flag
   are auto-detected and retried without it; set `ADVISOR_NO_STRICT_MCP=1`
   only if you deliberately want the advisor's Claude to have MCP tools.
4. **Secret leakage.** Obvious credentials in `context` (Anthropic/OpenAI/
   GitHub/AWS/Slack/Google keys, private key blocks, `password=...`) are
   regex-redacted before leaving the machine. This is a seatbelt, not a
   guarantee — the agent must still avoid pasting `.env` files, and users on
   the API backend should remember prompts land on the API account's data
   controls.
5. **Runaway context cost.** `context` is capped (default 60k chars,
   `ADVISOR_MAX_CONTEXT_CHARS`) with head+tail retention and a visible
   truncation marker. Whole-repo pastes get expensive fast on the API
   backend and drain the headless pool on subscription.
6. **NSFW content pass-through.** All outbound content (question + context)
   is scrubbed of NSFW words with case-preserving SFW replacements,
   word-boundary only — so `class`, `assert`, `cocktail`, `shell` are never
   mangled, but standalone profanity in logs/commit messages/history is
   replaced. Extend the wordlist with `ADVISOR_NSFW_EXTRA_JSON` (path to a
   `{"word":"replacement"}` file); disable with `ADVISOR_NSFW_SCRUB=0`.
   The agent is also ruled to scrub at the source before calling. Caveat:
   if the advisor's answer quotes your (scrubbed) input, a find-and-replace
   suggestion may reference the SFW substitute — sanity-check string-literal
   edits it proposes against the real file.
7. **Answers eaten by thinking.** On adaptive-thinking models, `max_tokens`
   covers thinking AND answer. At effort high/xhigh/max the server auto-raises
   the ceiling (24k/32k/48k). If you see the "[advisor returned no visible
   text...]" message, that guard fired — raise `ADVISOR_MAX_TOKENS` (or pass a
   larger per-call `max_tokens`) or lower effort.

## Auth & login (subscription backend)

- **`advisor_login` is the intended path.** It opens `claude auth login
  --claudeai` in a console window on the user's desktop, polls until the
  browser flow completes, and invalidates the cached backend so the next
  consult uses the subscription. No config edit, no server restart.
- **OAuth still cannot happen headlessly.** The server drives the flow; it
  cannot complete it. A machine with no desktop (container, plain SSH, remote
  transport) gets copy-pasteable instructions instead of a window — relay them
  to the user rather than retrying.
- **`claude setup-token` + `advisor_set_token` is the durable answer** for
  servers spawned by GUI editors. The token is stored in
  `~/.claude-advisor/credentials.json` with owner-only permissions and injected
  into every CLI subprocess, which avoids the whole class of "works in my
  terminal, fails from VS Code" credential issues — and, unlike the old
  `CLAUDE_CODE_OAUTH_TOKEN`-in-the-MCP-config approach, keeps the secret out of
  a file people commit.
- **`loggedIn: true` is not the same as "on the subscription".** A CLI signed
  in with `--console` or an API key reports `loggedIn: true` and bills the
  Console account per token. `advisor_auth_check` flags any `authMethod` that
  is not `claude.ai`; `advisor_login(force=true)` switches it.
- **The stored token beats a logged-out CLI, not a logged-in one.** If both
  exist, the CLI uses the token that is injected into its environment. To go
  back to the interactive login, call `advisor_logout` first.
- **Token lifecycle:** interactive OAuth tokens auto-refresh silently;
  re-login is only needed after `claude logout`, server-side revocation, or
  a deleted credential store. If auth breaks "out of nowhere", suspect one
  of those three.
- **`advisor_auth_check` now asks the CLI.** It runs `claude auth status
  --json` and reports `loggedIn`, `authMethod` and `subscriptionType` rather
  than sniffing for a credentials file — so the old "absent on macOS because
  credentials live in the Keychain" false alarm is gone.
- **`authMethod` matters, not just `loggedIn`.** A CLI logged in with an API
  key reports `loggedIn: true` but bills the Console account. `advisor_auth_check`
  flags anything that is not `claude.ai`.
- **Both `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are stripped** from
  claude subprocesses; `CLAUDE_CODE_OAUTH_TOKEN` passes through.

## Configuration & environment traps

8. **Timeout alignment.** Server-side `ADVISOR_TIMEOUT` (default 180s) must
   be LOWER than the Kilo per-server `timeout` (our configs use 300000ms).
   If you inverted them, the client would kill the call before the server
   can return its diagnostic error — you'd be back to mystery hangs.
9. **PATH in GUI-launched editors (Windows/macOS).** VS Code started from
   the dock/Start menu often lacks the shell PATH where pipx/uv installed
   `claude-advisor-mcp` (and `claude`). Symptom: "command not found" or
   instant server-failed status. Fix: use the ABSOLUTE path to the
   executable in the Kilo config, and set `ADVISOR_CLAUDE_BIN` to the full
   path of `claude` for the subscription backend.
10. **Config changes need a server restart — except via `advisor_configure`.**
   Kilo keeps the stdio process alive per session, so editing env vars in the
   config does nothing until the server entry is toggled/restarted. For model,
   effort and token budget, call `advisor_configure` instead — it takes effect
   immediately and lasts until the process exits. `advisor_status` reports the
   LIVE values; trust it over the config file when they disagree.

10a. **Precedence, when settings disagree.** per-call argument > runtime
   `advisor_configure` > environment variable > `~/.claude-advisor/config.json`
   > built-in default. `ADVISOR_LOCK=1` inverts the top two away: per-call and
   runtime changes are rejected and the configured defaults always win.
11. **HTTP transport has NO auth.** `ADVISOR_TRANSPORT=http` on
    `0.0.0.0` lets anyone on the network spend your tokens/subscription.
    Keep it on `127.0.0.1` (default) or put it behind a reverse proxy with
    auth. Never port-forward it.
11a. **`uv tool install --force` can install a STALE build.** uv caches the
    built wheel keyed on the project version, so re-installing after a source
    edit that did not bump `version` in `pyproject.toml` silently reinstalls
    the old code — `advisor_status` still reports the expected version, because
    the version is exactly what did not change. Bump the version for every
    change you intend to install, or force a real rebuild with
    `uv tool install --force --reinstall --no-cache .`. Verify by grepping the
    installed copy under
    `<uv tool dir>/claude-advisor-mcp/Lib/site-packages/claude_advisor/server.py`
    for something the change introduced, not by trusting the version string.

11b. **A running server locks its own install directory (Windows).**
    `uv tool install` fails with "Access is denied ... Scripts" while an MCP
    client still has the stdio server alive. Stop/toggle the server entry in
    the client (or kill the `claude-advisor-mcp` processes) before installing.

12. **Claude Code auto-updates.** The CLI updates itself; flags and headless
    behavior can drift. If the subscription backend suddenly errors after
    working, check `claude --version` changed, run one interactive `claude`
    session (updates sometimes re-prompt onboarding), and re-run the smoke
    test before deeper debugging.

## Local models as the caller (the usual case)

15a. **Tool schemas cost context on EVERY turn.** All ten tools are ~3,340
    tokens of the caller's window whether or not it escalates — 41% of an 8k
    context. `ADVISOR_MINIMAL_TOOLS=1` cuts that to ~1,090. A longer tool list
    also measurably degrades tool-selection accuracy in small models. Set it
    for anything under ~30B or under a 32k window.

15b. **The answer lands in the caller's context.** An unbounded Opus reply can
    be bigger than a small model's whole window. `ADVISOR_ANSWER_BUDGET`
    (default 600 words) is the control, and it works on BOTH backends because
    it goes through the system prompt. `ADVISOR_MAX_TOKENS` does NOT help on
    the subscription backend — the CLI has no such flag, and the answer footer
    says so when you pass one.

15c. **"The model never escalates" is usually correct behaviour.** The policy
    says escalate after its own attempts fail, so a model that reasons first on
    turn one is following it. Suspect a real problem only if it never escalates
    after repeated failures — then check that
    `.kilocode/rules/claude-advisor.md` is actually installed in the project.

15d. **A thinking model needs output headroom to call a tool at all.** If your
    client caps completion tokens too low, a reasoning model burns the whole
    budget inside `<think>` and emits no tool call — which looks exactly like
    "it refuses to use the advisor". Check `finish_reason`: `length` means the
    cap, not the policy. This bit the author's own test harness at 1500 tokens.

## Billing & limits (beyond the README's billing section)

13. **Headless pool exhaustion looks like an outage.** Subscription headless
    calls draw from the plan's separate non-interactive pool. When it is
    exhausted, calls fail until reset. In `ADVISOR_BACKEND=auto` **only**, the
    server retries once on API credits if a key is present, and says so
    loudly in the answer footer — set `ADVISOR_FALLBACK_TO_API=0` to disable.
    With an explicit `ADVISOR_BACKEND=claude-code` it never falls back. Either
    way the agent must surface limit errors to the user and must never flip
    `ADVISOR_BACKEND` itself; that is a money decision.
14. **429/529 from the API backend.** Overloaded/rate-limit errors are
    transient. Policy: retry at most once, then report. No retry loops —
    each retry costs money.
15. **`deep` + `max` latency.** An Opus consult at max effort can
    legitimately take 1-3 minutes. That's expected, not a hang (hangs now
    error out with diagnostics). Don't cancel it and re-issue — you pay for
    the cancelled one too on the API backend.

## Testing the server by hand (don't create false negatives)

18. **Closing stdin = telling the server to shut down.** stdin EOF is the
    MCP stdio shutdown signal. A shell test that writes all messages and
    closes the pipe (PowerShell `StandardInput.Close()`, `echo ... |`,
    heredocs) makes the server exit cleanly — possibly before an in-flight
    consult returns its response. That is correct protocol behavior, not a
    bug. Real clients (Kilo) keep stdin open for the whole session.
    Correct manual test: keep the write handle open, send messages one at a
    time, READ the id:N response before sending the next request, and only
    close stdin when done. If a tool "works in Kilo but not in my pipe
    test", suspect the test first.
19. **PowerShell CLIXML noise.** Remote/elevated PowerShell wraps child
    output in CLIXML envelopes that truncate/garble JSON-RPC lines. Test
    from a plain local console, or better: test through Kilo itself, which
    is the only transport that matters.

## Agent behavior traps (also added to .kilocode/rules)

16. **Advice is advice, not instructions.** The advisor's output is model
    text. The agent should evaluate suggested commands/code before running
    them — especially anything destructive (rm, force-push, DROP, chmod) —
    and never execute shell commands from advice verbatim without checking
    them against the actual task.
17. **No escalation ping-pong.** If advice fails twice on the same problem,
    stop consulting and ask the user. Endless agent↔advisor loops burn the
    budget with no human in the loop.
18. **Stateless means stateless.** Follow-up questions must re-include
    context; "as you suggested earlier" means nothing to the advisor.
