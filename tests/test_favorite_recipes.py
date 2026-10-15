"""Tests for get_favorite_recipes: user and household scope, sorting, households."""

import uuid

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import CURRENT_USER, OTHER_HOUSEHOLD_ID, OWN_HOUSEHOLD_ID
from mealie.client import MealieApiError

ME = CURRENT_USER["id"]
PARTNER = "3e2d1c0b-9a8f-4e7d-8c6b-5a4f3e2d1c05"
KID = "4f3e2d1c-0b9a-4f8e-9d7c-6b5a4f3e2d06"
NEIGHBOUR = "5a4f3e2d-1c0b-4a9f-8e8d-7c6b5a4f3e07"


def _recipe(name, household_id=OWN_HOUSEHOLD_ID, **extra):
    recipe_id = str(uuid.uuid5(uuid.NAMESPACE_URL, name))
    return {
        "id": recipe_id,
        "name": name,
        "slug": name.lower(),
        "householdId": household_id,
        "totalTime": "30 min",
        "rating": None,
        "tags": [{"id": "t1", "name": "Quick", "slug": "quick", "recipeCount": 0}],
        "recipeCategory": [
            {"id": "c1", "name": "Dinner", "slug": "dinner", "recipeCount": 0}
        ],
        "tools": [],
        **extra,
    }


def _favorite(recipe_id, rating=None):
    return {"recipeId": recipe_id, "rating": rating, "isFavorite": True}


def _member(user_id, username, household_id=OWN_HOUSEHOLD_ID):
    return {
        "id": user_id,
        "username": username,
        "fullName": username.title(),
        "groupId": CURRENT_USER["groupId"],
        "householdId": household_id,
    }


@pytest.fixture
def favorites(fetcher):
    """Three favorites (one owned by another household) and one non-favorite."""
    recipes = [
        _recipe("Stamppot"),
        _recipe("Erwtensoep", OTHER_HOUSEHOLD_ID),
        _recipe("Hutspot"),
        _recipe("Pannenkoeken"),
    ]
    fetcher.recipe_catalogue = recipes
    fetcher.favorites = [_favorite(r["id"]) for r in recipes[:3]]
    return recipes


@pytest.fixture
def household(fetcher, favorites):
    """The caller and a partner share Stamppot; a kid has no favorites, and a
    neighbour in another household is not part of the household scope."""
    stamppot, erwtensoep, hutspot, pannenkoeken = favorites
    fetcher.favorites = [_favorite(stamppot["id"]), _favorite(hutspot["id"])]
    fetcher.members += [
        _member(PARTNER, "partner"),
        _member(KID, "kid"),
        _member(NEIGHBOUR, "neighbour", OTHER_HOUSEHOLD_ID),
    ]
    fetcher.member_favorites = {
        PARTNER: [pannenkoeken["id"], stamppot["id"], erwtensoep["id"]],
        NEIGHBOUR: [erwtensoep["id"]],
    }
    return favorites


def _recipe_requests(fetcher):
    return [r for r in fetcher.requests if r["url"] == "/api/recipes"]


def _calls(fetcher):
    return [(r["method"], r["url"]) for r in fetcher.requests]


async def test_get_favorite_recipes_is_registered_read_only(server):
    mcp, _ = server
    tool = next(t for t in await mcp.list_tools() if t.name == "get_favorite_recipes")
    assert set(tool.inputSchema["properties"]) == {
        "page", "per_page", "households", "concise", "scope", "order_by",
        "order_direction",
    }
    assert "required" not in tool.inputSchema
    assert "Read-only" in tool.description
    assert "my favorites" in tool.description
    assert "family's favorites" in tool.description
    assert "favoritedBy" in tool.description and "favoriteCount" in tool.description
    assert "unrated recipes sort last" in tool.description


# --- user scope -------------------------------------------------------------


async def test_get_favorite_recipes_concise_by_default(invoke, fetcher, favorites):
    out = await invoke("get_favorite_recipes")
    concise = {"tags": ["Quick"], "totalTime": "30 min", "rating": None, "userRating": None}
    assert out == {
        "page": 1,
        "perPage": 50,
        "total": 3,
        "totalPages": 1,
        "items": [
            {
                "id": favorites[1]["id"],
                "name": "Erwtensoep",
                "slug": "erwtensoep",
                **concise,
                "household": "neighbours",
            },
            {
                "id": favorites[2]["id"],
                "name": "Hutspot",
                "slug": "hutspot",
                **concise,
                "household": "luchtenberg",
            },
            {
                "id": favorites[0]["id"],
                "name": "Stamppot",
                "slug": "stamppot",
                **concise,
                "household": "luchtenberg",
            },
        ],
    }
    assert _calls(fetcher) == [
        ("GET", "/api/users/self"),
        ("GET", "/api/recipes"),
        ("GET", "/api/users/self/ratings"),
        ("GET", "/api/groups/households"),
    ]
    assert _recipe_requests(fetcher)[0]["params"] == {
        "queryFilter": f'favoritedBy.id = "{ME}"',
        "page": 1,
        "perPage": 50,
        "orderBy": "name",
        "orderDirection": "asc",
        "orderByNullPosition": "last",
    }


async def test_get_favorite_recipes_full_summaries(invoke, fetcher, favorites):
    out = await invoke("get_favorite_recipes", concise=False)
    assert [item["name"] for item in out["items"]] == [
        "Erwtensoep", "Hutspot", "Stamppot",
    ]
    expected = {**favorites[1], "userRating": None}
    expected["tags"] = [{"id": "t1", "name": "Quick", "slug": "quick"}]
    expected["recipeCategory"] = [{"id": "c1", "name": "Dinner", "slug": "dinner"}]
    assert out["items"][0] == expected
    assert "favoritedBy" not in out["items"][0]
    assert out["total"] == 3
    assert ("GET", "/api/groups/households") not in _calls(fetcher)


async def test_get_favorite_recipes_empty(invoke, fetcher, favorites):
    fetcher.favorites = []
    out = await invoke("get_favorite_recipes")
    assert out == {"page": 1, "perPage": 50, "total": 0, "totalPages": 0, "items": []}
    assert _calls(fetcher) == [("GET", "/api/users/self"), ("GET", "/api/recipes")]


@pytest.mark.parametrize(
    ("page", "per_page", "names", "total_pages"),
    [
        (1, 2, ["Erwtensoep", "Hutspot"], 2),
        (2, 2, ["Stamppot"], 2),
        (3, 2, [], 2),
        (1, 3, ["Erwtensoep", "Hutspot", "Stamppot"], 1),
        (2, 3, [], 1),
        (3, 1, ["Stamppot"], 3),
    ],
)
async def test_get_favorite_recipes_pagination_boundaries(
    invoke, fetcher, favorites, page, per_page, names, total_pages
):
    out = await invoke("get_favorite_recipes", page=page, per_page=per_page)
    assert [item["name"] for item in out["items"]] == names
    assert out["page"] == page
    assert out["perPage"] == per_page
    assert out["total"] == 3
    assert out["totalPages"] == total_pages
    params = _recipe_requests(fetcher)[0]["params"]
    assert (params["page"], params["perPage"]) == (page, per_page)


async def test_get_favorite_recipes_rating_fields(invoke, fetcher, favorites):
    favorites[0]["rating"] = 3.5
    favorites[2]["rating"] = 0
    fetcher.ratings = [
        _favorite(favorites[0]["id"], rating=5),
        _favorite(favorites[2]["id"], rating=0),
        {"recipeId": favorites[3]["id"], "rating": 2, "isFavorite": False},
        "junk",
    ]
    out = await invoke("get_favorite_recipes")
    ratings = {item["name"]: (item["rating"], item["userRating"]) for item in out["items"]}
    assert ratings == {
        "Erwtensoep": (None, None),
        "Hutspot": (None, None),
        "Stamppot": (3.5, 5),
    }
    assert _calls(fetcher).count(("GET", "/api/users/self/ratings")) == 1


@pytest.mark.parametrize(
    ("arguments", "order"),
    [
        ({"order_by": "rating"}, ("rating", "desc")),
        ({"order_by": "date_added"}, ("date_added", "desc")),
        ({"order_by": "LAST_MADE"}, ("last_made", "desc")),
        ({"order_by": "name", "order_direction": "desc"}, ("name", "desc")),
        ({"order_by": "rating", "order_direction": " ASC "}, ("rating", "asc")),
    ],
)
async def test_get_favorite_recipes_sorting_is_passed_through(
    invoke, fetcher, favorites, arguments, order
):
    await invoke("get_favorite_recipes", **arguments)
    params = _recipe_requests(fetcher)[0]["params"]
    assert (params["orderBy"], params["orderDirection"]) == order
    assert params["orderByNullPosition"] == "last"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"order_by": "stars"}, "Unknown order_by: stars. Use one of: name, rating, date_added, last_made"),
        ({"order_by": "rating", "order_direction": "up"}, "Unknown order_direction: up"),
        ({"scope": "group"}, "Unknown scope: group. Use user or household"),
    ],
)
async def test_get_favorite_recipes_rejects_bad_options(invoke, fetcher, arguments, message):
    with pytest.raises(ToolError, match=message):
        await invoke("get_favorite_recipes", **arguments)
    assert fetcher.requests == []


async def test_get_favorite_recipes_households_self(invoke, fetcher, favorites):
    out = await invoke("get_favorite_recipes", households=["self"])
    assert [item["name"] for item in out["items"]] == ["Hutspot", "Stamppot"]
    assert out["total"] == 2
    assert _recipe_requests(fetcher)[0]["params"]["households"] == [OWN_HOUSEHOLD_ID]
    assert _calls(fetcher).count(("GET", "/api/groups/households")) == 1


async def test_get_favorite_recipes_households_slug(invoke, fetcher, favorites):
    out = await invoke("get_favorite_recipes", households=["neighbours"])
    assert [item["household"] for item in out["items"]] == ["neighbours"]


async def test_get_favorite_recipes_unknown_household_errors(invoke, fetcher, favorites):
    with pytest.raises(ToolError, match="Unknown household\\(s\\): nobody"):
        await invoke("get_favorite_recipes", households=["nobody"])
    assert "/api/recipes" not in [r["url"] for r in fetcher.requests]


async def test_get_favorite_recipes_rejects_malformed_user_id(invoke, fetcher, favorites):
    fetcher.user["id"] = '"] OR name IS NOT NULL'
    with pytest.raises(ToolError, match="invalid user id"):
        await invoke("get_favorite_recipes")
    assert _recipe_requests(fetcher) == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"page": 0}, "page must be at least 1"),
        ({"per_page": 0}, "per_page must be at least 1"),
        ({"per_page": -1}, "per_page must be at least 1"),
    ],
)
async def test_get_favorite_recipes_rejects_bad_pagination(
    invoke, fetcher, arguments, message
):
    with pytest.raises(ToolError, match=message):
        await invoke("get_favorite_recipes", **arguments)
    assert fetcher.requests == []


async def test_get_favorite_recipes_rejects_invalid_arguments(invoke, fetcher):
    with pytest.raises(ToolError):
        await invoke("get_favorite_recipes", concise="sometimes")
    with pytest.raises(ToolError):
        await invoke("get_favorite_recipes", households="self")
    assert fetcher.requests == []


@pytest.mark.parametrize(
    "url", ["/api/users/self", "/api/recipes", "/api/users/self/ratings"]
)
async def test_get_favorite_recipes_surfaces_failures(invoke, fetcher, favorites, url):
    # fail_on matches url fragments, and /api/users/self prefixes the ratings url.
    handle = fetcher._handle_request

    def failing(method, request_url, **kwargs):
        if request_url == url:
            raise MealieApiError(500, "boom")
        return handle(method, request_url, **kwargs)

    fetcher._handle_request = failing
    with pytest.raises(ToolError, match="Error fetching favorite recipes"):
        await invoke("get_favorite_recipes")


async def test_get_favorite_recipes_reports_rejected_token(invoke, fetcher, favorites):
    fetcher.fail_on("/api/users/self", 401)
    with pytest.raises(ToolError, match="HTTP 401"):
        await invoke("get_favorite_recipes")


@pytest.mark.parametrize(
    ("url", "response", "message"),
    [
        ("/api/recipes", {"detail": "nope"}, "unexpected recipe list"),
        ("/api/users/self/ratings", {"detail": "nope"}, "unexpected ratings list"),
    ],
)
async def test_get_favorite_recipes_rejects_malformed_responses(
    invoke, fetcher, favorites, url, response, message
):
    fetcher.responses["GET", url] = response
    with pytest.raises(ToolError, match=message):
        await invoke("get_favorite_recipes")


# --- household scope --------------------------------------------------------


async def test_household_favorites_merge_dedupe_count_and_sort(invoke, fetcher, household):
    stamppot, erwtensoep, hutspot, pannenkoeken = household
    out = await invoke("get_favorite_recipes", scope="household")
    assert [
        (item["name"], item["favoritedBy"], item["favoriteCount"])
        for item in out["items"]
    ] == [
        ("Stamppot", ["partner", "tester"], 2),
        ("Erwtensoep", ["partner"], 1),
        ("Hutspot", ["tester"], 1),
        ("Pannenkoeken", ["partner"], 1),
    ]
    assert (out["page"], out["perPage"], out["total"], out["totalPages"]) == (1, 50, 4, 1)
    assert set(out["items"][0]) == {
        "id", "name", "slug", "tags", "totalTime", "rating", "userRating",
        "household", "favoritedBy", "favoriteCount",
    }
    # One member lookup, then one favorites request per household member
    # (the kid included, the neighbour not).
    assert _calls(fetcher) == [
        ("GET", "/api/users/self"),
        ("GET", "/api/groups/members"),
        ("GET", "/api/recipes"),
        ("GET", "/api/recipes"),
        ("GET", "/api/recipes"),
        ("GET", "/api/users/self/ratings"),
        ("GET", "/api/groups/households"),
    ]
    assert fetcher.last("GET", "/api/groups/members")["params"] == {"perPage": -1}
    assert [r["params"] for r in _recipe_requests(fetcher)] == [
        {
            "queryFilter": f'favoritedBy.id = "{user_id}"',
            "perPage": -1,
            "orderDirection": "desc",
        }
        for user_id in (KID, PARTNER, ME)
    ]


async def test_household_favorites_member_without_favorites(invoke, fetcher, household):
    fetcher.member_favorites = {}
    fetcher.favorites = []
    out = await invoke("get_favorite_recipes", scope="household")
    assert out == {"page": 1, "perPage": 50, "total": 0, "totalPages": 0, "items": []}
    assert len(_recipe_requests(fetcher)) == 3
    assert ("GET", "/api/users/self/ratings") not in _calls(fetcher)


@pytest.mark.parametrize(
    ("page", "per_page", "names", "total_pages"),
    [
        (1, 2, ["Stamppot", "Erwtensoep"], 2),
        (2, 2, ["Hutspot", "Pannenkoeken"], 2),
        (3, 2, [], 2),
        (2, 3, ["Pannenkoeken"], 2),
    ],
)
async def test_household_favorites_paginate_after_merging(
    invoke, fetcher, household, page, per_page, names, total_pages
):
    out = await invoke(
        "get_favorite_recipes", scope="household", page=page, per_page=per_page
    )
    assert [item["name"] for item in out["items"]] == names
    assert (out["page"], out["perPage"], out["total"], out["totalPages"]) == (
        page, per_page, 4, total_pages,
    )


async def test_household_favorites_explicit_order_is_sorted_by_mealie(
    invoke, fetcher, household
):
    out = await invoke(
        "get_favorite_recipes", scope="household", order_by="name",
        order_direction="desc", page=1, per_page=3,
    )
    assert [
        (item["name"], item["favoritedBy"], item["favoriteCount"])
        for item in out["items"]
    ] == [
        ("Stamppot", ["partner", "tester"], 2),
        ("Pannenkoeken", ["partner"], 1),
        ("Hutspot", ["tester"], 1),
    ]
    assert (out["total"], out["totalPages"]) == (4, 2)
    union = _recipe_requests(fetcher)[-1]["params"]
    # Only members with favorites are in the merged filter.
    assert union == {
        "queryFilter": f'favoritedBy.id IN ["{PARTNER}", "{ME}"]',
        "page": 1,
        "perPage": 3,
        "orderBy": "name",
        "orderDirection": "desc",
        "orderByNullPosition": "last",
    }


async def test_household_favorites_rating_order_is_passed_through(
    invoke, fetcher, household
):
    await invoke("get_favorite_recipes", scope="household", order_by="rating")
    union = _recipe_requests(fetcher)[-1]["params"]
    assert (union["orderBy"], union["orderDirection"], union["orderByNullPosition"]) == (
        "rating", "desc", "last",
    )
    assert len(_recipe_requests(fetcher)) == 4


async def test_household_favorites_explicit_order_without_favorites(
    invoke, fetcher, household
):
    fetcher.member_favorites = {}
    fetcher.favorites = []
    out = await invoke("get_favorite_recipes", scope="household", order_by="rating")
    assert out["items"] == [] and out["total"] == 0
    assert len(_recipe_requests(fetcher)) == 3


async def test_household_favorites_keep_households_filter(invoke, fetcher, household):
    out = await invoke("get_favorite_recipes", scope="household", households=["self"])
    assert [item["name"] for item in out["items"]] == ["Stamppot", "Hutspot", "Pannenkoeken"]
    assert all(
        r["params"]["households"] == [OWN_HOUSEHOLD_ID] for r in _recipe_requests(fetcher)
    )


async def test_household_favorites_full_output(invoke, fetcher, household):
    fetcher.ratings = [_favorite(household[0]["id"], rating=4)]
    out = await invoke("get_favorite_recipes", scope="household", concise=False)
    first = out["items"][0]
    assert first["name"] == "Stamppot"
    assert (first["favoritedBy"], first["favoriteCount"], first["userRating"]) == (
        ["partner", "tester"], 2, 4,
    )
    assert "recipeCount" not in first["tags"][0]


async def test_household_favorites_require_own_household(invoke, fetcher, household):
    fetcher.user["householdId"] = None
    with pytest.raises(ToolError, match="did not report your household"):
        await invoke("get_favorite_recipes", scope="household")


async def test_household_favorites_reject_malformed_members(invoke, fetcher, household):
    fetcher.responses["GET", "/api/groups/members"] = {"detail": "nope"}
    with pytest.raises(ToolError, match="unexpected member list"):
        await invoke("get_favorite_recipes", scope="household")


async def test_household_favorites_reject_malformed_member_id(invoke, fetcher, household):
    fetcher.members[1]["id"] = '"] OR name IS NOT NULL OR id IN ["'
    with pytest.raises(ToolError, match="invalid user id"):
        await invoke("get_favorite_recipes", scope="household")


# --- mixins -----------------------------------------------------------------


def test_favorites_mixin_request(fetcher):
    fetcher.favorites = [_favorite("r1")]
    assert fetcher.get_current_user_favorites() == {"ratings": [_favorite("r1")]}
    assert fetcher.last() == {
        "method": "GET",
        "url": "/api/users/self/favorites",
        "json": None,
        "params": None,
        "files": None,
        "data": None,
    }


def test_ratings_mixin_request(fetcher):
    fetcher.ratings = [_favorite("r1", rating=4)]
    assert fetcher.get_current_user_ratings() == {"ratings": [_favorite("r1", rating=4)]}
    assert (fetcher.last()["method"], fetcher.last()["url"], fetcher.last()["params"]) == (
        "GET", "/api/users/self/ratings", None,
    )


def test_group_members_mixin_request(fetcher):
    assert fetcher.get_group_members()["items"][0]["username"] == "tester"
    assert (fetcher.last()["method"], fetcher.last()["url"], fetcher.last()["params"]) == (
        "GET", "/api/groups/members", {"perPage": -1},
    )
