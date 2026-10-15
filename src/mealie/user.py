import logging
from typing import Any, Dict

logger = logging.getLogger("mealie-mcp")


class UserMixin:
    """Mixin class for user-related API endpoints"""

    def get_current_user(self) -> Dict[str, Any]:
        """Get information about the currently logged in user.

        Returns:
            Dictionary containing user details such as id, username, email, and other profile information.
        """
        logger.info({"message": "Retrieving current user information"})
        return self._handle_request("GET", "/api/users/self")

    def get_current_user_favorites(self) -> Dict[str, Any]:
        """Get the recipes the currently logged in user has favorited.

        Returns:
            Dictionary with a "ratings" list; each entry holds recipeId, rating,
            and isFavorite. Recipe details are not included.
        """
        logger.info({"message": "Retrieving current user favorites"})
        return self._handle_request("GET", "/api/users/self/favorites")

    def get_current_user_ratings(self) -> Dict[str, Any]:
        """Get every rating and favorite of the currently logged in user.

        Returns:
            Dictionary with a "ratings" list; each entry holds recipeId, rating
            (null when only favorited), and isFavorite.
        """
        logger.info({"message": "Retrieving current user ratings"})
        return self._handle_request("GET", "/api/users/self/ratings")
