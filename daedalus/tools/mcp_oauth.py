"""OAuth linking for remote MCP servers that require it (authorization code + PKCE).

The link is interactive and owner-only: ``McpOAuthBegin`` returns an authorization
URL that the human owner must open and approve on the server's own page; the owner
then pastes the redirect URL back and ``McpOAuthFinish`` exchanges the code for
tokens, which are stored on the state volume (mode 0600). Tokens are never shown in
tool results or chat.
"""

from __future__ import annotations

from protocore.contracts.tools import ToolContext
from protocore.contracts.types import ToolResult
from protocore.tools.decorator import tool

from daedalus.tools._common import error, ok, services_for


def _hook(context: ToolContext):  # type: ignore[no-untyped-def]
    manager = services_for(context).extra.get("manager")
    return manager.service_hooks.get("mcp") if manager is not None else None


@tool(
    name="McpOAuthStatus",
    description=(
        "Show whether an MCP server is configured for OAuth and already linked. Only relevant for "
        "remote HTTP MCP servers that require OAuth instead of a static bearer key."
    ),
)
async def mcp_oauth_status(context: ToolContext, server: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "MCP is not available in this session")
    try:
        return ok(context, await hook("oauth_status", session_id=context.session_id, server=server))
    except (KeyError, RuntimeError) as exc:
        return error(context, str(exc))


@tool(
    name="McpOAuthBegin",
    description=(
        "Start the OAuth link for an HTTP MCP server: register a client with the server and return "
        "the authorization URL. ONLY the human owner may open it — never fetch it yourself. After "
        "the owner approves and the browser lands on an unreachable local address, ask them to paste "
        "the full redirect URL back, then call McpOAuthFinish."
    ),
)
async def mcp_oauth_begin(context: ToolContext, server: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "MCP is not available in this session")
    try:
        return ok(context, await hook("oauth_begin", session_id=context.session_id, server=server))
    except (KeyError, RuntimeError) as exc:
        return error(context, str(exc))


@tool(
    name="McpOAuthFinish",
    description=(
        "Complete the OAuth link for an MCP server with the redirect URL the owner pasted back after "
        "approving the McpOAuthBegin link. Exchanges the code for tokens and stores them; then enable "
        "the server with McpEnable."
    ),
)
async def mcp_oauth_finish(context: ToolContext, server: str, redirect_url: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "MCP is not available in this session")
    try:
        return ok(
            context,
            await hook("oauth_finish", session_id=context.session_id, server=server, redirect_url=redirect_url),
        )
    except (KeyError, RuntimeError) as exc:
        return error(context, str(exc))


@tool(
    name="McpOAuthDisconnect",
    description="Forget the stored OAuth tokens of an MCP server (the server keeps its registration; relink with McpOAuthBegin).",
)
async def mcp_oauth_disconnect(context: ToolContext, server: str) -> ToolResult:
    hook = _hook(context)
    if hook is None:
        return error(context, "MCP is not available in this session")
    try:
        return ok(context, await hook("oauth_disconnect", session_id=context.session_id, server=server))
    except (KeyError, RuntimeError) as exc:
        return error(context, str(exc))


TOOLS = [mcp_oauth_status, mcp_oauth_begin, mcp_oauth_finish, mcp_oauth_disconnect]

__all__ = ["TOOLS"]
