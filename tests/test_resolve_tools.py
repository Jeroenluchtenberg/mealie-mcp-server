"""resolve_ingredients: candidate search, guarded alias/create writes, 403s."""

import logging

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from mealie.client import MealieApiError

CELERY = "c0000000-0000-4000-8000-000000000001"
CELERIAC = "c0000000-0000-4000-8000-000000000002"
SALT = "c0000000-0000-4000-8000-000000000003"
BAY_LEAF = "c0000000-0000-4000-8000-000000000004"


def food(food_id, name, aliases=(), plural=None):
    return {
        "id": food_id,
        "name": name,
        "pluralName": plural,
        "description": "",
        "labelId": "label-1",
        "aliases": [{"name": a} for a in aliases],
    }


@pytest.fixture
def catalogue(fetcher):
    fetcher.foods = [
        food(CELERY, "bleekselderij"),
        food(CELERIAC, "knolselderij"),
        food(SALT, "zout"),
        food(BAY_LEAF, "laurierblad", plural="laurierbladen"),
    ]
    return fetcher


def writes(fetcher):
    return [r for r in fetcher.requests if r["method"] in ("POST", "PUT")]


async def test_tool_is_registered_with_documented_schema(server):
    mcp, _ = server
    tool = next(t for t in await mcp.list_tools() if t.name == "resolve_ingredients")
    assert tool.inputSchema["required"] == ["items"]
    assert "can organize group data" in " ".join(tool.description.split())


@pytest.mark.parametrize(
    "text,suggestion",
    [
        (
            "stengel bleekselderij",
            {
                "action": "use_with_note",
                "food": {"id": CELERY, "name": "bleekselderij"},
                "note": "stengel",
            },
        ),
        (
            "Zout naar smaak",
            {
                "action": "use_with_note",
                "food": {"id": SALT, "name": "zout"},
                "note": "naar smaak",
            },
        ),
        ("ZOUT", {"action": "use", "food": {"id": SALT, "name": "zout"}}),
        # plural names count as exact matches
        ("laurierbladen", {"action": "use", "food": {"id": BAY_LEAF, "name": "laurierblad"}}),
        (
            "laurierbladeren",
            {
                "action": "alias",
                "food": {"id": BAY_LEAF, "name": "laurierblad"},
                "alias": "laurierbladeren",
            },
        ),
        ("pandanblad", {"action": "create", "name": "pandanblad"}),
    ],
)
async def test_suggest_ranks_candidates_without_writing(invoke, catalogue, text, suggestion):
    out = await invoke("resolve_ingredients", items=[{"text": text}])
    [result] = out["results"]
    assert result["status"] == "suggested"
    assert result["suggestion"] == suggestion
    if suggestion["action"] != "create":
        assert result["candidates"][0]["id"] == suggestion["food"]["id"]
    assert writes(catalogue) == []


async def test_catalogue_is_loaded_once_per_call(invoke, catalogue):
    await invoke(
        "resolve_ingredients",
        items=[{"text": "stengel bleekselderij"}, {"text": "zout naar smaak"}],
    )
    assert [(r["method"], r["url"], r["params"]) for r in catalogue.requests] == [
        ("GET", "/api/foods", {"perPage": -1})
    ]


async def test_writes_are_visible_to_later_items(invoke, catalogue):
    out = await invoke(
        "resolve_ingredients",
        items=[
            {"text": "pandanblad", "action": "create"},
            {"text": "Pandanblad", "action": "create"},
            {"text": "grof zout", "action": "alias", "food_id": SALT},
            {"text": "grof zout", "action": "alias", "food_id": CELERY},
            {"text": "grof zout"},
        ],
    )
    assert [r["status"] for r in out["results"]] == [
        "created", "exists", "aliased", "conflict", "suggested",
    ]
    assert out["results"][4]["suggestion"]["action"] == "use"
    assert len(writes(catalogue)) == 2


async def test_suggest_does_not_offer_a_different_ingredient_as_alias(invoke, catalogue):
    out = await invoke("resolve_ingredients", items=[{"text": "selderij"}])
    [result] = out["results"]
    # knolselderij/bleekselderij are similar strings but different ingredients:
    # they are listed as candidates, never suggested as an alias.
    assert {c["id"] for c in result["candidates"]} >= {CELERY, CELERIAC}
    assert result["suggestion"] == {"action": "create", "name": "selderij"}


async def test_alias_appends_to_existing_food(invoke, catalogue):
    out = await invoke(
        "resolve_ingredients",
        items=[{"text": "laurierbladeren", "action": "alias", "food_id": BAY_LEAF}],
    )
    assert out["results"] == [
        {
            "index": 0,
            "text": "laurierbladeren",
            "action": "alias",
            "status": "aliased",
            "food": {"id": BAY_LEAF, "name": "laurierblad"},
            "alias": "laurierbladeren",
        }
    ]
    [put] = writes(catalogue)
    assert put["method"] == "PUT"
    assert put["url"] == f"/api/foods/{BAY_LEAF}"
    # full record preserved, alias appended
    assert put["json"]["labelId"] == "label-1"
    assert put["json"]["pluralName"] == "laurierbladen"
    assert put["json"]["aliases"] == [{"name": "laurierbladeren"}]


async def test_alias_uses_explicit_alias_text(invoke, catalogue):
    out = await invoke(
        "resolve_ingredients",
        items=[
            {"text": "stengels bleekselderij", "action": "alias",
             "food_id": CELERY, "alias": "selderijstengel"}
        ],
    )
    assert out["results"][0]["alias"] == "selderijstengel"
    assert writes(catalogue)[0]["json"]["aliases"] == [{"name": "selderijstengel"}]


async def test_existing_alias_is_unchanged(invoke, catalogue):
    catalogue.foods[2]["aliases"] = [{"name": "keukenzout"}]
    out = await invoke(
        "resolve_ingredients",
        items=[{"text": "Keukenzout", "action": "alias", "food_id": SALT}],
    )
    assert out["results"][0]["status"] == "unchanged"
    assert writes(catalogue) == []


async def test_alias_owned_by_another_food_is_a_conflict(invoke, catalogue):
    out = await invoke(
        "resolve_ingredients",
        items=[{"text": "knolselderij", "action": "alias", "food_id": CELERY}],
    )
    [result] = out["results"]
    assert result["status"] == "conflict"
    assert result["food"] == {"id": CELERIAC, "name": "knolselderij"}
    assert writes(catalogue) == []


async def test_create_makes_new_food(invoke, catalogue):
    out = await invoke(
        "resolve_ingredients",
        items=[{"text": "pandan", "action": "create", "name": "pandanblad"}],
    )
    [result] = out["results"]
    assert result["status"] == "created"
    assert result["food"]["name"] == "pandanblad"
    [post] = writes(catalogue)
    assert (post["method"], post["url"], post["json"]) == (
        "POST", "/api/foods", {"name": "pandanblad"}
    )


@pytest.mark.parametrize("text", ["Zout", "laurierbladen"])
async def test_create_returns_existing_food_instead_of_duplicate(invoke, catalogue, text):
    catalogue.foods[2]["aliases"] = [{"name": "keukenzout"}]
    out = await invoke("resolve_ingredients", items=[{"text": text, "action": "create"}])
    assert out["results"][0]["status"] == "exists"
    assert writes(catalogue) == []


async def test_forbidden_writes_explain_permission_and_skip_the_rest(invoke, catalogue):
    catalogue.fail_on("/api/foods/" + BAY_LEAF, 403)
    out = await invoke(
        "resolve_ingredients",
        items=[
            {"text": "laurierbladeren", "action": "alias", "food_id": BAY_LEAF},
            {"text": "pandanblad", "action": "create"},
            {"text": "zout naar smaak"},
        ],
    )
    first, second, third = out["results"]
    assert first["status"] == "error"
    assert "can organize group data" in first["error"]
    assert second["status"] == "skipped"
    assert "can organize group data" in second["error"]
    # read-only suggestions still run
    assert third["status"] == "suggested"
    assert writes(catalogue) == []


async def test_create_forbidden_by_mealie(invoke, catalogue, monkeypatch):
    def forbidden(*args, **kwargs):
        raise MealieApiError(403, "private-detail")

    monkeypatch.setattr(catalogue, "create_food", forbidden)
    out = await invoke("resolve_ingredients", items=[{"text": "pandanblad", "action": "create"}])
    assert "can organize group data" in out["results"][0]["error"]
    assert "private-detail" not in str(out)


async def test_other_failures_keep_earlier_progress(invoke, catalogue):
    catalogue.fail_on("/api/foods/" + SALT, 500)
    out = await invoke(
        "resolve_ingredients",
        items=[
            {"text": "pandanblad", "action": "create"},
            {"text": "grof zout", "action": "alias", "food_id": SALT},
            {"text": "laurierbladeren", "action": "alias", "food_id": BAY_LEAF},
        ],
    )
    statuses = [r["status"] for r in out["results"]]
    assert statuses == ["created", "error", "aliased"]
    assert out["results"][1]["error"] == "Mealie API request failed (HTTP 500)"


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"text": "  "}],
        [{"text": "zout", "action": "alias"}],
        [{"text": "zout", "action": "alias", "food_id": " "}],
        [{"text": "zout", "action": "create", "name": ""}],
        [{"text": "zout", "action": "merge"}],
        [{"text": "zout"}] * 51,
    ],
)
async def test_invalid_items_fail_before_requests(invoke, catalogue, items):
    with pytest.raises(ToolError, match="Error"):
        await invoke("resolve_ingredients", items=items)
    assert catalogue.requests == []


async def test_catalogue_failure_stops_before_writes(invoke, catalogue, caplog):
    caplog.set_level(logging.DEBUG, logger="mealie-mcp")
    catalogue.fail_on("/api/foods", 500)
    with pytest.raises(ToolError, match="HTTP 500"):
        await invoke(
            "resolve_ingredients",
            items=[{"text": "private-food-text", "action": "create"}],
        )
    assert writes(catalogue) == []
    assert "private-food-text" not in caplog.text


async def test_malformed_catalogue_is_rejected(invoke, catalogue):
    catalogue.responses["GET", "/api/foods"] = {"detail": "unexpected"}
    with pytest.raises(ToolError, match="unexpected food list"):
        await invoke("resolve_ingredients", items=[{"text": "zout"}])


async def test_write_failure_logs_exclude_food_text(invoke, catalogue, caplog):
    caplog.set_level(logging.DEBUG, logger="mealie-mcp")
    catalogue.fail_on("/api/foods/" + SALT, 500)
    out = await invoke(
        "resolve_ingredients",
        items=[{"text": "private-food-text", "action": "alias", "food_id": SALT}],
    )
    assert out["results"][0]["status"] == "error"
    assert "private-food-text" not in caplog.text


def test_add_food_alias_rejects_invalid_input(fetcher):
    with pytest.raises(ValueError):
        fetcher.add_food_alias("", "alias")
    with pytest.raises(ValueError):
        fetcher.add_food_alias(SALT, "  ")
    assert fetcher.requests == []


def test_add_food_alias_rejects_incomplete_record(fetcher):
    fetcher.foods = [{"id": SALT, "name": ""}]
    with pytest.raises(ValueError):
        fetcher.add_food_alias(SALT, "keukenzout")
    assert [r["method"] for r in fetcher.requests] == ["GET"]
