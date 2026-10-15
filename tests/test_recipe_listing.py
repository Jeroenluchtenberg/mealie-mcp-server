"""Sorting, rating fields, nested recipeCount, and the shared pagination envelope."""

import uuid

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import OWN_HOUSEHOLD_ID

PAGE_KEYS = {"page", "perPage", "total", "totalPages", "items"}


def _recipe(name, **extra):
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, name)),
        "name": name,
        "slug": name.lower().replace(" ", "-"),
        "householdId": OWN_HOUSEHOLD_ID,
        "rating": None,
        "tags": [{"id": "t1", "name": "Soup", "slug": "soup", "recipeCount": 0}],
        "recipeCategory": [
            {"id": "c1", "name": "Dinner", "slug": "dinner", "recipeCount": 0}
        ],
        "tools": [
            {
                "id": "k1",
                "name": "Pan",
                "slug": "pan",
                "recipeCount": 0,
                "householdsWithTool": [],
            }
        ],
        **extra,
    }


@pytest.fixture
def catalogue(fetcher):
    recipes = [
        _recipe("Tomato Soup", rating=3.0),
        _recipe("Erwtensoep", description="a thick pea soup"),
        _recipe("Pasta Pesto", rating=2.5),
        _recipe("Onion Soup", rating=0),
    ]
    fetcher.recipe_catalogue = recipes
    return recipes


def _recipe_requests(fetcher):
    return [r["params"] for r in fetcher.requests if r["url"] == "/api/recipes"]


# --- sorting -----------------------------------------------------------------


async def test_get_recipes_sorts_by_name_by_default(invoke, fetcher, catalogue):
    out = await invoke("get_recipes")
    assert [item["name"] for item in out["items"]] == [
        "Erwtensoep", "Onion Soup", "Pasta Pesto", "Tomato Soup",
    ]
    assert _recipe_requests(fetcher) == [
        {"orderBy": "name", "orderDirection": "asc", "orderByNullPosition": "last"}
    ]


@pytest.mark.parametrize(
    ("arguments", "order"),
    [
        ({"order_by": "rating"}, ("rating", "desc")),
        ({"order_by": "date_added"}, ("date_added", "desc")),
        ({"order_by": "last_made"}, ("last_made", "desc")),
        ({"order_by": "name"}, ("name", "asc")),
        ({"order_by": "Rating", "order_direction": "ASC"}, ("rating", "asc")),
    ],
)
async def test_get_recipes_sorting_is_passed_to_mealie(
    invoke, fetcher, catalogue, arguments, order
):
    await invoke("get_recipes", per_page=10, **arguments)
    (params,) = _recipe_requests(fetcher)
    assert (params["orderBy"], params["orderDirection"]) == order
    assert params["orderByNullPosition"] == "last"
    assert params["perPage"] == 10


async def test_get_recipes_search_keeps_mealie_relevance_without_order(
    invoke, fetcher, catalogue
):
    await invoke("get_recipes", search="soup")
    (params,) = _recipe_requests(fetcher)
    assert params["search"] == "soup"
    assert "orderBy" not in params


async def test_get_recipes_explicit_order_wins_over_search_relevance(
    invoke, fetcher, catalogue
):
    """Mealie ranks search matches before orderBy, so the order comes from an
    unsearched, sorted listing restricted to the search matches."""
    out = await invoke(
        "get_recipes",
        search="soup",
        order_by="name",
        order_direction="desc",
        tags=["soup"],
        page=1,
        per_page=2,
    )
    assert _recipe_requests(fetcher) == [
        {"search": "soup", "orderDirection": "desc", "perPage": -1, "tags": ["soup"]},
        {
            "orderBy": "name",
            "orderDirection": "desc",
            "orderByNullPosition": "last",
            "perPage": -1,
            "tags": ["soup"],
        },
    ]
    assert [item["name"] for item in out["items"]] == ["Tomato Soup", "Onion Soup"]
    assert (out["page"], out["perPage"], out["total"], out["totalPages"]) == (1, 2, 2, 1)


async def test_get_recipes_search_with_order_and_no_matches(invoke, fetcher, catalogue):
    out = await invoke("get_recipes", search="curry", order_by="rating")
    assert out == {"page": 1, "perPage": 50, "total": 0, "totalPages": 0, "items": []}
    assert len(_recipe_requests(fetcher)) == 1


async def test_get_cookbook_recipes_search_with_order_uses_cookbook_twice(
    invoke, fetcher, catalogue
):
    await invoke(
        "get_cookbook_recipes", cookbook="soups", search="soup", order_by="rating"
    )
    assert [params.get("cookbook") for params in _recipe_requests(fetcher)] == [
        "soups", "soups",
    ]
    assert "search" not in _recipe_requests(fetcher)[1]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"order_by": "popularity"}, "Unknown order_by: popularity"),
        ({"order_by": "rating", "order_direction": "sideways"}, "Unknown order_direction"),
        ({"search": "soup", "order_direction": "up"}, "Unknown order_direction"),
    ],
)
async def test_get_recipes_rejects_bad_order(invoke, fetcher, arguments, message):
    with pytest.raises(ToolError, match=message):
        await invoke("get_recipes", **arguments)
    assert fetcher.requests == []


@pytest.mark.parametrize("tool", ["get_recipes", "get_favorite_recipes", "get_cookbook_recipes"])
async def test_sorting_docstrings_explain_rating_order(server, tool):
    mcp, _ = server
    description = next(t for t in await mcp.list_tools() if t.name == tool).description
    assert "your own rating where you set one, otherwise" in " ".join(description.split())
    assert "unrated recipes sort last" in " ".join(description.split())
    assert "userRating is the caller's own rating" in " ".join(description.split())


# --- rating fields -----------------------------------------------------------


async def test_get_recipes_concise_rating_and_user_rating(invoke, fetcher, catalogue):
    fetcher.ratings = [
        {"recipeId": catalogue[0]["id"].upper(), "rating": 5.0, "isFavorite": False},
        {"recipeId": catalogue[2]["id"], "rating": None, "isFavorite": True},
        {"recipeId": catalogue[3]["id"], "rating": 0, "isFavorite": False},
    ]
    out = await invoke("get_recipes")
    ratings = {item["name"]: (item["rating"], item["userRating"]) for item in out["items"]}
    assert ratings == {
        "Erwtensoep": (None, None),
        "Onion Soup": (None, None),
        "Pasta Pesto": (2.5, None),
        "Tomato Soup": (3.0, 5.0),
    }
    assert [r["url"] for r in fetcher.requests].count("/api/users/self/ratings") == 1


async def test_get_recipe_concise_rating_and_user_rating(invoke, fetcher):
    fetcher.recipe = {**fetcher.recipe, "rating": 0}
    fetcher.ratings = [
        {"recipeId": fetcher.recipe["id"], "rating": 4.0, "isFavorite": True}
    ]
    out = await invoke("get_recipe", slug="test-recipe", concise=True)
    assert (out["rating"], out["userRating"]) == (None, 4.0)
    fetcher.recipe["rating"] = 3.5
    fetcher.ratings = []
    out = await invoke("get_recipe", slug="test-recipe", concise=True)
    assert (out["rating"], out["userRating"]) == (3.5, None)


async def test_get_recipes_surfaces_ratings_failure(invoke, fetcher, catalogue):
    fetcher.responses["GET", "/api/users/self/ratings"] = {"detail": "nope"}
    with pytest.raises(ToolError, match="unexpected ratings list"):
        await invoke("get_recipes")


# --- recipeCount -------------------------------------------------------------


async def test_full_summaries_drop_nested_recipe_count(invoke, fetcher, catalogue):
    out = await invoke("get_recipes", concise=False)
    for item in out["items"]:
        for key in ("tags", "recipeCategory", "tools"):
            assert item[key] and all("recipeCount" not in org for org in item[key])
    pan = out["items"][0]["tools"][0]
    assert pan == {"id": "k1", "name": "Pan", "slug": "pan", "householdsWithTool": []}
    assert "recipeCount" in catalogue[0]["tags"][0]  # the source is untouched


@pytest.mark.parametrize(
    ("tool", "url"),
    [
        ("get_tags", "/api/organizers/tags"),
        ("get_categories", "/api/organizers/categories"),
        ("get_tools", "/api/organizers/tools"),
    ],
)
async def test_organizer_lists_keep_real_recipe_count(invoke, fetcher, tool, url):
    fetcher.responses["GET", url] = {
        "page": 1,
        "per_page": 50,
        "total": 1,
        "total_pages": 1,
        "items": [{"id": "t1", "name": "Soup", "slug": "soup", "recipeCount": 2}],
    }
    out = await invoke(tool)
    assert out["items"][0]["recipeCount"] == 2


# --- pagination envelope -----------------------------------------------------

MEALIE_PAGE = {
    "page": 2,
    "per_page": 1,
    "total": 3,
    "total_pages": 3,
    "next": "/api/x?page=3&perPage=1",
    "previous": "/api/x?page=1&perPage=1",
    "items": [],
}


@pytest.mark.parametrize(
    ("tool", "url", "arguments"),
    [
        ("get_recipes", "/api/recipes", {}),
        ("get_recipes", "/api/recipes", {"concise": False}),
        ("get_cookbook_recipes", "/api/recipes", {"cookbook": "soups"}),
        ("get_categories", "/api/organizers/categories", {}),
        ("get_tags", "/api/organizers/tags", {}),
        ("get_tools", "/api/organizers/tools", {}),
        ("get_foods", "/api/foods", {}),
        ("get_units", "/api/units", {}),
        ("get_all_mealplans", "/api/households/mealplans", {}),
        ("get_shopping_lists", "/api/households/shopping/lists", {}),
        ("get_shopping_list_items", "/api/households/shopping/items", {}),
    ],
)
async def test_paginated_tools_share_one_envelope(invoke, fetcher, tool, url, arguments):
    fetcher.responses["GET", url] = MEALIE_PAGE
    out = await invoke(tool, **arguments)
    assert out == {"page": 2, "perPage": 1, "total": 3, "totalPages": 3, "items": []}


@pytest.mark.parametrize("scope", ["user", "household"])
async def test_favorites_share_the_envelope(invoke, fetcher, scope):
    fetcher.recipe_catalogue = []
    out = await invoke("get_favorite_recipes", scope=scope)
    assert set(out) == PAGE_KEYS


async def test_camel_case_pages_are_accepted(invoke, fetcher):
    fetcher.responses["GET", "/api/organizers/tags"] = {
        "page": 1, "perPage": 50, "total": 0, "totalPages": 0, "items": [],
    }
    out = await invoke("get_tags")
    assert out == {"page": 1, "perPage": 50, "total": 0, "totalPages": 0, "items": []}


async def test_malformed_page_is_rejected(invoke, fetcher):
    fetcher.responses["GET", "/api/organizers/tags"] = {"detail": "nope"}
    with pytest.raises(ToolError, match="unexpected page"):
        await invoke("get_tags")
