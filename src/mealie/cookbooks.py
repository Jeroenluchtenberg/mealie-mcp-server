import logging
from typing import Any, Dict, Optional

from utils import format_api_params

logger = logging.getLogger("mealie-mcp")


class CookbooksMixin:
    """Mixin class for cookbook-related API endpoints"""

    def get_cookbooks(
        self,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        order_by: Optional[str] = None,
        order_direction: Optional[str] = None,
        query_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get cookbooks.

        Mealie returns every cookbook in the group here, not just the caller's
        household; each item carries its householdId.

        Args:
            page: Page number to retrieve
            per_page: Number of items per page (-1 for all)
            order_by: Field to order results by
            order_direction: Direction to order results ('asc' or 'desc')
            query_filter: Advanced query filter

        Returns:
            JSON response containing cookbook items and pagination information
        """
        param_dict = {
            "page": page,
            "perPage": per_page,
            "orderBy": order_by,
            "orderDirection": order_direction,
            "queryFilter": query_filter,
        }

        params = format_api_params(param_dict)

        logger.info({"message": "Retrieving cookbooks"})
        return self._handle_request("GET", "/api/households/cookbooks", params=params)
