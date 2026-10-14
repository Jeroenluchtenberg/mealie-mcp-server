"""Tests for ingredient parsing (single-line lists, batches, flattening, verbose)."""

import logging

import pytest
from mcp.server.fastmcp.exceptions import ToolError

FOOD_ID = "a0819c33-1a5e-4374-9151-ed85160c0049"


async def test_parse_ingredient_posts_to_parser_endpoint(invoke, fetcher):
    await invoke("parse_ingredients", ingredients=["1/4 cup chopped onion"])

    req = fetcher.last("POST", "/api/parser/ingredients")
    assert req["url"] == "/api/parser/ingredients"
    assert req["json"] == {"ingredients": ["1/4 cup chopped onion"], "parser": "nlp"}


async def test_parse_ingredient_flattens_to_recipe_ingredient_shape(invoke, fetcher):
    results = await invoke("parse_ingredients", ingredients=["1/4 cup chopped onion"])
    assert len(results) == 1
    out = results[0]

    # exactly the fields create_recipe accepts, plus input/confidence
    assert out == {
        "input": "1/4 cup chopped onion",
        "confidence": 0.996,
        "quantity": 0.25,
        "unit": None,  # unmatched vocabulary stays visible as null
        "food": {"id": FOOD_ID, "name": "onion"},
        "note": "chopped",
    }


async def test_parse_ingredient_verbose_returns_untouched_response(invoke, fetcher):
    out = (await invoke(
        "parse_ingredients", ingredients=["1/4 cup chopped onion"], verbose=True
    ))[0]

    assert out["ingredient"]["food"]["pluralName"] == "onions"
    assert out["confidence"]["quantity"] == 1.0


async def test_parse_ingredient_forwards_parser_choice(invoke, fetcher):
    await invoke("parse_ingredients", ingredients=["2 eggs"], parser="brute")

    assert fetcher.last("POST", "/api/parser/ingredients")["json"]["parser"] == "brute"


async def test_parse_ingredient_rejects_unknown_parser(invoke, fetcher):
    with pytest.raises(ToolError, match="parser must be one of"):
        await invoke("parse_ingredients", ingredients=["2 eggs"], parser="magic")

    assert fetcher.requests == []


@pytest.mark.parametrize("ingredients", [[""], [" \t"], ["2 eggs", ""]])
async def test_parse_ingredient_rejects_empty_input(invoke, fetcher, ingredients):
    with pytest.raises(ToolError, match="Ingredient cannot be empty"):
        await invoke("parse_ingredients", ingredients=ingredients)

    assert fetcher.requests == []


async def test_parse_ingredients_batches_in_one_request(invoke, fetcher):
    lines = ["1/4 cup chopped onion", "2 large eggs", "a pinch of salt"]
    out = await invoke("parse_ingredients", ingredients=lines)

    assert len(fetcher.requests) == 1
    assert fetcher.last("POST", "/api/parser/ingredients")["json"] == {
        "ingredients": lines,
        "parser": "nlp",
    }
    # one result per line, in input order
    assert [r["input"] for r in out] == lines
    assert out[0]["food"] == {"id": FOOD_ID, "name": "onion"}


async def test_parse_ingredients_verbose_returns_untouched_responses(invoke, fetcher):
    out = await invoke("parse_ingredients", ingredients=["2 eggs"], verbose=True)

    assert out[0]["ingredient"]["referenceId"] == "75e1853f-3cb1-49b9-b2ca-26ae76da256b"


async def test_parse_ingredients_rejects_empty_list(invoke, fetcher):
    with pytest.raises(ToolError, match="Ingredients cannot be empty"):
        await invoke("parse_ingredients", ingredients=[])

    assert fetcher.last("POST", "/api/parser/ingredients") is None


async def test_parsed_result_feeds_create_recipe(invoke, fetcher):
    """The parser output must be usable as a structured ingredient verbatim."""
    parsed = await invoke("parse_ingredients", ingredients=["1/4 cup chopped onion"])

    await invoke("create_recipe", name="Parsed", ingredients=parsed, instructions=[])

    written = fetcher.last("PUT", "/api/recipes/")["json"]["recipeIngredient"][0]
    assert written["quantity"] == 0.25
    assert written["food"]["id"] == FOOD_ID
    assert written["note"] == "chopped"


async def test_parse_ingredients_surfaces_client_failure(invoke, fetcher):
    fetcher.fail_on("/api/parser/ingredients", 500, "Parser unavailable")

    with pytest.raises(ToolError, match="Error parsing ingredients"):
        await invoke("parse_ingredients", ingredients=["2 eggs"])


async def test_parser_logs_exclude_inputs_and_client_errors(
    invoke, fetcher, caplog
):
    caplog.set_level(logging.DEBUG, logger="mealie-mcp")
    await invoke("parse_ingredients", ingredients=["private-ingredient"])
    fetcher.fail_on("/api/parser/", 500, "private-error-response")
    with pytest.raises(ToolError):
        await invoke("parse_ingredients", ingredients=["private-ingredient"])
    assert "private-ingredient" not in caplog.text
    assert "private-error-response" not in caplog.text


async def test_parser_validation_does_not_log_user_supplied_parser(invoke, caplog):
    caplog.set_level(logging.DEBUG, logger="mealie-mcp")
    with pytest.raises(ToolError, match="parser must be one of"):
        await invoke(
            "parse_ingredients", ingredients=["private-ingredient"], parser="private-parser"
        )
    assert "private-parser" not in caplog.text
    assert "private-ingredient" not in caplog.text


async def test_parse_ingredients_normalizes_decimal_commas(invoke, fetcher):
    out = await invoke("parse_ingredients", ingredients=["0,25 tl zout", "1,5 kg"])
    assert fetcher.last("POST", "/api/parser/ingredients")["json"]["ingredients"] == [
        "0.25 tl zout",
        "1.5 kg",
    ]
    # results are reported against the caller's own lines
    assert [r["input"] for r in out] == ["0,25 tl zout", "1,5 kg"]


# --- suggestions for lines the parser left without a food ---------------------

OLIVE_OIL = "d0000000-0000-4000-8000-000000000001"
SALT = "d0000000-0000-4000-8000-000000000002"
BAY_LEAF = "d0000000-0000-4000-8000-000000000003"
PASTA = "d0000000-0000-4000-8000-000000000004"
PASTA_SAUCE = "d0000000-0000-4000-8000-000000000005"
PARSNIP = "d0000000-0000-4000-8000-000000000006"


def catalogue_food(food_id, name, aliases=()):
    return {
        "id": food_id,
        "name": name,
        "pluralName": None,
        "description": "",
        "aliases": [{"name": a} for a in aliases],
    }


def parse_result(text, food, quantity=None, note=""):
    """A ParsedIngredient; food is a catalogue record or unmatched food text."""
    if isinstance(food, str):
        food = {"id": None, "name": food}
    return {
        "input": text,
        "confidence": {"average": 0.9},
        "ingredient": {
            "quantity": quantity,
            "unit": None,
            "food": food,
            "note": note,
            "referenceId": "75e1853f-3cb1-49b9-b2ca-26ae76da256b",
        },
    }


@pytest.fixture
def foods(fetcher):
    fetcher.foods = [
        catalogue_food(OLIVE_OIL, "olijfolie"),
        catalogue_food(SALT, "salt", aliases=["zout"]),
        catalogue_food(BAY_LEAF, "laurierblad", aliases=["laurier"]),
        catalogue_food(PASTA, "pasta"),
        catalogue_food(PASTA_SAUCE, "pastasaus"),
        catalogue_food(PARSNIP, "pastinaak"),
    ]
    return fetcher


def parser_returns(fetcher, *results):
    fetcher.responses["POST", "/api/parser/ingredients"] = list(results)


def food_lookups(fetcher):
    return [r for r in fetcher.requests if r["url"] == "/api/foods"]


async def test_matched_food_gets_no_suggestion(invoke, foods):
    parser_returns(
        foods, parse_result("2 el olijfolie", catalogue_food(OLIVE_OIL, "olijfolie"), 2)
    )
    [out] = await invoke("parse_ingredients", ingredients=["2 el olijfolie"])

    assert out["food"] == {"id": OLIVE_OIL, "name": "olijfolie"}
    assert "suggestion" not in out and "resolved" not in out
    # nothing to resolve, so the food list is not loaded
    assert food_lookups(foods) == []


async def test_word_match_is_suggested_with_note_not_applied(invoke, foods):
    parser_returns(foods, parse_result("zout naar smaak", "zout naar smaak"))
    [out] = await invoke("parse_ingredients", ingredients=["zout naar smaak"])

    assert out["food"] == {"id": None, "name": "zout naar smaak"}
    assert "resolved" not in out
    suggestion = out["suggestion"]
    assert suggestion["text"] == "zout naar smaak"
    assert suggestion["status"] == "suggested"
    assert suggestion["suggestion"] == {
        "action": "use_with_note",
        "food": {"id": SALT, "name": "salt"},
        "note": "naar smaak",
    }
    assert suggestion["candidates"][0]["id"] == SALT


async def test_exact_alias_missed_by_parser_is_auto_resolved(invoke, foods):
    parser_returns(foods, parse_result("2 laurier", "Laurier", 2, note="gedroogd"))
    [out] = await invoke("parse_ingredients", ingredients=["2 laurier"])

    assert out["food"] == {"id": BAY_LEAF, "name": "laurierblad"}
    assert out["resolved"] == "auto"
    assert out["note"] == "gedroogd"
    assert "suggestion" not in out


async def test_unknown_food_is_suggested_for_creation(invoke, foods):
    parser_returns(foods, parse_result("400 g passata", "passata", 400))
    [out] = await invoke("parse_ingredients", ingredients=["400 g passata"])

    assert out["food"] == {"id": None, "name": "passata"}
    assert out["suggestion"]["suggestion"] == {"action": "create", "name": "passata"}
    # near misses are listed for the caller to judge, never picked
    assert PASTA in {c["id"] for c in out["suggestion"]["candidates"]}


async def test_food_list_is_loaded_once_and_nothing_is_written(invoke, foods):
    parser_returns(
        foods,
        parse_result("2 el olijfolie", catalogue_food(OLIVE_OIL, "olijfolie"), 2),
        parse_result("zout naar smaak", "zout naar smaak"),
        parse_result("2 laurier", "laurier", 2),
        parse_result("400 g passata", "passata", 400),
    )
    lines = ["2 el olijfolie", "zout naar smaak", "2 laurier", "400 g passata"]
    out = await invoke("parse_ingredients", ingredients=lines)

    assert [(r["method"], r["params"]) for r in food_lookups(foods)] == [
        ("GET", {"perPage": -1})
    ]
    assert [r["method"] for r in foods.requests] == ["POST", "GET"]
    assert [r["input"] for r in out] == lines
    assert [r.get("resolved") for r in out] == [None, None, "auto", None]


async def test_verbose_adds_suggestion_to_untouched_response(invoke, foods):
    parser_returns(
        foods,
        parse_result("zout naar smaak", "zout naar smaak"),
        parse_result("2 laurier", "laurier", 2),
    )
    out = await invoke(
        "parse_ingredients", ingredients=["zout naar smaak", "2 laurier"], verbose=True
    )

    assert out[0]["ingredient"]["food"] == {"id": None, "name": "zout naar smaak"}
    assert out[0]["suggestion"]["suggestion"]["action"] == "use_with_note"
    # verbose never rewrites Mealie's response, even for an exact match
    assert out[1]["ingredient"]["food"] == {"id": None, "name": "laurier"}
    assert out[1]["suggestion"]["suggestion"] == {
        "action": "use",
        "food": {"id": BAY_LEAF, "name": "laurierblad"},
    }
    assert "resolved" not in out[1]


async def test_food_list_failure_keeps_parse_results(invoke, foods, caplog):
    caplog.set_level(logging.DEBUG, logger="mealie-mcp")
    parser_returns(foods, parse_result("private-food-text", "private-food-text"))
    foods.fail_on("/api/foods", 500, "private-error-response")
    [out] = await invoke("parse_ingredients", ingredients=["private-food-text"])

    assert out["food"] == {"id": None, "name": "private-food-text"}
    assert out["suggestion"] == {
        "text": "private-food-text",
        "status": "error",
        "error": "Mealie API request failed (HTTP 500)",
    }
    assert "private-food-text" not in caplog.text
    assert "private-error-response" not in caplog.text


async def test_malformed_food_list_is_reported_on_the_line(invoke, foods):
    parser_returns(foods, parse_result("passata", "passata"))
    foods.responses["GET", "/api/foods"] = {"detail": "unexpected"}
    [out] = await invoke("parse_ingredients", ingredients=["passata"])

    assert out["suggestion"]["status"] == "error"
    assert "unexpected food list" in out["suggestion"]["error"]


async def test_compound_match_stays_a_suggestion(invoke, fetcher):
    fetcher.foods = [
        catalogue_food("d0000000-0000-4000-8000-000000000007", "selderij"),
        catalogue_food("d0000000-0000-4000-8000-000000000008", "knolselderij"),
    ]
    parser_returns(fetcher, parse_result("1 stengel bleekselderij", "stengel bleekselderij", 1))
    [out] = await invoke("parse_ingredients", ingredients=["1 stengel bleekselderij"])

    assert out["food"] == {"id": None, "name": "stengel bleekselderij"}
    assert "resolved" not in out
    assert out["suggestion"]["suggestion"]["action"] == "alias"
    assert [c["name"] for c in out["suggestion"]["candidates"]] == [
        "selderij",
        "knolselderij",
    ]
