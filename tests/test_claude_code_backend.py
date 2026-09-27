"""The subscription backend: shelling out to the local Claude Code CLI.

Every test here runs against the fake CLI from conftest -- no credentials, no
spend -- and asserts on the exact argv, stdin, and environment the server hands
to the child process.
"""

import json
import os

import pytest


def consult(srv, **kw):
    kw.setdefault("question", "why is this broken?")
    return srv._consult(**kw)


# --------------------------------------------------------------------------
# The prompt must travel on stdin
# --------------------------------------------------------------------------

def test_prompt_is_passed_on_stdin_not_argv(server, fake_claude):
    """Windows caps a command line at ~32k chars (~8k through cmd.exe).

    The context cap alone is 60k, so a prompt in argv makes large consults fail
    to even launch. It has to go through stdin.
    """
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv, question="MARKER-QUESTION", context="MARKER-CONTEXT")
    call = fake_claude.last
    assert "MARKER-QUESTION" in call["prompt"]
    assert "MARKER-CONTEXT" in call["prompt"]
    assert not any("MARKER-QUESTION" in arg for arg in call["argv"])


def test_a_very_large_context_still_reaches_the_cli(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_MAX_CONTEXT_CHARS="200000")
    consult(srv, question="q", context="x" * 120_000)
    assert len(fake_claude.last["prompt"]) > 100_000


# --------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------

def test_tools_are_disabled_with_a_flag_not_a_polite_request(server, fake_claude):
    """A system-prompt instruction is advisory; the --tools flag is enforced."""
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv)
    argv = fake_claude.last["argv"]
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == ""


def test_output_format_is_json_when_the_cli_cannot_stream(server, fake_claude,
                                                          tmp_path):
    """JSON carries is_error and the model actually used; text carries neither.
    A CLI that streams gets `stream-json` instead -- see test_streaming.py."""
    script = tmp_path / "fake_claude.py"
    src = script.read_text(encoding="utf-8")
    script.write_text(src.replace("  --include-partial-messages\n", ""),
                      encoding="utf-8")
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv)
    assert fake_claude.flag_value("--output-format") == "json"


def test_answer_is_read_from_the_result_field(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    assert "FAKE ANSWER" in consult(srv)


def test_isolation_flags_are_present(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv)
    argv = fake_claude.last["argv"]
    for flag in ("-p", "--strict-mcp-config", "--no-session-persistence",
                 "--disable-slash-commands"):
        assert flag in argv, "missing " + flag


def test_strict_mcp_config_can_be_disabled(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_NO_STRICT_MCP="1")
    consult(srv)
    assert "--strict-mcp-config" not in fake_claude.last["argv"]


def test_system_prompt_replaces_rather_than_appends(server, fake_claude):
    """Appending keeps ~12k tokens of coding-agent framing that is wrong for an
    advisor and is billed on every call."""
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv)
    argv = fake_claude.last["argv"]
    assert {"--system-prompt", "--system-prompt-file"} & set(argv)
    assert "--append-system-prompt" not in argv
    assert "--append-system-prompt-file" not in argv


def test_runs_in_a_dedicated_workdir(server, fake_claude):
    """Otherwise the consult inherits the caller's CLAUDE.md and project state."""
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv)
    # Compared against `_workdir()` rather than a literal directory name: since
    # 0.8.0 the state directory is `~/.wisdomtooth` on a fresh machine but the
    # legacy `~/.claude-advisor` on one that predates the rename, and a test
    # that pins the name passes or fails depending on whose home it runs in.
    assert fake_claude.last["cwd"] == srv._workdir()
    assert fake_claude.last["cwd"] != os.getcwd()


# --------------------------------------------------------------------------
# Model and effort selection
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tier,alias", [
    ("fast", "haiku"), ("balanced", "sonnet"), ("deep", "opus")])
def test_tier_maps_to_cli_alias(server, fake_claude, tier, alias):
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv, model=tier)
    assert fake_claude.flag_value("--model") == alias


def test_env_default_model_is_honoured(server, fake_claude):
    """Regression: the CLI path used to read the raw per-call argument and
    ignore ADVISOR_MODEL entirely."""
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_MODEL="fast")
    consult(srv)
    assert fake_claude.flag_value("--model") == "haiku"


def test_lock_overrides_the_agents_model_choice(server, fake_claude):
    """Regression: ADVISOR_LOCK was a no-op on this backend."""
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_MODEL="fast",
                 ADVISOR_LOCK="1")
    consult(srv, model="deep")
    assert fake_claude.flag_value("--model") == "haiku"


def test_effort_is_forwarded(server, fake_claude):
    """Regression: effort was documented as unsupported here, but the CLI has
    had an --effort flag for a while."""
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv, effort="xhigh")
    assert fake_claude.flag_value("--effort") == "xhigh"


def test_no_effort_means_no_flag(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv)
    assert "--effort" not in fake_claude.last["argv"]


def test_budget_cap_is_forwarded_when_configured(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_MAX_BUDGET_USD="0.5")
    consult(srv)
    assert fake_claude.flag_value("--max-budget-usd") == "0.5"


# --------------------------------------------------------------------------
# Billing isolation
# --------------------------------------------------------------------------

def test_api_credentials_are_stripped_from_the_child(server, fake_claude):
    """With a key present the CLI bills the Console account, not the plan."""
    srv = server(ADVISOR_BACKEND="claude-code",
                 ANTHROPIC_API_KEY="sk-ant-nope",
                 ANTHROPIC_AUTH_TOKEN="bearer-nope")
    consult(srv)
    env = fake_claude.last["env"]
    assert env["ANTHROPIC_API_KEY"] is None
    assert env["ANTHROPIC_AUTH_TOKEN"] is None


def test_headless_subscription_token_is_preserved(server, fake_claude):
    """CLAUDE_CODE_OAUTH_TOKEN is the credential `claude setup-token` mints."""
    srv = server(ADVISOR_BACKEND="claude-code", CLAUDE_CODE_OAUTH_TOKEN="oauth-abc")
    consult(srv)
    assert fake_claude.last["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-abc"


def test_stripping_can_be_opted_out_of(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code", ANTHROPIC_API_KEY="sk-ant-keep",
                 ADVISOR_KEEP_AUTH_ENV="1")
    consult(srv)
    assert fake_claude.last["env"]["ANTHROPIC_API_KEY"] == "sk-ant-keep"


def test_footer_names_the_subscription_as_the_payer(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    assert "SUBSCRIPTION" in consult(srv)


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------

def test_auth_failure_says_a_human_must_log_in(server, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "auth_fail")
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(RuntimeError) as exc:
        consult(srv)
    text = str(exc.value)
    assert "AUTH" in text
    assert "/login" in text


def test_usage_limit_is_reported_distinctly(server, fake_claude, monkeypatch):
    """Exhausting the plan is not a bug and not retryable -- the agent has to
    tell the user rather than loop or silently switch to paid API credits."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "usage_limit")
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(RuntimeError) as exc:
        consult(srv)
    assert "usage limit" in str(exc.value).lower()


def test_is_error_true_raises_even_on_exit_zero(server, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "is_error")
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(RuntimeError) as exc:
        consult(srv)
    assert "exploded" in str(exc.value)


def test_empty_answer_raises(server, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "empty")
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(RuntimeError):
        consult(srv)


def test_non_json_output_is_still_usable(server, fake_claude, monkeypatch):
    """Tolerate a CLI whose JSON output format is absent or has changed."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "not_json")
    srv = server(ADVISOR_BACKEND="claude-code")
    assert "plain text answer" in consult(srv)


def test_nonzero_exit_reports_stderr(server, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(RuntimeError) as exc:
        consult(srv)
    assert "boom" in str(exc.value)


def test_timeout_errors_out_instead_of_hanging(server, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_TIMEOUT="2",
                 ADVISOR_TIMEOUT_SCALE="0")
    with pytest.raises(RuntimeError) as exc:
        consult(srv)
    assert "2s" in str(exc.value)


# --------------------------------------------------------------------------
# The kill timeout is sized to the consult
# --------------------------------------------------------------------------

def test_timeout_grows_with_input_size(server):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_ANSWER_BUDGET="600")
    small = srv._consult_timeout(2_000, None)
    large = srv._consult_timeout(60_000, None)
    assert small >= srv.TIMEOUT
    assert large >= small + 500


def test_timeout_grows_with_answer_budget_and_effort(server):
    short = server(ADVISOR_ANSWER_BUDGET="600")._consult_timeout(10_000, None)
    srv = server(ADVISOR_ANSWER_BUDGET="10000")
    assert srv._consult_timeout(10_000, None) > short
    assert srv._consult_timeout(10_000, "high") > srv._consult_timeout(10_000, None)


def test_large_planning_consult_outlasts_the_old_flat_limit(server):
    """The regression: a big UI/UX planning consult (deep, effort=high,
    10,000-word budget) was killed at a flat 180s."""
    srv = server(ADVISOR_ANSWER_BUDGET="10000", ADVISOR_MODEL="deep",
                 ADVISOR_EFFORT="high")
    assert srv._consult_timeout(40_000, "high") >= 1200


def test_timeout_is_capped(server):
    srv = server(ADVISOR_TIMEOUT_MAX="900")
    assert srv._consult_timeout(10_000_000, "max") == 900


def test_timeout_scale_zero_restores_a_flat_limit(server):
    srv = server(ADVISOR_TIMEOUT="42", ADVISOR_TIMEOUT_SCALE="0")
    assert srv._consult_timeout(60_000, "max") == 42


def test_consult_hands_the_scaled_timeout_to_the_cli(server, fake_claude,
                                                      monkeypatch):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_ANSWER_BUDGET="600")
    seen = []
    real = srv._run_claude

    def spy(cmd, env=None, workdir=None, timeout_s=60, stdin_text="", **kw):
        seen.append(timeout_s)
        return real(cmd, env, workdir, timeout_s, stdin_text, **kw)

    monkeypatch.setattr(srv, "_run_claude", spy)
    consult(srv, context="lorem ipsum dolor sit amet " * 2000)
    assert seen[-1] >= srv.TIMEOUT + 500


def test_missing_cli_gives_actionable_error(server, no_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(RuntimeError) as exc:
        consult(srv)
    assert "ADVISOR_CLAUDE_BIN" in str(exc.value)


# --------------------------------------------------------------------------
# Feature detection
# --------------------------------------------------------------------------

def test_unsupported_flags_are_dropped(server, fake_claude, tmp_path):
    """The CLI self-updates; a flag can disappear. Probe --help, do not guess."""
    script = tmp_path / "fake_claude.py"
    src = script.read_text(encoding="utf-8")
    script.write_text(src.replace("  --disable-slash-commands\n", ""),
                      encoding="utf-8")
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv)
    assert "--disable-slash-commands" not in fake_claude.last["argv"]
    assert "--tools" in fake_claude.last["argv"]


def test_system_prompt_travels_as_a_file(server, fake_claude):
    """A multi-line argv element is truncated at the first newline when an
    npm-installed CLI is reached through `cmd /c claude.cmd`, which silently
    drops every flag after it."""
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv)
    path = fake_claude.flag_value("--system-prompt-file")
    assert path
    assert "expert technical advisor" in fake_claude.last["system_prompt"]
    assert not os.path.exists(path)  # removed once the CLI exited


def test_flags_after_the_system_prompt_survive(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_MAX_BUDGET_USD="0.25")
    consult(srv, effort="high")
    argv = fake_claude.last["argv"]
    assert argv.index("--effort") > argv.index("--system-prompt-file")
    assert "--setting-sources" in argv


def test_inline_system_prompt_is_used_when_the_file_flag_is_absent(
        server, fake_claude, tmp_path):
    script = tmp_path / "fake_claude.py"
    src = script.read_text(encoding="utf-8")
    script.write_text(
        src.replace("  (context via: --system-prompt[-file], "
                    "--append-system-prompt[-file])\n", ""), encoding="utf-8")
    srv = server(ADVISOR_BACKEND="claude-code")
    consult(srv)
    assert "--system-prompt" in fake_claude.last["argv"]


# --------------------------------------------------------------------------
# Paths and arguments that reach a shell
# --------------------------------------------------------------------------

def test_consults_work_when_paths_contain_spaces(server, fake_claude, tmp_path,
                                                 monkeypatch):
    """A Windows user named "Jane Doe" has spaces in the npm shim's path and in
    the system-prompt file's path. cmd.exe mangles such a command line unless
    it is quoted as a whole."""
    spaced = tmp_path / "dir with space"
    spaced.mkdir()
    launcher = spaced / fake_claude.path.name
    launcher.write_bytes(fake_claude.path.read_bytes())
    if os.name != "nt":
        launcher.chmod(0o755)
    monkeypatch.setenv("ADVISOR_CLAUDE_BIN", str(launcher))
    srv = server(ADVISOR_BACKEND="claude-code")
    monkeypatch.setattr(srv, "_state_dir", lambda: str(spaced / "state"))
    assert "FAKE ANSWER" in consult(srv)
    argv = fake_claude.last["argv"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "dir with space" in fake_claude.flag_value("--system-prompt-file")


@pytest.mark.parametrize("model", ["sonnet&echo INJECTED", "opus|calc",
                                   "haiku>out.txt", "a b", "%PATH%"])
def test_a_model_name_that_could_reach_a_shell_is_refused(server, fake_claude,
                                                          model):
    """On Windows an npm-installed CLI runs through cmd.exe, where `&`, `|`,
    `>` and `%` in an argument are commands, not text."""
    srv = server(ADVISOR_BACKEND="claude-code")
    with pytest.raises(ValueError):
        consult(srv, model=model)
    assert not any(c["prompt"] for c in fake_claude.calls())


@pytest.mark.parametrize("model", ["claude-opus-4-6[1m]", "claude-sonnet-5",
                                   "opus", "us.anthropic.claude-opus-5-v1:0"])
def test_real_model_ids_are_accepted(server, fake_claude, model):
    srv = server(ADVISOR_BACKEND="claude-code")
    assert "FAKE ANSWER" in consult(srv, model=model)


def test_the_config_file_can_name_the_cli(server, fake_claude, tmp_path,
                                          monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"claude_bin": str(fake_claude.path)}),
                      encoding="utf-8")
    monkeypatch.delenv("ADVISOR_CLAUDE_BIN")
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    srv = server(ADVISOR_BACKEND="claude-code", ADVISOR_CONFIG=str(config))
    assert srv._claude_bin() == str(fake_claude.path)
