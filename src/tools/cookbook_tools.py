from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from mealie import MealieFetcher
from mealie.client import MealieApiError
from tools.errors import tool_error_boundary
from tools.recipe_tools import list_recipes, recipe_order, recipe_page_result

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


def _cookbook_slug_by_name(mealie: MealieFetcher, name: str) -> str:
    """Find a household cookbook by its display name, ignoring case.

    Mealie itself only resolves cookbook ids and slugs.
    """
    cookbooks = _household_cookbooks(mealie)
    matches = [
        cookbook
        for cookbook in cookbooks
        if str(cookbook.get("name") or "").casefold() == name.casefold()
    ]
    if len(matches) > 1:
        slugs = ", ".join(str(cookbook.get("slug")) for cookbook in matches)
        raise ValueError(
            f"Cookbook name {name} matches several cookbooks: {slugs}. "
            "Pass one of these slugs instead"
        )
    if not matches or not matches[0].get("slug"):
        available = ", ".join(
            str(c.get("slug")) for c in cookbooks if c.get("slug")
        )
        raise ValueError(
            f"Unknown cookbook: {name}. "
            f"Available cookbooks: {available or 'none'}"
        )
    return str(matches[0]["slug"])


def register_cookbook_tools(mcp: FastMCP, mealie: MealieFetcher) -> None:
    """Register all cookbook-related tools with the MCP server."""

    @mcp.tool()
    def get_cookbooks(concise: bool = True) -> Dict[str, Any]:
        """List the caller's household cookbooks, ordered by position.

        Cookbooks belong to the household, like meal plans and shopping lists.
        Each one is a saved recipe filter over categories, tags, tools, and
        more. Use get_cookbook_recipes with a cookbook's slug, id, or name to
        list the recipes it contains.

        Args:
            concise: Return only id, name, slug, description, position, and
                public per cookbook (default). Set False for the full Mealie
                cookbook objects, including queryFilterString.

        Returns:
            Dict[str, Any]: {"items": [...], "total": n}, with the household's
                cookbooks ordered by position in items, even when there are
                none or only one.
        """
        with tool_error_boundary("Error fetching cookbooks"):
            cookbooks = _household_cookbooks(mealie)
            if concise:
                cookbooks = [
                    {field: cookbook.get(field) for field in _CONCISE_FIELDS}
                    for cookbook in cookbooks
                ]
            return {"items": cookbooks, "total": len(cookbooks)}

    @mcp.tool()
    def get_cookbook_recipes(
        cookbook: str,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        concise: bool = True,
        search: Optional[str] = None,
        order_by: Optional[str] = None,
        order_direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List the recipes in a cookbook, with optional search, sorting, and pagination.

        Mealie applies the cookbook's filter itself, so results always match
        what the cookbook shows in Mealie.

        Args:
            cookbook: The cookbook's id, slug, or display name, tried in that
                order; names match case-insensitively among the household's
                cookbooks. A name shared by several cookbooks raises an error
                listing their slugs, and unknown cookbooks raise an error
                listing the available ones. Use get_cookbooks to find them.
            page: Page number for pagination.
            per_page: Number of items per page.
            concise: Return only id, name, slug, tag names, totalTime, rating,
                userRating, and the owning household's slug per recipe
                (default), as get_recipes does. Set False for the full Mealie
                recipe summaries, plus userRating.
            search: Filters the cookbook's recipes by name, description, or
                ingredients. Without order_by, matches are ordered by
                relevance; an explicit order_by sorts them instead.
            order_by: name (default unless searching), rating, date_added, or
                last_made. order_by="rating" sorts by your own rating where
                you set one, otherwise by the recipe rating; unrated recipes
                sort last. last_made uses your household's last-made date;
                never-made recipes sort last.
            order_direction: asc or desc. Defaults to asc for name and desc
                for rating, date_added, and last_made.

        Returns:
            Dict[str, Any]: page, perPage, total, totalPages, and items. Each
                item's rating is the recipe-level rating (null when unrated);
                userRating is the caller's own rating, or null.
        """
        with tool_error_boundary("Error fetching cookbook recipes"):
            # Mealie treats an empty cookbook as no filter and returns every recipe.
            key = cookbook.strip()
            if not key:
                raise ValueError("Cookbook slug, id, or name cannot be empty")
            order = recipe_order(order_by, order_direction, search)

            def fetch(cookbook_key: str) -> Dict[str, Any]:
                return list_recipes(
                    mealie,
                    search=search,
                    order=order,
                    page=page,
                    per_page=per_page,
                    cookbook=cookbook_key,
                )

            try:
                result = fetch(key)
            except MealieApiError as error:
                if error.status_code != 404:
                    raise
                result = fetch(_cookbook_slug_by_name(mealie, key))
            return recipe_page_result(mealie, result, concise)
