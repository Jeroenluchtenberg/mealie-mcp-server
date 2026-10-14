"""Resolve ingredient text that Mealie's parser could not link to a food.

The food catalogue is loaded once per call and matched here rather than with
Mealie's search, which only finds substrings: "laurierbladeren" would never
find "laurierblad". Catching such near-duplicates before a food is created
matters because duplicate foods break shopping-list merging.
"""

import logging
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from identity import CredentialError
from mealie import MealieFetcher
from mealie.client import MealieApiError
from models.foods import IngredientResolution
from tools.errors import tool_error_boundary

logger = logging.getLogger("mealie-mcp")

MAX_RESOLUTIONS = 50
MAX_CANDIDATES = 5
# Similarity at or above this suggests an alias (plural forms, small typos).
ALIAS_THRESHOLD = 0.85
PERMISSION_MESSAGE = (
    "Mealie denied the change (HTTP 403). Creating foods or aliases needs the "
    "'can organize group data' permission; ask the admin to grant it."
)


def _normalize(text: Optional[str]) -> str:
    """Casefold, drop accents, and collapse whitespace for comparisons."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split()).strip(" .,;:()")


def _food_names(food: Dict[str, Any]) -> List[str]:
    aliases = [a.get("name") for a in food.get("aliases") or [] if isinstance(a, dict)]
    names = [_normalize(n) for n in (food.get("name"), food.get("pluralName"), *aliases)]
    return [n for n in dict.fromkeys(names) if n]


def _ref(food: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": food.get("id"), "name": food.get("name")}


def _match(food: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    """Score one food against the text; None when it is not a plausible match."""
    tokens = text.split()
    words = [_normalize(t) for t in tokens]
    target = " ".join(words)
    best: Optional[Dict[str, Any]] = None
    for name in _food_names(food):
        if name == target:
            return {"score": 1.0, "match": "exact", "leftover": []}
        name_words = name.split()
        for start in range(len(words) - len(name_words) + 1):
            if words[start : start + len(name_words)] == name_words:
                # Prefer the longest food name contained in the text.
                score = min(0.9 + 0.01 * len(name_words), 0.99)
                leftover = tokens[:start] + tokens[start + len(name_words) :]
                if best is None or score > best["score"]:
                    best = {"score": score, "match": "word", "leftover": leftover}
        matcher = SequenceMatcher(None, name, target)
        # quick_ratio is a cheap upper bound; skip the full comparison when it
        # cannot reach the candidate threshold.
        if matcher.quick_ratio() < 0.6:
            continue
        ratio = matcher.ratio()
        if ratio >= 0.6 and (best is None or ratio > best["score"]):
            best = {"score": ratio, "match": "similar", "leftover": []}
    return best


def _load_foods(mealie: MealieFetcher) -> List[Dict[str, Any]]:
    """Fetch the whole food catalogue in one request."""
    response = mealie.get_foods(per_page=-1)
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise ValueError("Mealie returned an unexpected food list")
    return [i for i in items if isinstance(i, dict) and i.get("id")]


def _exact_food(foods: List[Dict[str, Any]], text: str) -> Optional[Dict[str, Any]]:
    """Return a food whose name, plural, or alias equals the text."""
    target = _normalize(text)
    return next((food for food in foods if target in _food_names(food)), None)


def _suggest(foods: List[Dict[str, Any]], text: str) -> Dict[str, Any]:
    scored = []
    for food in foods:
        match = _match(food, text)
        if match:
            scored.append((match, food))
    scored.sort(key=lambda pair: pair[0]["score"], reverse=True)
    candidates = [
        {**_ref(food), "score": round(match["score"], 2), "match": match["match"]}
        for match, food in scored[:MAX_CANDIDATES]
    ]
    if not scored:
        suggestion = {"action": "create", "name": text}
    else:
        match, food = scored[0]
        if match["match"] == "exact":
            suggestion = {"action": "use", "food": _ref(food)}
        elif match["match"] == "word":
            suggestion = {
                "action": "use_with_note",
                "food": _ref(food),
                "note": " ".join(match["leftover"]),
            }
        elif match["score"] >= ALIAS_THRESHOLD:
            suggestion = {"action": "alias", "food": _ref(food), "alias": text}
        else:
            suggestion = {"action": "create", "name": text}
    return {"status": "suggested", "candidates": candidates, "suggestion": suggestion}


def _add_alias(
    mealie: MealieFetcher, foods: List[Dict[str, Any]], item: IngredientResolution
) -> Dict[str, Any]:
    alias = (item.alias or item.text).strip()
    owner = _exact_food(foods, alias)
    if owner and owner["id"] != item.food_id:
        # The text already resolves to another food; aliasing it here as well
        # would make the parser's choice ambiguous.
        return {"status": "conflict", "food": _ref(owner), "alias": alias}
    result = mealie.add_food_alias(item.food_id, alias)
    if result["added"] and isinstance(result["food"], dict):
        # Later items in this call must see the new alias.
        foods[:] = [f for f in foods if f["id"] != item.food_id] + [result["food"]]
    return {
        "status": "aliased" if result["added"] else "unchanged",
        "food": _ref(result["food"]),
        "alias": alias,
    }


def _create(
    mealie: MealieFetcher, foods: List[Dict[str, Any]], item: IngredientResolution
) -> Dict[str, Any]:
    name = (item.name or item.text).strip()
    existing = _exact_food(foods, name)
    if existing:
        return {"status": "exists", "food": _ref(existing)}
    created = mealie.create_food(name)
    if isinstance(created, dict) and created.get("id"):
        foods.append(created)
    return {"status": "created", "food": _ref(created)}


def _failure(error: Exception) -> str:
    if isinstance(error, CredentialError):
        return str(error)
    if isinstance(error, MealieApiError):
        if error.status_code == 403:
            return PERMISSION_MESSAGE
        return f"Mealie API request failed (HTTP {error.status_code})"
    if isinstance(error, httpx.HTTPStatusError):
        return f"Mealie API request failed (HTTP {error.response.status_code})"
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "Mealie API request timed out"
    if isinstance(error, ValueError):
        return str(error)
    return "Could not communicate with the Mealie API"


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

        Use this for create_recipe/update_recipe unmatched_ingredients (or
        parse_ingredients results with a null food). Run it in two passes:

        1. action "suggest" (default, no writes): compares the text with every
           existing food name, plural, and alias, as a whole, by word, and by
           similarity, and returns ranked candidates plus a suggestion:
           - use: an existing food already matches; link it directly.
           - use_with_note: an existing food appears inside the text, e.g.
             "stengel bleekselderij" -> food "bleekselderij" with note
             "stengel", or "zout naar smaak" -> "zout" with note "naar smaak".
             Link that food and keep the rest as the note; no alias is needed.
           - alias: a near-identical food exists (plural form, typo).
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
            foods = _load_foods(mealie)
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
                        result.update(_suggest(foods, item.text))
                    elif item.action == "alias":
                        result.update(_add_alias(mealie, foods, item))
                    else:
                        result.update(_create(mealie, foods, item))
                except (
                    CredentialError,
                    MealieApiError,
                    httpx.HTTPError,
                    TimeoutError,
                    ConnectionError,
                    ValueError,
                ) as error:
                    metadata = {
                        "operation": "Resolving ingredient",
                        "error_type": type(error).__name__,
                    }
                    if isinstance(error, MealieApiError):
                        metadata["status_code"] = error.status_code
                        denied = denied or error.status_code == 403
                    logger.error(metadata)
                    result.update(status="error", error=_failure(error))
                results.append(result)
            return {"results": results}
