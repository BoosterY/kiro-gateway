"""
Native MCP server exposing Kiro's web_search as a client-driven tool.

This module runs an MCP (Model Context Protocol) server over StreamableHTTP,
mounted on the gateway's FastAPI app. Unlike the legacy in-gateway injection
(Path A/B in mcp_tools.py, which dumped raw results back into the stream and
never let the model synthesize them), this endpoint lets an MCP-capable client
(e.g. opencode) drive the tool loop:

    client discovers web_search via tools/list
      -> model emits a web_search tool_use
      -> client calls tools/call here
      -> we invoke Kiro InvokeMCP (host fix applied in mcp_tools.call_kiro_mcp_api)
      -> client feeds the result back as a tool_result
      -> model synthesizes a natural-language answer

The account_manager is shared from the parent FastAPI app via a module-level
holder (same process), set during the app lifespan.
"""

from loguru import logger
from mcp.server import MCPServer

from kiro.mcp_tools import call_kiro_mcp_api, generate_search_summary

# ---------------------------------------------------------------------------
# account_manager bridge
#
# The MCP tool handler runs in the same process as the gateway, but under a
# separate ASGI sub-app, so it cannot reach request.app.state. main.py injects
# the AccountManager here during startup via set_account_manager().
# ---------------------------------------------------------------------------
_account_manager = None


def set_account_manager(account_manager) -> None:
    """Inject the shared AccountManager (called from main.py lifespan)."""
    global _account_manager
    _account_manager = account_manager
    logger.info("MCP server: account_manager injected")


# ---------------------------------------------------------------------------
# MCPServer instance
#
# In mcp 2.x, stateless_http is passed to streamable_http_app() rather than
# the constructor. See main.py where .streamable_http_app(stateless_http=True)
# is called at mount time so each request is self-contained (no server-side
# session state to track).
# ---------------------------------------------------------------------------
mcp = MCPServer("kiro-tools")


@mcp.tool(
    name="web_search",
    description=(
        "Search the web for current, up-to-date information. Use this when you "
        "need facts, news, prices, versions, or anything that may have changed "
        "recently and is not in your training data. Returns titles, URLs, "
        "publish dates and content snippets you should synthesize into an answer."
    ),
)
async def web_search(query: str) -> str:
    """
    Execute a Kiro web search and return formatted results for the model.

    Args:
        query: The search query (Kiro caps this at ~200 chars).

    Returns:
        Human-readable, tag-wrapped search results ready for model synthesis,
        or an error string if the search could not be performed.
    """
    if _account_manager is None:
        logger.error("MCP web_search called before account_manager was injected")
        return "web_search unavailable: gateway account system not initialized."

    account = _account_manager.get_first_account()
    if account is None or getattr(account, "auth_manager", None) is None:
        logger.error("MCP web_search: no usable account/auth_manager available")
        return "web_search unavailable: no authenticated Kiro account."

    logger.info(f"MCP web_search query={query!r}")
    tool_use_id, results = await call_kiro_mcp_api(query, account.auth_manager)

    if results is None:
        logger.warning(f"MCP web_search returned no results for query={query!r}")
        return f'No search results (the upstream search failed) for "{query}".'

    return generate_search_summary(query, results)
