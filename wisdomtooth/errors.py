"""Failures whose text the calling agent must actually see."""

try:  # mcp >= 2.0
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp.exceptions import ToolError as _ToolError


class AdvisorError(_ToolError, RuntimeError):
    """A failure whose text the calling agent must actually see.

    The SDK treats an arbitrary exception as a server crash and replaces its
    message with a generic "Error executing tool ..." -- which would hide every
    diagnostic this server produces, including the login instructions that are
    the whole point of a good auth failure. `ToolError` is the deliberate,
    message-preserving channel; RuntimeError is kept in the bases so callers
    can catch this without importing the MCP SDK.
    """


class AdvisorInputError(AdvisorError, ValueError):
    """A bad tool argument, where the message IS the fix.

    Same reasoning as `AdvisorError`: a bare `ValueError` would be swallowed by
    the SDK and the caller would be told only that something went wrong, not
    what to pass instead. `ValueError` stays in the bases because that is what
    a bad argument is.
    """


class UsageLimitError(AdvisorError):
    """Claude Code reports its usage limit is reached. Not retryable."""


class OverLimitError(AdvisorError):
    """A consult held because it would cost more than the account has left.

    Nothing was sent. The message tells the agent to ask the user, and how to
    send it anyway once they agree (`confirm_over_limit=true`).
    """
