"""Offline coverage for the cookbook tools."""

from copy import deepcopy

import pytest
from conftest import OTHER_HOUSEHOLD_ID, OWN_HOUSEHOLD_ID
from mcp.server.fastmcp.exceptions import ToolError

COOKBOOKS = "/api/households/cookbooks"
RECIPES = "/api/recipes"
GROUP_ID = "5f3a1b2c-3d4e-4f5a-8b6c-7d8e9f0a1b03"
WEEKNIGHT_ID = "4d2c8b1a-6e5f-4a3b-9c8d-7e6f5a4b3c11"


def _cookbook(cookbook_id, name, slug, position, household_id=OWN_HOUSEHOLD_ID):
    """A ReadCookBook as Mealie returns it."""
    return {
        "name": name,
        "description": f"{name} recipes",
        "slug": slug,
        "position": position,
        "public": False,
        "queryFilterString": 'tags.slug IN ["quick"]',
        "groupId": GROUP_ID,
        "householdId": household_id,
        "id": cookbook_id,
        "queryFilter": {"parts": []},
    }


# Out of position order, with another household's cookbook mixed in, since
# Mealie returns every cookbook in the group.
COOKBOOK_ITEMS = [
    _cookbook("4d2c8b1a-6e5f-4a3b-9c8d-7e6f5a4b3c12", "Soups", "soups", 3),
    _cookbook(WEEKNIGHT_ID, "Weeknight Dinners", "weeknight-dinners", 1),
    _cookbook(
        "4d2c8b1a-6e5f-4a3b-9c8d-7e6f5a4b3c13",
        "Neighbour Bakes",
        "neighbour-bakes",
        2,
        household_id=OTHER_HOUSEHOLD_ID,
    ),
    _cookbook("4d2c8b1a-6e5f-4a3b-9c8d-7e6f5a4b3c14", "Desserts", "desserts", 2),
]

COOKBOOK_PAGE = {
    "page": 1,
    "perPage": -1,
    "total": len(COOKBOOK_ITEMS),
    "totalPages": 1,
    "next": None,
    "previous": None,
    "items": COOKBOOK_ITEMS,
}

RECIPE_PAGE = {
    "page": 2,
    "perPage": 1,
    "total": 2,
    "totalPages": 2,
    "next": None,
    "previous": "/api/recipes?page=1&perPage=1&cookbook=weeknight-dinners",
    "items": [
        {
            "id": "r1",
            "name": "Stamppot",
            "slug": "stamppot",
            "householdId": OWN_HOUSEHOLD_ID,
            "totalTime": "45 min",
            "tags": [{"id": "t1", "name": "Quick", "slug": "quick"}],
            "recipeCategory": [],
            "tools": [],
        }
    ],
}


@pytest.fixture(autouse=True)
def cookbook_responses(fetcher):
    fetcher.responses["GET", COOKBOOKS] = deepcopy(COOKBOOK_PAGE)
    fetcher.responses["GET", RECIPES] = deepcopy(RECIPE_PAGE)


def _calls(fetcher):
    return [(r["method"], r["url"]) for r in fetcher.requests]


async def test_cookbook_tools_are_registered(server):
    mcp, _ = server
    names = {tool.name for tool in await mcp.list_tools()}
    assert {"get_cookbooks", "get_cookbook_recipes"} <= names


async def test_get_cookbooks_is_concise_and_ordered_by_position(invoke, fetcher):
    out = await invoke("get_cookbooks")
    assert out == [
        {
            "id": WEEKNIGHT_ID,
            "name": "Weeknight Dinners",
            "slug": "weeknight-dinners",
            "description": "Weeknight Dinners recipes",
            "position": 1,
            "public": False,
        },
        {
            "id": "4d2c8b1a-6e5f-4a3b-9c8d-7e6f5a4b3c14",
            "name": "Desserts",
            "slug": "desserts",
            "description": "Desserts recipes",
            "position": 2,
            "public": False,
        },
        {
            "id": "4d2c8b1a-6e5f-4a3b-9c8d-7e6f5a4b3c12",
            "name": "Soups",
            "slug": "soups",
            "description": "Soups recipes",
            "position": 3,
            "public": False,
        },
    ]
    assert fetcher.last("GET", url_contains=COOKBOOKS)["params"] == {
        "perPage": -1,
        "orderBy": "position",
        "orderDirection": "asc",
    }


async def test_get_cookbooks_full_output_keeps_query_filter(invoke):
    out = await invoke("get_cookbooks", concise=False)
    assert [c["slug"] for c in out] == ["weeknight-dinners", "desserts", "soups"]
    assert out[0] == COOKBOOK_ITEMS[1]
    assert out[0]["queryFilterString"] == 'tags.slug IN ["quick"]'


async def test_get_cookbooks_excludes_other_households(invoke, fetcher):
    fetcher.user["householdId"] = OTHER_HOUSEHOLD_ID.upper()
    out = await invoke("get_cookbooks")
    assert [c["slug"] for c in out] == ["neighbour-bakes"]


async def test_get_cookbooks_empty(invoke, fetcher):
    fetcher.responses["GET", COOKBOOKS] = {**COOKBOOK_PAGE, "items": [], "total": 0}
    assert await invoke("get_cookbooks") == []
    assert await invoke("get_cookbooks", concise=False) == []


async def test_get_cookbooks_rejects_malformed_list(invoke, fetcher):
    fetcher.responses["GET", COOKBOOKS] = {"detail": "nope"}
    with pytest.raises(ToolError, match="unexpected cookbook list"):
        await invoke("get_cookbooks")


async def test_get_cookbooks_requires_own_household(invoke, fetcher):
    fetcher.user["householdId"] = None
    with pytest.raises(ToolError, match="did not report your household"):
        await invoke("get_cookbooks")
    assert ("GET", COOKBOOKS) not in _calls(fetcher)


@pytest.mark.parametrize("url", ["/api/users/self", COOKBOOKS])
async def test_get_cookbooks_surfaces_failures(invoke, fetcher, url):
    fetcher.fail_on(url, 500)
    with pytest.raises(ToolError, match="Error fetching cookbooks"):
        await invoke("get_cookbooks")


async def test_get_cookbooks_rejects_invalid_arguments(invoke):
    with pytest.raises(ToolError):
        await invoke("get_cookbooks", concise="sometimes")


@pytest.mark.parametrize("cookbook", ["weeknight-dinners", WEEKNIGHT_ID])
async def test_get_cookbook_recipes_by_slug_or_id(invoke, fetcher, cookbook):
    out = await invoke("get_cookbook_recipes", cookbook=f" {cookbook} ")
    assert fetcher.last("GET", url_contains=RECIPES)["params"] == {
        "cookbook": cookbook,
        "orderDirection": "desc",
    }
    assert out == {
        "page": 2,
        "perPage": 1,
        "total": 2,
        "totalPages": 2,
        "items": [
            {
                "id": "r1",
                "name": "Stamppot",
                "slug": "stamppot",
                "tags": ["Quick"],
                "totalTime": "45 min",
                "household": "luchtenberg",
            }
        ],
    }
    assert _calls(fetcher) == [("GET", RECIPES), ("GET", "/api/groups/households")]


async def test_get_cookbook_recipes_matches_get_recipes_shape(invoke, fetcher):
    from_cookbook = await invoke("get_cookbook_recipes", cookbook="soups")
    assert from_cookbook == await invoke("get_recipes")
    full = await invoke("get_cookbook_recipes", cookbook="soups", concise=False)
    assert full == RECIPE_PAGE == await invoke("get_recipes", concise=False)


async def test_get_cookbook_recipes_combines_search(invoke, fetcher):
    await invoke(
        "get_cookbook_recipes",
        cookbook="weeknight-dinners",
        search="pasta",
        page=1,
        per_page=20,
    )
    assert fetcher.last("GET", url_contains=RECIPES)["params"] == {
        "cookbook": "weeknight-dinners",
        "search": "pasta",
        "page": 1,
        "perPage": 20,
        "orderDirection": "desc",
    }


@pytest.mark.parametrize("page,per_page", [(1, 1), (2, 1), (1, -1)])
async def test_get_cookbook_recipes_pagination_is_passed_through(
    invoke, fetcher, page, per_page
):
    out = await invoke(
        "get_cookbook_recipes", cookbook="soups", page=page, per_page=per_page
    )
    params = fetcher.last("GET", url_contains=RECIPES)["params"]
    assert (params["page"], params["perPage"]) == (page, per_page)
    assert set(out) == {"page", "perPage", "total", "totalPages", "items"}


async def test_get_cookbook_recipes_page_past_end_is_empty(invoke, fetcher):
    fetcher.responses["GET", RECIPES] = {
        "page": 3,
        "perPage": 1,
        "total": 2,
        "totalPages": 2,
        "next": None,
        "previous": None,
        "items": [],
    }
    out = await invoke("get_cookbook_recipes", cookbook="soups", page=3, per_page=1)
    assert out == {"page": 3, "perPage": 1, "total": 2, "totalPages": 2, "items": []}
    assert _calls(fetcher) == [("GET", RECIPES)]


async def test_get_cookbook_recipes_unknown_cookbook(invoke, fetcher):
    fetcher.fail_on(RECIPES, 404, "cookbook not found")
    with pytest.raises(ToolError) as error:
        await invoke("get_cookbook_recipes", cookbook="nope")
    assert str(error.value).endswith(
        "Error fetching cookbook recipes: Unknown cookbook: nope. "
        "Available cookbooks: weeknight-dinners, desserts, soups"
    )


async def test_get_cookbook_recipes_unknown_cookbook_without_any(invoke, fetcher):
    fetcher.fail_on(RECIPES, 404, "cookbook not found")
    fetcher.responses["GET", COOKBOOKS] = {**COOKBOOK_PAGE, "items": []}
    with pytest.raises(ToolError, match="Unknown cookbook: nope. Available cookbooks: none"):
        await invoke("get_cookbook_recipes", cookbook="nope")


@pytest.mark.parametrize("cookbook", ["", "   "])
async def test_get_cookbook_recipes_rejects_empty_cookbook(invoke, fetcher, cookbook):
    with pytest.raises(ToolError, match="cannot be empty"):
        await invoke("get_cookbook_recipes", cookbook=cookbook)
    assert fetcher.requests == []


async def test_get_cookbook_recipes_surfaces_other_failures(invoke, fetcher):
    fetcher.fail_on(RECIPES, 500)
    with pytest.raises(ToolError) as error:
        await invoke("get_cookbook_recipes", cookbook="soups")
    assert "HTTP 500" in str(error.value)
    assert "Unknown cookbook" not in str(error.value)
    assert ("GET", COOKBOOKS) not in _calls(fetcher)


async def test_get_cookbook_recipes_rejects_invalid_arguments(invoke):
    with pytest.raises(ToolError):
        await invoke("get_cookbook_recipes")
    with pytest.raises(ToolError):
        await invoke("get_cookbook_recipes", cookbook="soups", concise="sometimes")
