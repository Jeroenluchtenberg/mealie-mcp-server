"""The one pagination envelope every paginated tool returns."""

import math
from typing import Any, Dict, List, Optional

# Mealie's default page size when a request does not set perPage.
DEFAULT_PER_PAGE = 50


def page_result(page: Any, items: Optional[List[Any]] = None) -> Dict[str, Any]:
    """Shape a page as page, perPage, total, totalPages, and items.

    Mealie names the keys per_page and total_pages and adds next/previous API
    paths that are no use to a model, so only these five keys are kept.
    ``items`` replaces the page's own items when the caller reshaped them.
    """
    if not isinstance(page, dict):
        raise ValueError("Mealie returned an unexpected page")

    def pick(*keys: str) -> Any:
        return next((page[key] for key in keys if key in page), None)

    if items is None:
        items = page.get("items")
    if not isinstance(items, list):
        raise ValueError("Mealie returned an unexpected page")
    return {
        "page": pick("page"),
        "perPage": pick("perPage", "per_page"),
        "total": pick("total"),
        "totalPages": pick("totalPages", "total_pages"),
        "items": items,
    }


def paginate(
    items: List[Any], page: Optional[int], per_page: Optional[int]
) -> Dict[str, Any]:
    """Page over a list built here, with Mealie's defaults (-1 means all)."""
    page = page or 1
    per_page = per_page or DEFAULT_PER_PAGE
    if per_page < 1:
        return page_result(
            {"page": 1, "perPage": per_page, "total": len(items), "totalPages": 1},
            items,
        )
    start = (page - 1) * per_page
    return page_result(
        {
            "page": page,
            "perPage": per_page,
            "total": len(items),
            "totalPages": math.ceil(len(items) / per_page),
        },
        items[start : start + per_page],
    )
