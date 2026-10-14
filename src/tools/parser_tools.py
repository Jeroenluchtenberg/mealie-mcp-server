import logging
import re
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from mealie import MealieFetcher
from mealie.client import MealieApiError
from tools.errors import tool_error_boundary
from tools.food_matching import ITEM_ERRORS, failure_message, load_foods, suggest

logger = logging.getLogger("mealie-mcp")

_PARSERS = ("nlp", "brute", "openai")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")


def normalize_ingredient_line(line: str) -> str:
    """Rewrite decimal commas ("0,25 tl") so Mealie's parser reads the quantity."""
    return _DECIMAL_COMMA.sub(".", line.strip())


def _organizer_ref(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Reduce a parsed food/unit to the {id, name} pair the recipe tools need."""
    if not value:
        return None
    return {"id": value.get("id"), "name": value.get("name")}


def _flatten(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a Mealie ParsedIngredient to a RecipeIngredientInput-shaped dict.

    Mealie returns the full food and unit records plus a per-field confidence
    breakdown. Only the ids and names are needed to build a recipe, so the rest
    is dropped and the confidences collapse to their average; pass verbose=True
    to the tool to get the untouched response instead.
    """
    ingredient = parsed.get("ingredient") or {}
    return {
        "input": parsed.get("input"),
        "confidence": (parsed.get("confidence") or {}).get("average"),
        "quantity": ingredient.get("quantity"),
        "unit": _organizer_ref(ingredient.get("unit")),
        "food": _organizer_ref(ingredient.get("food")),
        "note": ingredient.get("note"),
    }


def _unmatched_food_text(parsed: Any) -> Optional[str]:
    """Return the food text of a parse result the parser left without a food."""
    ingredient = (parsed or {}).get("ingredient") if isinstance(parsed, dict) else None
    food = (ingredient or {}).get("food")
    if not isinstance(food, dict) or food.get("id"):
        return None
    return (food.get("name") or "").strip() or None


def _suggestions(
    mealie: MealieFetcher, texts: List[Optional[str]]
) -> List[Optional[Dict[str, Any]]]:
    """Suggest a food for each unmatched text, loading the catalogue once.

    A failed catalogue lookup is reported on each unmatched line rather than
    failing the call, so the parse results are not lost.
    """
    if not any(texts):
        return [None] * len(texts)
    try:
        foods = load_foods(mealie)
    except ITEM_ERRORS as error:
        metadata = {
            "operation": "Loading foods for ingredient suggestions",
            "error_type": type(error).__name__,
        }
        if isinstance(error, MealieApiError):
            metadata["status_code"] = error.status_code
        logger.error(metadata)
        failed = {"status": "error", "error": failure_message(error)}
        return [{"text": text, **failed} if text else None for text in texts]
    return [{"text": text, **suggest(foods, text)} if text else None for text in texts]


def _apply_suggestion(
    result: Dict[str, Any], suggestion: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Link an exact food match; attach anything less certain as a suggestion."""
    if suggestion is None:
        return result
    proposed = suggestion.get("suggestion") or {}
    if proposed.get("action") == "use":
        return {**result, "food": proposed["food"], "resolved": "auto"}
    return {**result, "suggestion": suggestion}


def _validate_parser(parser: str) -> None:
    if parser not in _PARSERS:
        raise ValueError(f"parser must be one of {', '.join(_PARSERS)}, got '{parser}'")


def register_parser_tools(mcp: FastMCP, mealie: MealieFetcher) -> None:
    """Register the ingredient-parser tools with the MCP server."""

    @mcp.tool()
    def parse_ingredients(
        ingredients: List[str], parser: str = "nlp", verbose: bool = False
    ) -> List[Dict[str, Any]]:
        """Resolve one or more ingredient lines in a single call.

        Mealie's server-side parser turns "1/4 cup chopped onion" into
        quantity 0.25, the existing "cup" unit, the existing "onion" food, and
        the note "chopped" — in one call, instead of searching get_foods and
        get_units per ingredient. Decimal commas ("0,25 tl") are read as decimal
        points. Pass a one-element list for a single line.
        Results always come back as a list in input order, one per line.

        The returned unit and food carry the ids create_recipe needs, so a
        result can be passed straight through as a structured ingredient.
        A null unit means Mealie has no matching unit.

        When the parser finds no food, its food text is compared with every
        existing food name, plural, and alias (the food list is loaded once
        per call, and nothing is written):
        - An exact name, plural, or alias match is linked as if the parser had
          matched it, and the line is marked "resolved": "auto".
        - Otherwise food stays {id: null, name: <text>} and the line gets a
          suggestion: {text, status, candidates, suggestion}, the same shape
          resolve_ingredients returns. suggestion.action is one of
          use_with_note (an existing food appears inside the text, e.g.
          "zout naar smaak" -> "zout" with note "naar smaak"; check it is the
          same ingredient: "zoete aardappel" is not "aardappel"), alias (a
          near-identical food, such as a plural or typo), or create (nothing
          similar). candidates lists the closest existing foods.
        A suggestion is not applied. Confirm it with the user or decide
        between the candidates, then link the food or apply the alias or
        create with resolve_ingredients. Never turn an unaccepted suggestion
        into a new food silently: duplicate foods break shopping-list merging.
        If the food list cannot be loaded, the suggestion has status "error".

        Confidence is Mealie's own 0-1 score; low values are worth checking
        against the source text before writing the recipe.

        Args:
            ingredients: Nonempty ingredient lines to parse, in recipe order.
            parser: Parser backend — "nlp" (default, trained model), "brute"
                (regex splitting, better for terse or unusual formats), or
                "openai" (only if the Mealie server is configured for it).
            verbose: If True, return Mealie's untouched responses, including the
                full food/unit records and the per-field confidence breakdown,
                plus suggestion on lines without a food. Exact matches are
                reported as a "use" suggestion and not applied.

        Returns:
            List[Dict[str, Any]]: One result per input line, in the same order,
            each with input, confidence, quantity, unit, food, and note, plus
            resolved or suggestion on lines the parser left without a food.
        """
        with tool_error_boundary("Error parsing ingredients"):
            _validate_parser(parser)
            if any(not ingredient.strip() for ingredient in ingredients):
                raise ValueError("Ingredient cannot be empty")
            parsed = mealie.parse_ingredients(
                [normalize_ingredient_line(line) for line in ingredients], parser=parser
            )
            suggestions = _suggestions(
                mealie, [_unmatched_food_text(result) for result in parsed]
            )
            if verbose:
                return [
                    {**result, "suggestion": suggestion} if suggestion else result
                    for result, suggestion in zip(parsed, suggestions)
                ]
            # Report each result against the caller's own line.
            return [
                _apply_suggestion({**_flatten(result), "input": line}, suggestion)
                for line, result, suggestion in zip(ingredients, parsed, suggestions)
            ]
