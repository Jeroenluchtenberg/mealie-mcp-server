"""Shared test fixtures.

Tests exercise the MCP tools end-to-end against a fake Mealie client that
records the HTTP requests the mixins would make (method, url, json, params)
and returns canned responses. No network access is required.
"""

import re
from copy import deepcopy

import pytest
from mcp.server.fastmcp import FastMCP

from mealie import MealieFetcher
from mealie.client import MealieApiError
from tools import register_all_tools

# A minimal but schema-valid recipe payload (satisfies the required Recipe
# fields), used as the response to GET /api/recipes/<slug>.
BASE_RECIPE = {
    "id": "00000000-0000-0000-0000-000000000000",
    "userId": "11111111-1111-1111-1111-111111111111",
    "householdId": "22222222-2222-2222-2222-222222222222",
    "groupId": "33333333-3333-3333-3333-333333333333",
    "name": "Test Recipe",
    "slug": "test-recipe",
    "dateAdded": "2024-01-01",
    "dateUpdated": "2024-01-01T00:00:00",
    "createdAt": "2024-01-01T00:00:00",
    "updatedAt": "2024-01-01T00:00:00",
    # Mealie seeds these from the household preferences, so a fresh recipe does
    # not necessarily start all-false.
    "settings": {
        "public": False,
        "showNutrition": True,
        "showAssets": True,
        "landscapeView": False,
        "disableComments": False,
        "locked": True,
    },
}


def _parsed(text):
    """A ParsedIngredient shaped like Mealie's, with the full food/unit records.

    Unit is left unmatched so the flattening keeps a null visible.
    """
    return {
        "input": text,
        "confidence": {
            "average": 0.996,
            "comment": 0.993,
            "name": None,
            "unit": 0.999,
            "quantity": 1.0,
            "food": 0.991,
        },
        "ingredient": {
            "quantity": 0.25,
            "unit": None,
            "food": {
                "id": "a0819c33-1a5e-4374-9151-ed85160c0049",
                "name": "onion",
                "pluralName": "onions",
                "description": "",
                "extras": {},
                "labelId": None,
                "aliases": [],
                "householdsWithIngredientFood": [],
                "label": None,
                "createdAt": "2026-08-03T02:15:22.603088Z",
                "updatedAt": "2026-08-03T02:15:22.603092Z",
            },
            "referencedRecipe": None,
            "note": "chopped",
            "display": "¹/₄ cup onion chopped",
            "title": None,
            "originalText": None,
            "referenceId": "75e1853f-3cb1-49b9-b2ca-26ae76da256b",
        },
    }


OWN_HOUSEHOLD_ID = "0b6f7b2a-2f4e-4a8e-9a43-5d1c2f3e4a01"
OTHER_HOUSEHOLD_ID = "7c1d9e3f-8a2b-4c5d-9e6f-1a2b3c4d5e02"

HOUSEHOLDS = [
    {
        "groupId": "5f3a1b2c-3d4e-4f5a-8b6c-7d8e9f0a1b03",
        "name": "Luchtenberg",
        "id": OWN_HOUSEHOLD_ID,
        "slug": "luchtenberg",
        "preferences": None,
    },
    {
        "groupId": "5f3a1b2c-3d4e-4f5a-8b6c-7d8e9f0a1b03",
        "name": "Neighbours",
        "id": OTHER_HOUSEHOLD_ID,
        "slug": "neighbours",
        "preferences": None,
    },
]

# Subset of Mealie's UserOut (GET /api/users/self).
CURRENT_USER = {
    "id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c04",
    "username": "tester",
    "fullName": "Test User",
    "email": "tester@example.com",
    "admin": False,
    "group": "Home",
    "groupId": "5f3a1b2c-3d4e-4f5a-8b6c-7d8e9f0a1b03",
    "groupSlug": "home",
    "household": "Luchtenberg",
    "householdId": OWN_HOUSEHOLD_ID,
    "householdSlug": "luchtenberg",
}


class FakeFetcher(MealieFetcher):
    """MealieFetcher with the network layer replaced by a recorder.

    Skips MealieClient.__init__ (no connection) and overrides _handle_request
    so the real mixin methods run and we can assert on the requests they build.
    """

    def __init__(self):
        self.requests = []
        self.created_slug = "test-recipe"
        self.recipe = deepcopy(BASE_RECIPE)
        self.responses = {}
        # url fragment -> MealieApiError to raise, for client-failure tests
        self.failures = {}
        # Optional food catalogue; when set, /api/foods searches and reads use it.
        self.foods = None
        self.user = deepcopy(CURRENT_USER)
        self.households = deepcopy(HOUSEHOLDS)
        # Entries of GET /api/users/self/favorites (UserRatingSummary); they
        # are also the current user's favoritedBy matches in /api/recipes.
        self.favorites = []
        # Entries of GET /api/users/self/ratings (UserRatingSummary).
        self.ratings = []
        # Items of GET /api/groups/members (UserSummary); the current user only.
        self.members = [
            {
                key: CURRENT_USER[key]
                for key in ("id", "username", "fullName", "groupId", "householdId")
            }
        ]
        # Other users' favorited recipe ids, by user id.
        self.member_favorites = {}
        # Optional recipe summaries; when set, /api/recipes honours `id IN`
        # and favoritedBy.id queryFilters, search, and the households filter.
        self.recipe_catalogue = None

    def fail_on(self, url_contains, status_code=422, message="Mealie rejected it"):
        """Make any request whose url contains this fragment raise."""
        self.failures[url_contains] = MealieApiError(status_code, message)

    def _handle_request(self, method, url, **kwargs):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "json": deepcopy(kwargs.get("json")),
                "params": deepcopy(kwargs.get("params")),
                "files": kwargs.get("files"),
                "data": deepcopy(kwargs.get("data")),
            }
        )
        for fragment, error in self.failures.items():
            if fragment in url:
                raise error
        if (method, url) in self.responses:
            return deepcopy(self.responses[method, url])
        resource_url, _, resource_id = url.rpartition("/")
        if self.foods is not None and method == "GET" and url == "/api/foods":
            return self._search_foods((kwargs.get("params") or {}).get("search"))
        if self.foods is not None and method == "GET" and resource_url == "/api/foods":
            match = next((f for f in self.foods if f["id"] == resource_id), None)
            if match is None:
                raise MealieApiError(404, "Food not found")
            return deepcopy(match)
        if method == "GET" and url == "/api/users/self":
            return deepcopy(self.user)
        if method == "GET" and url == "/api/users/self/favorites":
            return {"ratings": deepcopy(self.favorites)}
        if method == "GET" and url == "/api/users/self/ratings":
            return {"ratings": deepcopy(self.ratings)}
        if method == "GET" and url == "/api/groups/members":
            return {
                "items": deepcopy(self.members),
                "page": 1,
                "per_page": -1,
                "total": len(self.members),
                "total_pages": 1,
            }
        if self.recipe_catalogue is not None and method == "GET" and url == "/api/recipes":
            return self._filter_recipes(kwargs.get("params") or {})
        if method == "GET" and url == "/api/groups/households":
            return {
                "items": deepcopy(self.households),
                "page": 1,
                "perPage": -1,
                "total": len(self.households),
                "totalPages": 1,
            }
        if method == "POST" and url == "/api/recipes/create/ai":
            return self.created_slug
        if method == "POST" and url == "/api/recipes":
            name = (kwargs.get("json") or {}).get("name")
            if name:
                # reflect the created name on subsequent GET (like the real API)
                self.recipe = {**self.recipe, "name": name, "slug": self.created_slug}
            return self.created_slug
        if method == "GET" and url.startswith("/api/recipes/") and url.count("/") == 3:
            return deepcopy(self.recipe)
        if (
            method in ("POST", "PUT")
            and url.startswith("/api/recipes/")
            and url.count("/") == 4
            and url.endswith("/image")
        ):
            return {"image": "1"}
        if method == "POST" and url.startswith("/api/recipes/") and url.endswith("/assets"):
            data = kwargs.get("data") or {}
            return {
                "name": data.get("name"),
                "icon": data.get("icon"),
                "fileName": f"{data.get('name')}.{data.get('extension')}",
            }
        if method == "POST" and url == "/api/parser/ingredient":
            return _parsed((kwargs.get("json") or {}).get("ingredient"))
        if method == "POST" and url == "/api/parser/ingredients":
            return [
                _parsed(text) for text in (kwargs.get("json") or {}).get("ingredients", [])
            ]
        if method in ("PUT", "PATCH") and resource_url == "/api/recipes":
            return deepcopy(kwargs.get("json", {}))
        # single-record GET (the fetch-merge update path reads the existing record)
        if method == "GET" and resource_url in (
            "/api/foods",
            "/api/units",
            "/api/organizers/tools",
        ):
            return {
                "id": url.rsplit("/", 1)[-1],
                "name": "Existing",
                "pluralName": "Existings",
                "description": "old",
                **({"slug": "existing"} if resource_url == "/api/organizers/tools" else {}),
            }
        if method == "GET" and resource_url == "/api/organizers/categories":
            item_id = url.rsplit("/", 1)[-1]
            return {"id": item_id, "name": "Category", "slug": "category"}
        if method == "GET" and resource_url == "/api/organizers/tags":
            item_id = url.rsplit("/", 1)[-1]
            return {"id": item_id, "name": "Tag", "slug": "tag"}
        if method == "GET" and resource_url == "/api/households/mealplans":
            return {
                "id": url.rsplit("/", 1)[-1],
                "groupId": "group-1",
                "userId": "user-1",
                "date": "2026-07-12",
                "entryType": "dinner",
                "title": "Existing meal",
            }
        if method == "GET" and resource_url == "/api/households/shopping/lists":
            return {
                "id": url.rsplit("/", 1)[-1],
                "groupId": "group-1",
                "userId": "user-1",
                "name": "Existing list",
            }
        # list endpoints
        if method == "GET" and url in (
            "/api/foods",
            "/api/units",
            "/api/organizers/tools",
            "/api/organizers/categories",
            "/api/organizers/tags",
            "/api/recipes",
            "/api/households/mealplans",
            "/api/households/shopping/lists",
        ):
            return {
                "items": [{"id": "x1", "name": "Sample"}],
                "page": 1,
                "perPage": 50,
                "total": 1,
            }
        # create echoes the body with a generated id
        if method == "POST" and url in (
            "/api/foods",
            "/api/units",
            "/api/organizers/tools",
            "/api/organizers/categories",
            "/api/organizers/tags",
            "/api/households/mealplans",
            "/api/households/shopping/lists",
        ):
            return {**deepcopy(kwargs.get("json") or {}), "id": "generated-0001"}
        # full-replace update echoes the merged body
        if method == "PUT" and resource_url in (
            "/api/foods",
            "/api/units",
            "/api/organizers/tools",
            "/api/organizers/categories",
            "/api/organizers/tags",
            "/api/households/mealplans",
            "/api/households/shopping/lists",
        ):
            return deepcopy(kwargs.get("json", {}))
        # delete (Mealie normalizes the empty body to a success payload)
        if method == "DELETE" and resource_id and resource_url in (
            "/api/recipes",
            "/api/foods",
            "/api/units",
            "/api/organizers/tools",
            "/api/organizers/categories",
            "/api/organizers/tags",
            "/api/households/mealplans",
            "/api/households/shopping/lists",
        ):
            return {"success": True, "message": "Operation completed successfully"}
        raise AssertionError(f"Unhandled fake request: {method} {url}")

    def _favorited_by(self, user_id):
        if user_id == self.user["id"]:
            return {
                entry["recipeId"]
                for entry in self.favorites
                if isinstance(entry, dict) and entry.get("isFavorite") is not False
            }
        return set(self.member_favorites.get(user_id, ()))

    def _filter_recipes(self, params):
        """Apply Mealie's filters to the catalogue.

        Supports `id IN [...]`, `favoritedBy.id = "..."`, and
        `favoritedBy.id IN [...]` queryFilters, a name-substring search, the
        households filter, and page/perPage. Only orderBy=name is applied;
        other orders keep catalogue order and tests assert the parameters.
        """
        query_filter = params.get("queryFilter") or ""
        quoted = set(re.findall(r'"([^"]+)"', query_filter))
        ids = None
        if query_filter.startswith("id IN"):
            ids = quoted
        elif query_filter.startswith("favoritedBy.id"):
            ids = set().union(*(self._favorited_by(user_id) for user_id in quoted))
        search = (params.get("search") or "").casefold()
        households = {h.lower() for h in params.get("households") or []}
        items = [
            deepcopy(recipe)
            for recipe in self.recipe_catalogue
            if (ids is None or recipe["id"] in ids)
            and search in recipe["name"].casefold()
            and (not households or recipe["householdId"].lower() in households)
        ]
        if params.get("orderBy") == "name":
            items.sort(
                key=lambda recipe: recipe["name"].lower(),
                reverse=params.get("orderDirection") == "desc",
            )
        page = params.get("page", 1)
        per_page = params.get("perPage", 50)
        total = len(items)
        if per_page > 0:
            items = items[(page - 1) * per_page : page * per_page]
        return {
            "items": items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": 1 if per_page < 1 else -(-total // per_page),
            "next": None,
            "previous": None,
        }

    def _search_foods(self, search):
        """Substring match of any search word on names and aliases, like Mealie."""
        words = (search or "").casefold().split()

        def names(food):
            return [food["name"], *(a["name"] for a in food.get("aliases", []))]

        items = [
            deepcopy(food)
            for food in self.foods
            if not words
            or any(w in n.casefold() for w in words for n in names(food))
        ]
        return {"items": items, "page": 1, "perPage": 50, "total": len(items)}

    def last(self, method=None, url_contains=None):
        """Return the most recent recorded request matching method/url filter."""
        for req in reversed(self.requests):
            if method is not None and req["method"] != method:
                continue
            if url_contains is not None and url_contains not in req["url"]:
                continue
            return req
        return None


@pytest.fixture
def fetcher():
    return FakeFetcher()


@pytest.fixture
def server(fetcher):
    mcp = FastMCP("test")
    register_all_tools(mcp, fetcher)
    return mcp, fetcher


@pytest.fixture
def invoke(server):
    """Async helper: call a tool by name with kwargs, return its parsed result."""
    import json

    mcp, _ = server

    async def _invoke(tool_name, /, **arguments):
        result = await mcp.call_tool(tool_name, arguments)
        content, structured = result if isinstance(result, tuple) else (result, None)
        if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
            return structured["result"]
        if structured is not None:
            return structured
        if content and getattr(content[0], "text", None):
            return json.loads(content[0].text)
        return content

    return _invoke
