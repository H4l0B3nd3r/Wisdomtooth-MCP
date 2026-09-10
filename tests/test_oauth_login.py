"""Connecting a Claude subscription account over OAuth.

The server cannot perform OAuth itself -- it needs a human at a browser -- but
it can drive the official CLI flow, wait for it to finish, and then hold onto
the resulting credential so the user never has to hand-edit their MCP client's
JSON or restart the server entry.
"""

import json
import os

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the credential store to a temporary HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def creds_path(home):
    return home / ".wisdomtooth" / "credentials.json"


def write_creds(home, **data):
    path = creds_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The credential store
# --------------------------------------------------------------------------

def test_a_stored_token_is_loaded_at_startup(server, home, fake_claude):
    """The whole point: connect once, and later server starts stay connected."""
    write_creds(home, claude_code_oauth_token="oat-stored-123")
    srv = server(ADVISOR_BACKEND="claude-code")
    assert srv._oauth_token() == "oat-stored-123"


def test_a_stored_token_reaches_the_cli_subprocess(server, home, fake_claude):
    """A GUI-launched client has a reduced environment; the token has to be
    injected rather than inherited."""
    write_creds(home, claude_code_oauth_token="oat-stored-123")
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._consult(question="q")
    assert fake_claude.last["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-stored-123"


def test_an_env_token_wins_over_the_stored_one(server, home, fake_claude):
    write_creds(home, claude_code_oauth_token="oat-stored")
    srv = server(ADVISOR_BACKEND="claude-code",
                 CLAUDE_CODE_OAUTH_TOKEN="oat-from-env")
    assert srv._oauth_token() == "oat-from-env"


def test_no_store_is_not_an_error(server, home, fake_claude):
    assert server()._oauth_token() is None


def test_a_malformed_store_does_not_kill_the_server(server, home, fake_claude):
    path = creds_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ broken", encoding="utf-8")
    assert server()._oauth_token() is None


def test_saving_a_token_persists_it(server, home, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._set_oauth_token("oat-brand-new")
    stored = json.loads(creds_path(home).read_text(encoding="utf-8"))
    assert stored["claude_code_oauth_token"] == "oat-brand-new"


def test_saving_a_token_applies_without_a_restart(server, home, fake_claude):
    """Editing the MCP client config needs a restart; this must not."""
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._set_oauth_token("oat-brand-new")
    srv._consult(question="q")
    assert fake_claude.last["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-brand-new"


def test_saving_a_token_switches_auto_onto_the_subscription(
        server, home, fake_claude, monkeypatch):
    """Before connecting there may be no usable backend at all; afterwards the
    very next consult must go to the subscription, with no restart."""
    monkeypatch.setenv("FAKE_AUTH_STATUS", '{"loggedIn": false}')
    srv = server(ADVISOR_BACKEND="auto")
    assert srv._active_backend() == "unavailable"
    monkeypatch.setenv("FAKE_AUTH_STATUS", '{"loggedIn": true,'
                       ' "authMethod": "claude.ai", "subscriptionType": "pro"}')
    srv._set_oauth_token("oat-brand-new")
    assert srv._active_backend() == "claude-code"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_store_is_not_world_readable(server, home, fake_claude):
    srv = server()
    srv._set_oauth_token("oat-secret")
    assert oct(creds_path(home).stat().st_mode)[-3:] == "600"


def test_a_saved_token_is_never_echoed_back(server, home, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    assert "oat-secret-value" not in srv._set_oauth_token("oat-secret-value")


def test_an_empty_token_is_rejected(server, home, fake_claude):
    with pytest.raises(ValueError):
        server()._set_oauth_token("   ")


def test_a_pasted_command_line_is_rejected(server, home, fake_claude):
    """Users paste the whole command; catch it instead of storing garbage."""
    with pytest.raises(ValueError):
        server()._set_oauth_token("claude setup-token")


def test_disconnecting_removes_the_stored_token(server, home, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    srv._set_oauth_token("oat-secret")
    srv._clear_oauth_token()
    assert srv._oauth_token() is None
    assert not creds_path(home).exists() or not json.loads(
        creds_path(home).read_text(encoding="utf-8")).get("claude_code_oauth_token")


def test_disconnecting_when_nothing_is_stored_is_harmless(server, home, fake_claude):
    assert server()._clear_oauth_token()


# --------------------------------------------------------------------------
# The interactive login flow
# --------------------------------------------------------------------------

@pytest.fixture
def spawned(monkeypatch):
    """Capture the login command instead of opening a real console."""
    calls = []

    def _install(srv, ok=True):
        def fake_spawn(cmd):
            calls.append(cmd)
            if not ok:
                raise RuntimeError("no console available")
            return "a new terminal window"
        monkeypatch.setattr(srv, "_spawn_login_console", fake_spawn)
        return calls
    return _install


def test_login_reports_an_already_connected_account(server, home, fake_claude,
                                                    spawned):
    srv = server(ADVISOR_BACKEND="auto")
    calls = spawned(srv)
    report = srv._login()
    assert "already" in report.lower()
    assert "pro" in report.lower()
    assert not calls, "must not re-launch OAuth for a connected account"


def test_login_launches_the_subscription_oauth_flow(server, home, fake_claude,
                                                    spawned, monkeypatch):
    monkeypatch.setenv("FAKE_AUTH_STATUS", '{"loggedIn": false}')
    srv = server(ADVISOR_BACKEND="auto")
    calls = spawned(srv)
    srv._login(wait_seconds=0)
    assert calls, "no login flow was started"
    cmd = calls[0]
    assert "auth" in cmd and "login" in cmd
    # --claudeai is the subscription flow; --console would bill API credits.
    assert "--claudeai" in cmd
    assert "--console" not in cmd


def test_login_can_be_forced_for_a_connected_account(server, home, fake_claude,
                                                     spawned):
    srv = server(ADVISOR_BACKEND="auto")
    calls = spawned(srv)
    srv._login(force=True, wait_seconds=0)
    assert calls


def test_login_waits_for_the_browser_flow_then_confirms(server, home,
                                                        fake_claude, spawned,
                                                        monkeypatch):
    srv = server(ADVISOR_BACKEND="auto")
    spawned(srv)
    states = [{"loggedIn": False}, {"loggedIn": False},
              {"loggedIn": True, "authMethod": "claude.ai",
               "subscriptionType": "max", "email": "u@example.com"}]
    monkeypatch.setattr(srv, "_cli_auth_status",
                        lambda: states.pop(0) if len(states) > 1 else states[0])
    monkeypatch.setattr(srv, "_LOGIN_POLL_SECONDS", 0)
    report = srv._login(force=True, wait_seconds=30)
    assert "connected" in report.lower()
    assert "max" in report.lower()


def test_a_successful_login_switches_the_active_backend(server, home,
                                                        fake_claude, spawned,
                                                        monkeypatch):
    """_active_backend is cached; a fresh login has to invalidate it."""
    monkeypatch.setenv("FAKE_AUTH_STATUS", '{"loggedIn": false}')
    srv = server(ADVISOR_BACKEND="auto", ANTHROPIC_API_KEY="sk-ant-x")
    assert srv._active_backend() == "api"
    states = [{"loggedIn": True, "authMethod": "claude.ai",
               "subscriptionType": "pro"}]
    monkeypatch.setattr(srv, "_cli_auth_status", lambda: states[0])
    monkeypatch.setattr(srv, "_LOGIN_POLL_SECONDS", 0)
    srv._login(force=True, wait_seconds=30)
    assert srv._active_backend() == "claude-code"


def test_login_that_is_still_pending_is_not_an_error(server, home, fake_claude,
                                                     spawned, monkeypatch):
    """The user may take longer than the poll window; say so, do not fail."""
    monkeypatch.setenv("FAKE_AUTH_STATUS", '{"loggedIn": false}')
    srv = server(ADVISOR_BACKEND="auto")
    spawned(srv)
    monkeypatch.setattr(srv, "_LOGIN_POLL_SECONDS", 0)
    report = srv._login(wait_seconds=1)
    assert "advisor_login" in report  # tells the caller how to re-check
    assert "not" in report.lower()


def test_login_falls_back_to_instructions_without_a_console(
        server, home, fake_claude, spawned, monkeypatch):
    """Headless boxes, containers and remote transports have no desktop."""
    monkeypatch.setenv("FAKE_AUTH_STATUS", '{"loggedIn": false}')
    srv = server(ADVISOR_BACKEND="auto")
    spawned(srv, ok=False)
    report = srv._login(wait_seconds=0)
    assert "claude auth login" in report
    assert "advisor_set_token" in report
    assert "claude setup-token" in report


def test_login_without_the_cli_explains_how_to_get_it(server, home, no_claude):
    srv = server(ADVISOR_BACKEND="auto")
    with pytest.raises(RuntimeError) as exc:
        srv._login()
    assert "Claude Code" in str(exc.value)


def test_login_flags_a_console_account_as_the_wrong_kind(server, home,
                                                         fake_claude,
                                                         monkeypatch):
    """`loggedIn: true` via an API key still bills the Console account, which
    is exactly what the user is trying to get away from."""
    monkeypatch.setenv("FAKE_AUTH_STATUS", '{"loggedIn": true,'
                       ' "authMethod": "apiKey"}')
    srv = server(ADVISOR_BACKEND="auto")
    report = srv._login()
    assert "subscription" in report.lower()
    assert "apikey" in report.lower() or "api key" in report.lower()


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def test_status_says_where_the_token_came_from(server, home, fake_claude):
    write_creds(home, claude_code_oauth_token="oat-stored")
    srv = server(ADVISOR_BACKEND="claude-code")
    assert "stored" in srv.advisor_status().lower()


def test_auth_check_points_at_the_easy_path_when_disconnected(
        server, home, no_claude, monkeypatch):
    srv = server(ADVISOR_BACKEND="auto")
    assert "advisor_login" in srv._auth_report()


def test_the_no_credentials_error_points_at_the_easy_path(server, home, no_claude):
    srv = server(ADVISOR_BACKEND="auto")
    with pytest.raises(RuntimeError) as exc:
        srv._consult(question="q")
    assert "advisor_login" in str(exc.value)
