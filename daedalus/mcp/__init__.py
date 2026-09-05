"""MCP (Model Context Protocol) client: connect to configured servers, expose their tools."""

from daedalus.mcp.manager import McpManager, McpToolProxy, mcp_tool_name

__all__ = ["McpManager", "McpToolProxy", "mcp_tool_name"]
