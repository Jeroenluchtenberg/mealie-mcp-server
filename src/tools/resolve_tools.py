"""Resolve ingredient text that Mealie's parser could not link to a food.

Matching lives in tools.food_matching, shared with parse_ingredients; this
module adds the guarded alias and create writes.
"""

import logging
from typing import Any, Dict, List

from mcp.server.fastmcp import FastMCP

from mealie import MealieFetcher
from mealie.client import MealieApiError
from models.foods import IngredientResolution
from tools.errors import tool_error_boundary
from tools.food_matching import (
    ITEM_ERRORS,
    PERMISSION_MESSAGE,
    exact_food,
    failure_message,
    food_ref,
    load_foods,
    suggest,
)

logger = logging.getLogger("mealie-mcp")

MAX_RESOLUTIONS = 50


def _add_alias(
    mealie: MealieFetcher, foods: List[Dict[str, Any]], item: IngredientResolution
) -> Dict[str, Any]:
    alias = (item.alias or item.text).strip()
    owner = exact_food(foods, alias)
    if owner and owner["id"] != item.food_id:
        # The text already resolves to another food; aliasing it here as well
        # would make the parser's choice ambiguous.
        return {"status": "conflict", "food": food_ref(owner), "alias": alias}
    result = mealie.add_food_alias(item.food_id, alias)
    if result["added"] and isinstance(result["food"], dict):
        # Later items in this call must see the new alias.
        foods[:] = [f for f in foods if f["id"] != item.food_id] + [result["food"]]
    return {
        "status": "aliased" if result["added"] else "unchanged",
        "food": food_ref(result["food"]),
        "alias": alias,
    }


def _create(
    mealie: MealieFetcher, foods: List[Dict[str, Any]], item: IngredientResolution
) -> Dict[str, Any]:
    name = (item.name or item.text).strip()
    existing = exact_food(foods, name)
    if existing:
        return {"status": "exists", "food": food_ref(existing)}
    created = mealie.create_food(name)
    if isinstance(created, dict) and created.get("id"):
        foods.append(created)
    return {"status": "created", "food": food_ref(created)}


def _validate(items: List[IngredientResolution]) -> None:
    if not items:
        raise ValueError("Provide at least one item to resolve")
    if len(items) > MAX_RESOLUTIONS:
        raise ValueError(f"Resolve at most {MAX_RESOLUTIONS} items per call")
    for item in items:
        if not item.text.strip():
            raise ValueError("Item text cannot be empty")
        if item.action == "alias" and not (item.food_id or "").strip():
            raise ValueError("alias items need the food_id of an existing food")
        for value in (item.alias, item.name):
            if value is not None and not value.strip():
                raise ValueError("alias and name cannot be empty when given")


def register_resolve_tools(mcp: FastMCP, mealie: MealieFetcher) -> None:
    """Register the ingredient-resolution tool with the MCP server."""

    @mcp.tool()
    def resolve_ingredients(items: List[IngredientResolution]) -> Dict[str, Any]:
        """Match unmatched ingredient foods to existing foods, or add new ones.

        Use this for create_recipe/update_recipe unmatched_ingredients, and to
        apply an alias or create decision for a parse_ingredients suggestion
        (which already holds the pass-1 result). Run it in two passes:

        1. action "suggest" (default, no writes): compares the text with every
           existing food name, plural, and alias, as a whole and word by word,
           by equality, similarity, and compound suffix, and returns ranked
           candidates plus a suggestion:
           - use: an existing food already matches; link it directly.
           - use_with_note: an existing food appears inside the text, e.g.
             "stengel bleekselderij" -> food "bleekselderij" with note
             "stengel", or "zout naar smaak" -> "zout" with note "naar smaak".
             Link that food and keep the rest as the note; no alias is needed.
           - alias: a near-identical food exists (plural form, typo), or a
             word ends in an existing food name ("bleekselderij" ->
             "selderij"). alias holds the matched words and note any others.
             Check the candidates first: the prefix can make it a different
             ingredient (knolselderij).
           - create: nothing similar exists.
        2. Apply the decision: action "alias" with food_id adds the text as an
           alias, so the parser matches it from then on; action "create" makes a
           new food. Prefer an alias over a new food whenever the food is the
           same ingredient, so shopping lists merge. Do not alias different
           ingredients (knolselderij is not bleekselderij).

        Writes are guarded against duplicates: create returns status "exists"
        when a food with that name or alias already exists, and alias returns
        "conflict" when the alias already belongs to a different food.

        Creating foods or aliases needs the Mealie permission "can organize
        group data"; without it the item fails with a message saying so and
        the remaining writes are skipped.

        Args:
            items: Up to 50 items, each {text, action, food_id, alias, name}.

        Returns:
            Dict[str, Any]: {"results": [...]} in input order. Each result has
            index, text, action, and status (suggested, created, exists, aliased,
            unchanged, conflict, error, skipped), plus food,
            candidates, suggestion, alias, or error where relevant. Items are
            processed independently; one failure does not undo earlier writes.
        """
        with tool_error_boundary("Error resolving ingredients"):
            _validate(items)
            foods = load_foods(mealie)
            results = []
            denied = False
            for index, item in enumerate(items):
                result: Dict[str, Any] = {
                    "index": index,
                    "text": item.text,
                    "action": item.action,
                }
                if denied and item.action != "suggest":
                    results.append(
                        {**result, "status": "skipped", "error": PERMISSION_MESSAGE}
                    )
                    continue
                try:
                    if item.action == "suggest":
                        result.update(suggest(foods, item.text))
                    elif item.action == "alias":
                        result.update(_add_alias(mealie, foods, item))
                    else:
                        result.update(_create(mealie, foods, item))
                except ITEM_ERRORS as error:
                    metadata = {
                        "operation": "Resolving ingredient",
                        "error_type": type(error).__name__,
                    }
                    if isinstance(error, MealieApiError):
                        metadata["status_code"] = error.status_code
                        denied = denied or error.status_code == 403
                    logger.error(metadata)
                    result.update(status="error", error=failure_message(error))
                results.append(result)
            return {"results": results}
