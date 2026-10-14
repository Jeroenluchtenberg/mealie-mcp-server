"""Tests for get_favorite_recipes: favorites resolution, pagination, households."""

import uuid

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from conftest import OTHER_HOUSEHOLD_ID, OWN_HOUSEHOLD_ID


def _recipe(name, household_id=OWN_HOUSEHOLD_ID, **extra):
    recipe_id = str(uuid.uuid5(uuid.NAMESPACE_URL, name))
    return {
        "id": recipe_id,
        "name": name,
        "slug": name.lower(),
        "householdId": household_id,
        "totalTime": "30 min",
        "tags": [{"id": "t1", "name": "Quick", "slug": "quick"}],
        "recipeCategory": [{"id": "c1", "name": "Dinner", "slug": "dinner"}],
        "tools": [],
        **extra,
    }


def _favorite(recipe_id, rating=None):
    return {"recipeId": recipe_id, "rating": rating, "isFavorite": True}


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


def _recipe_requests(fetcher):
    return [r for r in fetcher.requests if r["url"] == "/api/recipes"]


async def test_get_favorite_recipes_is_registered_read_only(server):
    mcp, _ = server
    tool = next(t for t in await mcp.list_tools() if t.name == "get_favorite_recipes")
    assert set(tool.inputSchema["properties"]) == {
        "page", "per_page", "households", "concise",
    }
    assert "required" not in tool.inputSchema
    assert "Read-only" in tool.description
    assert "my favorites" in tool.description


async def test_get_favorite_recipes_concise_by_default(invoke, fetcher, favorites):
    out = await invoke("get_favorite_recipes")
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
                "tags": ["Quick"],
                "totalTime": "30 min",
                "household": "neighbours",
            },
            {
                "id": favorites[2]["id"],
                "name": "Hutspot",
                "slug": "hutspot",
                "tags": ["Quick"],
                "totalTime": "30 min",
                "household": "luchtenberg",
            },
            {
                "id": favorites[0]["id"],
                "name": "Stamppot",
                "slug": "stamppot",
                "tags": ["Quick"],
                "totalTime": "30 min",
                "household": "luchtenberg",
            },
        ],
    }
    assert [(r["method"], r["url"]) for r in fetcher.requests] == [
        ("GET", "/api/users/self/favorites"),
        ("GET", "/api/recipes"),
        ("GET", "/api/groups/households"),
    ]
    params = _recipe_requests(fetcher)[0]["params"]
    ids = ", ".join(f'"{r["id"]}"' for r in favorites[:3])
    assert params["queryFilter"] == f"id IN [{ids}]"
    assert params["perPage"] == -1
    assert "households" not in params
    assert "page" not in params


async def test_get_favorite_recipes_full_summaries(invoke, fetcher, favorites):
    out = await invoke("get_favorite_recipes", concise=False)
    assert [item["name"] for item in out["items"]] == [
        "Erwtensoep", "Hutspot", "Stamppot",
    ]
    assert out["items"][0] == favorites[1]
    assert out["total"] == 3
    assert ("GET", "/api/groups/households") not in [
        (r["method"], r["url"]) for r in fetcher.requests
    ]


async def test_get_favorite_recipes_empty(invoke, fetcher, favorites):
    fetcher.favorites = []
    out = await invoke("get_favorite_recipes")
    assert out == {"page": 1, "perPage": 50, "total": 0, "totalPages": 0, "items": []}
    assert [r["url"] for r in fetcher.requests] == ["/api/users/self/favorites"]


async def test_get_favorite_recipes_skips_deleted_recipe(invoke, fetcher, favorites):
    deleted = str(uuid.uuid4())
    fetcher.favorites.insert(1, _favorite(deleted))
    out = await invoke("get_favorite_recipes")
    assert out["total"] == 3
    assert deleted not in [item["id"] for item in out["items"]]
    assert deleted in _recipe_requests(fetcher)[0]["params"]["queryFilter"]


async def test_get_favorite_recipes_ignores_malformed_and_duplicate_entries(
    invoke, fetcher, favorites
):
    fetcher.favorites += [
        _favorite(favorites[0]["id"]),
        {"recipeId": '"] OR name IS NOT NULL OR id IN ["', "isFavorite": True},
        {"recipeId": None},
        {**_favorite(favorites[3]["id"]), "isFavorite": False},
        "junk",
    ]
    out = await invoke("get_favorite_recipes")
    assert [item["name"] for item in out["items"]] == [
        "Erwtensoep", "Hutspot", "Stamppot",
    ]
    query_filter = _recipe_requests(fetcher)[0]["params"]["queryFilter"]
    assert query_filter.count('"') == 6
    assert "OR" not in query_filter


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


async def test_get_favorite_recipes_chunks_many_favorites(invoke, fetcher):
    recipes = [_recipe(f"Recipe {index:03}") for index in range(120)]
    fetcher.recipe_catalogue = recipes
    fetcher.favorites = [_favorite(r["id"]) for r in reversed(recipes)]
    out = await invoke("get_favorite_recipes", page=3, per_page=50, concise=False)
    filters = [r["params"]["queryFilter"] for r in _recipe_requests(fetcher)]
    assert [f.count('"') // 2 for f in filters] == [50, 50, 20]
    assert out["total"] == 120
    assert out["totalPages"] == 3
    assert [item["name"] for item in out["items"]] == [
        f"Recipe {index:03}" for index in range(100, 120)
    ]


async def test_get_favorite_recipes_households_self(invoke, fetcher, favorites):
    out = await invoke("get_favorite_recipes", households=["self"])
    assert [item["name"] for item in out["items"]] == ["Hutspot", "Stamppot"]
    assert out["total"] == 2
    assert _recipe_requests(fetcher)[0]["params"]["households"] == [OWN_HOUSEHOLD_ID]
    calls = [r["url"] for r in fetcher.requests]
    assert calls.count("/api/groups/households") == 1


async def test_get_favorite_recipes_households_slug(invoke, fetcher, favorites):
    out = await invoke("get_favorite_recipes", households=["neighbours"])
    assert [item["household"] for item in out["items"]] == ["neighbours"]


async def test_get_favorite_recipes_unknown_household_errors(invoke, fetcher, favorites):
    with pytest.raises(ToolError, match="Unknown household\\(s\\): nobody"):
        await invoke("get_favorite_recipes", households=["nobody"])
    assert "/api/users/self/favorites" not in [r["url"] for r in fetcher.requests]


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


@pytest.mark.parametrize("url", ["/api/users/self/favorites", "/api/recipes"])
async def test_get_favorite_recipes_surfaces_failures(invoke, fetcher, favorites, url):
    fetcher.fail_on(url, 500)
    with pytest.raises(ToolError, match="Error fetching favorite recipes"):
        await invoke("get_favorite_recipes")


async def test_get_favorite_recipes_reports_rejected_token(invoke, fetcher, favorites):
    fetcher.fail_on("/api/users/self/favorites", 401)
    with pytest.raises(ToolError, match="HTTP 401"):
        await invoke("get_favorite_recipes")


@pytest.mark.parametrize(
    ("url", "response", "message"),
    [
        ("/api/users/self/favorites", {"detail": "nope"}, "unexpected favorites list"),
        ("/api/recipes", {"detail": "nope"}, "unexpected recipe list"),
    ],
)
async def test_get_favorite_recipes_rejects_malformed_responses(
    invoke, fetcher, favorites, url, response, message
):
    fetcher.responses["GET", url] = response
    with pytest.raises(ToolError, match=message):
        await invoke("get_favorite_recipes")


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
