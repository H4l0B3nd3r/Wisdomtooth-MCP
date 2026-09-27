"""The public build signs in to nothing.

Claude consults use ANTHROPIC_API_KEY, or the user's own Claude Code install,
signed in however the user signed it in. The server offers no login flow and
stores no subscription credential.
"""

import json

import pytest

LOGIN_TOOLS = ("advisor_login", "advisor_set_token", "advisor_logout")


@pytest.mark.parametrize("name", LOGIN_TOOLS)
def test_there_are_no_login_tools(server, name):
    srv = server()
    assert not hasattr(srv, name)
    assert name not in srv._TOOL_NAMES


def test_a_token_file_from_an_older_install_is_not_used(server, fake_claude,
                                                        monkeypatch, tmp_path):
    """Earlier builds stored a subscription token and injected it; this one
    uses the install's own sign-in only."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "credentials.json").write_text(
        json.dumps({"claude_code_oauth_token": "stored-token"}),
        encoding="utf-8")
    srv = server(ADVISOR_BACKEND="claude-code")
    monkeypatch.setattr(srv, "_state_dir", lambda: str(state))
    srv._consult(question="q")
    assert fake_claude.last["env"]["CLAUDE_CODE_OAUTH_TOKEN"] is None


def test_the_users_own_oauth_env_var_is_left_alone(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code", CLAUDE_CODE_OAUTH_TOKEN="own")
    srv._consult(question="q")
    assert fake_claude.last["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "own"


def test_an_api_key_is_the_advertised_path(server, no_claude):
    srv = server(ADVISOR_BACKEND="auto")
    with pytest.raises(srv.AdvisorError) as exc:
        srv._consult(question="q")
    text = str(exc.value)
    assert text.index("ANTHROPIC_API_KEY") < text.index("claude")


def test_status_and_banner_describe_the_users_own_install(server, fake_claude):
    srv = server(ADVISOR_BACKEND="claude-code")
    status = srv._status_report()
    assert "your own Claude Code install" in status
    assert "SUBSCRIPTION" not in status and "subscription token" not in status
    assert "your own Claude Code install" in srv._billing_banner()


def test_auth_check_tells_the_user_to_sign_in_themselves(server, fake_claude,
                                                         monkeypatch):
    monkeypatch.setenv("FAKE_AUTH_STATUS", '{"loggedIn": false}')
    srv = server(ADVISOR_BACKEND="auto")
    report = srv._auth_report()
    assert "`claude`" in report and "sign" in report
    assert "advisor_login" not in report and "setup-token" not in report
