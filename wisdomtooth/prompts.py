"""The advisor's system prompt and the fixed texts agents and users read."""

BUILTIN_SYSTEM_PROMPT = """\
You are an expert technical advisor being consulted by another AI agent.
The agent is stuck: it has already attempted the task itself and consulted
documentation tools without success. Treat this as an escalation, not a
first question.

Guidelines:
- Be direct and concise. Lead with your recommendation, then justify it.
- Because the agent is stuck, question its framing: the most valuable advice
  is often identifying a wrong assumption in what it has already tried.
- If the question is ambiguous, state your assumptions explicitly rather
  than asking clarifying questions (the caller cannot easily reply).
- Flag risks, edge cases, or better alternatives the agent may have missed.
- If you are uncertain, say so and explain what would resolve the uncertainty.
- Never fabricate APIs, flags, or library behavior. If unsure, say "verify this".
- You have no tools and cannot read the caller's files. Answer from the context
  you were given plus your own knowledge; if something decisive is missing, name
  it and say what you would conclude either way.
"""

WHEN_TO_USE = """\
WHEN TO USE THIS SERVER -- it is an ESCALATION path, not a first resort:
USE when ALL of the following are true:
  1. You are having genuine difficulty -- implementing code, understanding a
     framework, platform, operating system, build system, or protocol.
  2. You have already made at least 1-2 serious attempts yourself.
  3. Documentation tools (e.g. Context7, official docs, web search) have not
     helped significantly, OR the problem is judgment-based (architecture,
     tradeoffs, debugging strategy) where docs don't apply.
DO NOT USE for: questions you can answer yourself, simple syntax lookups,
things Context7/docs would answer directly, or trivial decisions.
ALWAYS include in `context`: what you tried, exact errors, and what the docs
said -- the advisor is stateless and sees nothing else.
SEVERAL ADVISORS: Claude is the default. If the user connected others,
advisor_status lists them: pass advisor=<name> to ask one, or use
multi_advisor to ask 2-3 at once -- the same question to compare answers, or
a targeted question to each, matched to each model's strengths.
A consult HELD because it would exceed an account's remaining allowance must
go to the user for a decision; never set confirm_over_limit on your own.
"""


def answer_budget_instruction(budget: int) -> str:
    """The only length control that works on the Claude Code backend.

    `max_tokens` is an API-only parameter; the Claude Code CLI has no
    equivalent flag, so on that backend the system prompt is the sole lever.
    """
    if budget <= 0:
        return ""
    return (
        f"\nLength: keep the answer under roughly {budget} words -- a ceiling, "
        "not a target. Size the answer to what the question actually needs; "
        "most consults need a small fraction of the ceiling, and every word is "
        "billed against the user's usage limits and inserted into the context "
        "window of the agent that asked. Lead with the recommendation, keep "
        "code to the minimum that makes it concrete, and drop restatement of "
        "the question. Go long only when the problem genuinely demands it, such "
        "as a full design or migration plan. If even the ceiling is not enough, "
        "give the decisive part and say what you left out.\n")


def trim_note(kept_words: int, total_words: int, budget: int) -> str:
    return (f"\n\n[trimmed: this is the first ~{kept_words:,} of "
            f"{total_words:,} words -- the answer ran far past the "
            f"{budget:,}-word budget, so only its lead is shown here to save "
            "your context. The full answer is in the file on the [saved: ...] "
            "line below; read it there if you need the rest. Do not ask the "
            "same question again.]")


AUTH_HELP = (
    "AUTH FAILURE from the claude CLI -- this cannot be fixed by retrying; a "
    "person has to sign in. Tell the user to do ONE of these on this machine: "
    "(a) open a terminal, run `claude`, and sign in; or (b) set "
    "ANTHROPIC_API_KEY in the MCP server env to use the Anthropic API "
    "instead. Then re-run advisor_auth_check. CLI said: "
)

NO_CREDENTIALS = (
    "No usable Claude credentials on this machine, so the advisor cannot run.\n"
    "Either: set ANTHROPIC_API_KEY in the MCP server env (a key from "
    "console.anthropic.com; billed per token) and restart the server entry; "
    "or, if Claude Code is installed, sign in to it by running `claude` in a "
    "terminal -- Wisdomtooth then uses that install.\n"
    "A person has to do one of these -- do not retry the consult until they "
    "have."
)
