from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from mealie import MealieFetcher
from mealie.client import MealieApiError
from tools.errors import tool_error_boundary
from tools.recipe_tools import recipe_page_result

_CONCISE_FIELDS = ("id", "name", "slug", "description", "position", "public")


def _household_cookbooks(mealie: MealieFetcher) -> List[Dict[str, Any]]:
    """Fetch the caller's household cookbooks, ordered by position.

    Mealie's cookbook list holds every household in the group, so it is
    narrowed to the caller's household here.
    """
    own = mealie.get_current_user().get("householdId")
    if not own:
        raise ValueError("Mealie did not report your household")
    page = mealie.get_cookbooks(
        per_page=-1, order_by="position", order_direction="asc"
    )
    items = page.get("items") if isinstance(page, dict) else None
    if not isinstance(items, list):
        raise ValueError("Mealie returned an unexpected cookbook list")
    own = str(own).lower()
    cookbooks = [
        cookbook
        for cookbook in items
        if isinstance(cookbook, dict)
        and str(cookbook.get("householdId") or "").lower() == own
    ]
    return sorted(cookbooks, key=lambda cookbook: cookbook.get("position") or 0)


def register_cookbook_tools(mcp: FastMCP, mealie: MealieFetcher) -> None:
    """Register all cookbook-related tools with the MCP server."""

    @mcp.tool()
    def get_cookbooks(concise: bool = True) -> List[Dict[str, Any]]:
        """List the caller's household cookbooks, ordered by position.

        Cookbooks belong to the household, like meal plans and shopping lists.
        Each one is a saved recipe filter over categories, tags, tools, and
        more. Use get_cookbook_recipes with a cookbook's slug or id to list
        the recipes it contains.

        Args:
            concise: Return only id, name, slug, description, position, and
                public per cookbook (default). Set False for the full Mealie
                cookbook objects, including queryFilterString.

        Returns:
            List[Dict[str, Any]]: The household's cookbooks ordered by position.
        """
        with tool_error_boundary("Error fetching cookbooks"):
            cookbooks = _household_cookbooks(mealie)
            if not concise:
                return cookbooks
            return [
                {field: cookbook.get(field) for field in _CONCISE_FIELDS}
                for cookbook in cookbooks
            ]

    @mcp.tool()
    def get_cookbook_recipes(
        cookbook: str,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        concise: bool = True,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List the recipes in a cookbook, with optional search and pagination.

        Mealie applies the cookbook's filter itself, so results always match
        what the cookbook shows in Mealie.

        Args:
            cookbook: The cookbook's slug or id, not its display name. Use
                get_cookbooks to find it. Unknown cookbooks raise an error.
            page: Page number for pagination.
            per_page: Number of items per page.
            concise: Return only id, name, slug, tag names, totalTime, and the
                owning household's slug per recipe (default), as get_recipes
                does. Set False for the full Mealie recipe summaries.
            search: Filters the cookbook's recipes by name or description.

        Returns:
            Dict[str, Any]: Pagination info (page, perPage, total, totalPages) and
                recipe items, concise by default or full summaries when concise=False.
        """
        with tool_error_boundary("Error fetching cookbook recipes"):
            # Mealie treats an empty cookbook as no filter and returns every recipe.
            key = cookbook.strip()
            if not key:
                raise ValueError("Cookbook slug or id cannot be empty")
            try:
                result = mealie.get_recipes(
                    cookbook=key, search=search, page=page, per_page=per_page
                )
            except MealieApiError as error:
                if error.status_code != 404:
                    raise
                available = ", ".join(
                    str(c.get("slug")) for c in _household_cookbooks(mealie) if c.get("slug")
                )
                raise ValueError(
                    f"Unknown cookbook: {key}. "
                    f"Available cookbooks: {available or 'none'}"
                ) from None
            return recipe_page_result(mealie, result, concise)
