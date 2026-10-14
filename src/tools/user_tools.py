from typing import Any, Dict

from mcp.server.fastmcp import FastMCP

from mealie import MealieFetcher
from tools.errors import tool_error_boundary


def register_user_tools(mcp: FastMCP, mealie: MealieFetcher) -> None:
    """Register all user-related tools with the MCP server."""

    @mcp.tool()
    def whoami() -> Dict[str, Any]:
        """Identify the current Mealie user and their household and group.

        Read-only. Recipes are shared across the group; meal plans and shopping
        lists belong to the household. To list only this user's household
        recipes, call get_recipes(households=["self"]).

        Returns:
            Dict[str, Any]: username, plus household and group each with id,
                slug, and name.
        """
        with tool_error_boundary("Error fetching current user"):
            user = mealie.get_current_user()
            if not isinstance(user, dict):
                raise ValueError("Mealie returned an unexpected user record")
            return {
                "username": user.get("username"),
                "household": {
                    "id": user.get("householdId"),
                    "slug": user.get("householdSlug"),
                    "name": user.get("household"),
                },
                "group": {
                    "id": user.get("groupId"),
                    "slug": user.get("groupSlug"),
                    "name": user.get("group"),
                },
            }
