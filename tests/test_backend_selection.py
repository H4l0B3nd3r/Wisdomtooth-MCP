"""Which account pays.

The server's job is to prefer the user's Claude subscription and fall back to
API credits only when the subscription is not usable -- and to say, on every
answer, which one it actually charged.
"""

import pytest


def test_auto_is_the_default_backend(server, fake_claude):
    """Shipping `api` as the default silently billed a Console account."""
    assert server().BACKEND == "auto"


def test_auto_prefers_an_api_key_over_claude_code(server, fake_claude):
    """The advertised path is the Anthropic API; a signed-in Claude Code install
    is used only when no key is set."""
    srv = server(ADVISOR_BACKEND="auto", ANTHROPIC_API_KEY="sk-ant-present")
    assert srv._active_backend() == "api"


def test_auto_uses_the_users_own_claude_code_without_a_key(server, fake_claude):
    srv = server(ADVISOR_BACKEND="auto")
    assert srv._active_backend() == "claude-code"


def test_auto_skips_a_claude_code_install_that_is_not_signed_in(
        server, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_AUTH_STATUS", '{"loggedIn": false}')
    srv = server(ADVISOR_BACKEND="auto")
    assert srv._active_backend() == "unavailable"


def test_auto_falls_back_to_api_when_the_cli_is_absent(server, no_claude):
    srv = server(ADVISOR_BACKEND="auto", ANTHROPIC_API_KEY="sk-ant-present")
    assert srv._active_backend() == "api"


def test_auto_falls_back_to_api_when_the_cli_is_logged_out(
        server, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_AUTH_STATUS", '{"loggedIn": false}')
    srv = server(ADVISOR_BACKEND="auto", ANTHROPIC_API_KEY="sk-ant-present")
    assert srv._active_backend() == "api"


def test_auto_with_no_credentials_at_all_explains_both_options(server, no_claude):
    srv = server(ADVISOR_BACKEND="auto")
    with pytest.raises(RuntimeError) as exc:
        srv._consult(question="hi")
    text = str(exc.value)
    assert "ANTHROPIC_API_KEY" in text    # the advertised path
    assert "`claude`" in text             # or sign in to your own Claude Code
    assert "advisor_login" not in text    # the server signs in to nothing


def test_explicit_claude_code_never_silently_uses_the_api(
        server, fake_claude, monkeypatch):
    """An explicit subscription choice is a billing decision; honour it even if
    the CLI looks unhealthy, and fail loudly rather than spend API credits."""
    monkeypatch.setenv("FAKE_AUTH_STATUS", '{"loggedIn": false}')
    srv = server(ADVISOR_BACKEND="claude-code", ANTHROPIC_API_KEY="sk-ant-present")
    assert srv._active_backend() == "claude-code"


def test_explicit_api_backend_is_honoured(server, fake_claude):
    srv = server(ADVISOR_BACKEND="api", ANTHROPIC_API_KEY="sk-ant-present")
    assert srv._active_backend() == "api"


def test_auto_backend_resolution_is_cached(server, fake_claude):
    """`claude auth status` spawns a process; do it once, not per consult."""
    srv = server(ADVISOR_BACKEND="auto")
    for _ in range(3):
        srv._active_backend()
    auth_calls = [c for c in fake_claude.calls() if c["argv"][:1] == ["auth"]]
    assert len(auth_calls) <= 1


# --------------------------------------------------------------------------
# Claude Code at its usage limit
# --------------------------------------------------------------------------

def test_claude_code_at_its_limit_never_moves_to_the_api(
        server, fake_claude, monkeypatch):
    """Switching a user onto paid credits without being asked is a money
    decision the server does not get to make."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "usage_limit")
    srv = server(ADVISOR_BACKEND="claude-code", ANTHROPIC_API_KEY="sk-ant-present")
    monkeypatch.setattr(srv, "_consult_api",
                        lambda *a, **k: pytest.fail("must not reach the API"))
    with pytest.raises(RuntimeError):
        srv._consult(question="q")


def test_auto_notices_credentials_that_appear_after_startup(
        server, no_claude, monkeypatch):
    """A user who signs in, or sets a key, after the server started must not
    need a restart: "unavailable" is not cached."""
    srv = server(ADVISOR_BACKEND="auto")
    assert srv._active_backend() == "unavailable"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-added-later")
    assert srv._active_backend() == "api"


# --------------------------------------------------------------------------
# Detecting API credentials the way the Anthropic SDK finds them
# --------------------------------------------------------------------------

def test_an_empty_sdk_config_dir_is_not_a_credential(server, no_claude,
                                                     tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "anthropic"))
    (tmp_path / "anthropic").mkdir()
    srv = server(ADVISOR_BACKEND="auto")
    assert srv._api_credentials_present() is False


def test_the_active_sdk_profile_is_a_credential(server, no_claude, tmp_path,
                                                monkeypatch):
    base = tmp_path / "anthropic"
    (base / "configs").mkdir(parents=True)
    (base / "configs" / "work.json").write_text("{}", encoding="utf-8")
    (base / "active_config").write_text("work\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(base))
    srv = server(ADVISOR_BACKEND="auto")
    assert srv._api_credentials_present() is True
    assert srv._active_backend() == "api"


def test_the_sdk_config_dir_on_windows_is_under_appdata(server, no_claude,
                                                        tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CONFIG_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr("os.path.expanduser",
                        lambda p: p.replace("~", str(tmp_path / "home")))
    srv = server(ADVISOR_BACKEND="auto")
    monkeypatch.setattr(srv.sys, "platform", "win32")
    (tmp_path / "Anthropic" / "configs").mkdir(parents=True)
    (tmp_path / "Anthropic" / "configs" / "default.json").write_text(
        "{}", encoding="utf-8")
    assert srv._api_credentials_present() is True


def test_a_profile_name_cannot_escape_the_config_dir(server, no_claude,
                                                     tmp_path, monkeypatch):
    base = tmp_path / "anthropic"
    base.mkdir()
    (tmp_path / "evil.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(base))
    monkeypatch.setenv("ANTHROPIC_PROFILE", "../../evil")
    srv = server(ADVISOR_BACKEND="auto")
    assert srv._api_credentials_present() is False
