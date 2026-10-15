import logging
from typing import Any, Dict

logger = logging.getLogger("mealie-mcp")


class GroupMixin:
    """Mixin class for group-related API endpoints"""

    def get_current_group(self) -> Dict[str, Any]:
        """Get information about the current user's group.

        Returns:
            Dictionary containing group details such as id, name, slug, and other group information.
        """
        logger.info({"message": "Retrieving current group information"})
        return self._handle_request("GET", "/api/groups/self")

    def get_households(self) -> Dict[str, Any]:
        """List every household in the current user's group.

        Returns:
            Paginated response whose items hold each household's id, name, and slug.
        """
        logger.info({"message": "Retrieving group households"})
        return self._handle_request(
            "GET", "/api/groups/households", params={"perPage": -1}
        )

    def get_group_members(self) -> Dict[str, Any]:
        """List every user in the current user's group.

        Unlike /api/households/members, this needs no manage permission.

        Returns:
            Paginated response whose items hold each user's id, username,
            fullName, groupId, and householdId.
        """
        logger.info({"message": "Retrieving group members"})
        return self._handle_request(
            "GET", "/api/groups/members", params={"perPage": -1}
        )
