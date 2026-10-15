import json
import math
import re
import uuid
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Dict, List, Optional, Union

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from mealie import MealieFetcher
from models.recipe import (
    OrganizerRef,
    Recipe,
    RecipeIngredient,
    RecipeIngredientInput,
    RecipeInstruction,
    RecipeInstructionInput,
    RecipeNutrition,
    RecipeSettingsInput,
)
from tools.errors import tool_error_boundary
from tools.pagination import page_result, paginate
from tools.parser_tools import normalize_ingredient_line


def _known_ref(value: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Keep a parsed unit/food only when it links to an existing Mealie record."""
    if not value or not value.get("id"):
        return None
    return {"id": value["id"], "name": value.get("name")}


def _parse_string_ingredients(
    mealie: MealieFetcher,
    ingredients: Optional[List[Union[str, RecipeIngredientInput]]],
) -> tuple[
    Optional[List[Union[str, Dict[str, Any], RecipeIngredientInput]]],
    List[Dict[str, Any]],
]:
    """Resolve plain-string ingredients through Mealie's parser in one request.

    Mealie stores a string ingredient as a bare note, so strings are parsed
    here and turned into structured ingredients linked to existing units and
    foods. Text the parser could not link to an existing record is kept in the
    note and reported back, so the caller can resolve it. Structured
    ingredients pass through unchanged.
    """
    positions = [
        index
        for index, ingredient in enumerate(ingredients or [])
        if isinstance(ingredient, str) and ingredient.strip()
    ]
    if not positions:
        return ingredients, []
    resolved: List[Union[str, Dict[str, Any], RecipeIngredientInput]] = list(
        ingredients
    )
    lines = [normalize_ingredient_line(ingredients[index]) for index in positions]
    parsed = mealie.parse_ingredients(lines)
    if not isinstance(parsed, list) or len(parsed) != len(lines):
        raise ValueError("Mealie returned an unexpected ingredient parse result")
    unmatched = []
    for index, result in zip(positions, parsed):
        original = ingredients[index]
        ingredient = (result or {}).get("ingredient") or {}
        unit = _known_ref(ingredient.get("unit"))
        food = _known_ref(ingredient.get("food"))
        unit_text = None if unit else (ingredient.get("unit") or {}).get("name")
        food_text = None if food else (ingredient.get("food") or {}).get("name")
        note_parts = [unit_text, food_text, ingredient.get("note")]
        entry: Dict[str, Any] = {"originalText": original}
        quantity = ingredient.get("quantity")
        if isinstance(quantity, (int, float)) and math.isfinite(quantity):
            entry["quantity"] = quantity
        if unit:
            entry["unit"] = unit
        if food:
            entry["food"] = food
        note = " ".join(part.strip() for part in note_parts if part and part.strip())
        if note:
            entry["note"] = note
        resolved[index] = entry
        if not food or unit_text:
            unmatched.append(
                {
                    "index": index,
                    "input": original,
                    "food": food_text,
                    "unit": unit_text,
                }
            )
    return resolved, unmatched


def _build_ingredient(
    ingredient: Union[str, RecipeIngredientInput, Dict[str, Any]],
) -> RecipeIngredient:
    """Coerce a flat string or structured ingredient into a RecipeIngredient.

    Strings are normally resolved by _parse_string_ingredients first; a string
    reaching this point is stored as a note. A structured object maps its
    fields straight onto the Mealie RecipeIngredient model.
    """
    if isinstance(ingredient, str):
        return RecipeIngredient(note=ingredient)
    if isinstance(ingredient, RecipeIngredientInput):
        ingredient = ingredient.model_dump(exclude_none=True)
    return RecipeIngredient(**ingredient)


def _build_instruction(
    instruction: Union[str, RecipeInstructionInput, Dict[str, Any]],
) -> RecipeInstruction:
    """Coerce a flat string or structured step into a RecipeInstruction."""
    if isinstance(instruction, str):
        return RecipeInstruction(text=instruction)
    if isinstance(instruction, RecipeInstructionInput):
        instruction = instruction.model_dump(exclude_none=True)
    return RecipeInstruction(**instruction)


_REF_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "mealie-mcp/recipe-ingredient-reference"
)


def _slugify(name: str) -> str:
    """Best-effort Mealie-style slug, used to fill an omitted organizer slug."""
    slug = name.strip().lower()
    for umlaut, ascii_ in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss")):
        slug = slug.replace(umlaut, ascii_)
    return re.sub(r"[^a-z0-9]+", "-", slug).strip("-")


def _organizer_payload(org: OrganizerRef) -> Dict[str, Any]:
    """Map an OrganizerRef to the {id, name, slug} Mealie requires for tags/tools.

    Mealie requires a slug on every tag/tool reference; derive one from the name
    when the caller omits it (Mealie links the organizer by its id, so the exact
    slug only needs to be present).
    """
    return {"id": org.id, "name": org.name, "slug": org.slug or _slugify(org.name)}


def _coerce_reference_id(value: Optional[str]) -> Optional[str]:
    """Return a UUID v4 string for an ingredient referenceId.

    Mealie validates instruction ingredientReferences as UUID **v4**. An
    existing v4 UUID passes through unchanged; anything else (a non-UUID, or a
    UUID of another version) is mapped to a stable v4 UUID derived from it — a
    uuid5 hash with the version/variant bits forced to 4/RFC-4122. It is
    deterministic, so the same caller-supplied id used on an ingredient and on
    an instruction step resolves to the same v4 UUID and keeps the link intact.
    """
    if not value:
        return value
    try:
        existing = uuid.UUID(str(value))
        if existing.version == 4:
            return str(existing)
    except (ValueError, AttributeError, TypeError):
        pass
    return str(uuid.UUID(int=uuid.uuid5(_REF_NAMESPACE, str(value)).int, version=4))


def _normalize_references(
    ingredients: List[RecipeIngredient], instructions: List[RecipeInstruction]
) -> None:
    """Coerce every ingredient referenceId and instruction link to a UUID."""
    for ingredient in ingredients:
        if ingredient.referenceId:
            ingredient.referenceId = _coerce_reference_id(ingredient.referenceId)
    for step in instructions:
        for ref in step.ingredientReferences:
            if ref.referenceId:
                ref.referenceId = _coerce_reference_id(ref.referenceId)


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} cannot be empty")


def _recipe_changes(
    *,
    ingredients: Optional[List[Union[str, RecipeIngredientInput]]] = None,
    instructions: Optional[List[Union[str, RecipeInstructionInput]]] = None,
    tags: Optional[List[OrganizerRef]] = None,
    tools: Optional[List[OrganizerRef]] = None,
    nutrition: Optional[RecipeNutrition] = None,
    settings: Optional[RecipeSettingsInput] = None,
    **fields: Any,
) -> Dict[str, Any]:
    """Validate and serialize caller content before any creation request."""
    field_names = {
        "name": "name",
        "description": "description",
        "org_url": "orgURL",
        "total_time": "totalTime",
        "prep_time": "prepTime",
        "cook_time": "cookTime",
        "perform_time": "performTime",
        "recipe_yield": "recipeYield",
        "servings": "recipeServings",
    }
    changes = {
        field_names[key]: value for key, value in fields.items() if value is not None
    }
    if "name" in changes:
        _require_text(changes["name"], "Recipe name")
    if "recipeServings" in changes and not math.isfinite(changes["recipeServings"]):
        raise ValueError("Servings must be finite")
    built_ingredients = [_build_ingredient(i) for i in ingredients or []]
    built_instructions = [_build_instruction(i) for i in instructions or []]
    _normalize_references(built_ingredients, built_instructions)
    if ingredients is not None:
        changes["recipeIngredient"] = [
            i.model_dump(exclude_none=True) for i in built_ingredients
        ]
    if instructions is not None:
        changes["recipeInstructions"] = [
            i.model_dump(exclude_none=True) for i in built_instructions
        ]
    if tags is not None:
        changes["tags"] = [_organizer_payload(t) for t in tags]
    if tools is not None:
        changes["tools"] = [_organizer_payload(t) for t in tools]
    if nutrition is not None:
        changes["nutrition"] = nutrition.model_dump(exclude_none=True)
    if settings is not None:
        changes["settings"] = settings.model_dump(exclude_none=True)
    return changes


def _parsed_recipe_changes(
    mealie: MealieFetcher, **arguments: Any
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Validate all content first, then resolve ingredient strings.

    Invalid input fails before the parse request, so it never reaches Mealie.
    """
    changes = _recipe_changes(**arguments)
    ingredients, unmatched = _parse_string_ingredients(
        mealie, arguments.get("ingredients")
    )
    if ingredients is not arguments.get("ingredients"):
        changes = _recipe_changes(**{**arguments, "ingredients": ingredients})
    return changes, unmatched


def _compose_recipe(
    current: Dict[str, Any], changes: Dict[str, Any]
) -> Dict[str, Any]:
    """Preserve fetched fields, replacing content but merging settings toggles."""
    recipe = deepcopy(current)
    recipe.setdefault("nutrition", {})
    recipe.update(deepcopy(changes))
    if "settings" in changes:
        recipe["settings"] = {
            **deepcopy(current.get("settings") or {}),
            **changes["settings"],
        }
    return recipe


@contextmanager
def _created_recipe_stage(slug: str, stage: str):
    """Expose recovery information only to the caller, never to logs."""
    try:
        with tool_error_boundary("Error completing created recipe"):
            yield
    except ToolError:
        raise ToolError(
            json.dumps(
                {
                    "message": (
                        "Recipe was created; inspect it and resume using its slug "
                        "instead of creating it again."
                    ),
                    "created_slug": slug,
                    "stage": stage,
                }
            )
        ) from None


def _create_populated_recipe(
    mealie: MealieFetcher,
    name: str,
    changes: Dict[str, Any],
    image_url: Optional[str] = None,
) -> Dict[str, Any]:
    _require_text(name, "Recipe name")
    if image_url is not None:
        _require_text(image_url, "Image URL")
    slug = mealie.create_recipe(name)
    with _created_recipe_stage(slug, "fetch"):
        current = mealie.get_recipe(slug)
    with _created_recipe_stage(slug, "populate"):
        updated = mealie.update_recipe(slug, _compose_recipe(current, changes))
    if image_url is not None:
        with _created_recipe_stage(slug, "image"):
            mealie.scrape_recipe_image_from_url(slug, image_url)
        with _created_recipe_stage(slug, "fetch_image"):
            updated = mealie.get_recipe(slug)
    return updated


def _with_unmatched(
    recipe: Dict[str, Any], unmatched: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Report ingredient lines the parser could not link to an existing food."""
    if unmatched and isinstance(recipe, dict):
        return {**recipe, "unmatched_ingredients": unmatched}
    return recipe


def _households_by_id(mealie: MealieFetcher) -> Dict[str, Dict[str, Any]]:
    """Fetch the group's households once, keyed by lowercase id."""
    page = mealie.get_households()
    items = page.get("items") if isinstance(page, dict) else None
    if not isinstance(items, list):
        raise ValueError("Mealie returned an unexpected household list")
    return {
        str(household["id"]).lower(): household
        for household in items
        if isinstance(household, dict) and household.get("id")
    }


def _resolve_households(
    mealie: MealieFetcher,
    requested: List[str],
    households: Dict[str, Dict[str, Any]],
) -> List[str]:
    """Turn household ids, slugs, and "self" into known household ids.

    Mealie ignores slugs it cannot match and drops the filter entirely when
    nothing matches, so unknown values are rejected here instead.
    """
    by_slug = {
        str(household.get("slug")).lower(): household_id
        for household_id, household in households.items()
        if household.get("slug")
    }
    resolved: List[str] = []
    unknown: List[str] = []
    for value in requested:
        key = value.strip().lower()
        if key == "self":
            own = mealie.get_current_user().get("householdId")
            if not own:
                raise ValueError("Mealie did not report your household")
            match = str(own).lower()
        elif key in households:
            match = key
        elif key in by_slug:
            match = by_slug[key]
        else:
            unknown.append(value)
            continue
        if match not in resolved:
            resolved.append(match)
    if unknown:
        available = ", ".join(
            sorted(str(h.get("slug")) for h in households.values() if h.get("slug"))
        )
        raise ValueError(
            f"Unknown household(s): {', '.join(unknown)}. "
            f"Available households: {available or 'none'}"
        )
    return resolved


ORDER_FIELDS = ("name", "rating", "date_added", "last_made")


def recipe_order(
    order_by: Optional[str], order_direction: Optional[str], search: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Validate sorting into get_recipes arguments for Mealie.

    Without order_by, results sort by name, except while searching, where
    Mealie's relevance order is kept (None). Nulls always sort last, so
    unrated or never-made recipes end up at the bottom either way.
    """
    if order_direction is not None:
        order_direction = order_direction.strip().lower()
        if order_direction not in ("asc", "desc"):
            raise ValueError(
                f"Unknown order_direction: {order_direction}. Use asc or desc"
            )
    if order_by is None:
        if search:
            return None
        order_by = "name"
    key = order_by.strip().lower()
    if key not in ORDER_FIELDS:
        raise ValueError(
            f"Unknown order_by: {order_by}. Use one of: {', '.join(ORDER_FIELDS)}"
        )
    return {
        "order_by": key,
        "order_direction": order_direction or ("asc" if key == "name" else "desc"),
        "order_by_null_position": "last",
    }


def _page_items(page: Any) -> List[Dict[str, Any]]:
    items = page.get("items") if isinstance(page, dict) else None
    if not isinstance(items, list):
        raise ValueError("Mealie returned an unexpected recipe list")
    return [item for item in items if isinstance(item, dict)]


def list_recipes(
    mealie: MealieFetcher,
    *,
    search: Optional[str],
    order: Optional[Dict[str, Any]],
    page: Optional[int],
    per_page: Optional[int],
    **filters: Any,
) -> Dict[str, Any]:
    """Fetch one page of recipes, always sorted by Mealie itself.

    Mealie ranks search matches by relevance before applying orderBy, so an
    explicit order would only break ties. The search then only selects the
    matches, and an unsearched listing with the same filters supplies their
    order, keeping Mealie's own rating and last-made rules.
    """
    if order is None or not search:
        return mealie.get_recipes(
            search=search, page=page, per_page=per_page, **(order or {}), **filters
        )
    matches = mealie.get_recipes(search=search, per_page=-1, **filters)
    matched = {item.get("id") for item in _page_items(matches)}
    items: List[Dict[str, Any]] = []
    if matched:
        ordered = mealie.get_recipes(per_page=-1, **order, **filters)
        items = [item for item in _page_items(ordered) if item.get("id") in matched]
    return paginate(items, page, per_page)


def _rating(value: Any) -> Any:
    """Mealie stores "no rating" as either null or 0."""
    return value or None


def user_ratings(mealie: MealieFetcher) -> Dict[str, Any]:
    """Fetch the caller's own ratings once, keyed by lowercase recipe id."""
    data = mealie.get_current_user_ratings()
    ratings = data.get("ratings") if isinstance(data, dict) else None
    if not isinstance(ratings, list):
        raise ValueError("Mealie returned an unexpected ratings list")
    return {
        str(entry["recipeId"]).lower(): _rating(entry.get("rating"))
        for entry in ratings
        if isinstance(entry, dict) and entry.get("recipeId") and _rating(entry.get("rating"))
    }


def _user_rating(recipe: Dict[str, Any], ratings: Dict[str, Any]) -> Any:
    return ratings.get(str(recipe.get("id")).lower())


def _concise_recipe(
    item: Dict[str, Any],
    households: Dict[str, Dict[str, Any]],
    ratings: Dict[str, Any],
) -> Dict[str, Any]:
    """Reduce a recipe summary to the fields needed to pick recipes.

    Full tag, category, and tool objects make a page of recipes very large, so
    only the id, name, slug, tag names, total time, ratings, and the slug of
    the owning household are kept.
    """
    summary = {
        "id": item.get("id"),
        "name": item.get("name"),
        "slug": item.get("slug"),
        "tags": [tag.get("name") for tag in item.get("tags") or []],
    }
    if item.get("totalTime") is not None:
        summary["totalTime"] = item["totalTime"]
    summary["rating"] = _rating(item.get("rating"))
    summary["userRating"] = _user_rating(item, ratings)
    household_id = item.get("householdId")
    if household_id:
        household = households.get(str(household_id).lower()) or {}
        summary["household"] = household.get("slug") or household_id
    return summary


def _full_recipe(item: Dict[str, Any], ratings: Dict[str, Any]) -> Dict[str, Any]:
    """Keep a full summary, minus the always-zero nested recipeCount.

    Mealie's summary schema reuses its organizer models, whose recipeCount is
    only filled in on the organizer endpoints and reads 0 here.
    """
    recipe = dict(item)
    for key in ("tags", "recipeCategory", "tools"):
        if isinstance(recipe.get(key), list):
            recipe[key] = [
                {k: v for k, v in org.items() if k != "recipeCount"}
                if isinstance(org, dict)
                else org
                for org in recipe[key]
            ]
    recipe["rating"] = _rating(recipe.get("rating"))
    recipe["userRating"] = _user_rating(recipe, ratings)
    return recipe


def recipe_items(
    mealie: MealieFetcher,
    items: List[Dict[str, Any]],
    concise: bool,
    households: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Shape recipe summaries the way the recipe list tools return them.

    The ratings and household lookups are skipped for empty lists, and the
    household lookup is reused when the caller already fetched it.
    """
    if not items:
        return []
    ratings = user_ratings(mealie)
    if not concise:
        return [_full_recipe(item, ratings) for item in items]
    if households is None:
        households = _households_by_id(mealie)
    return [_concise_recipe(item, households, ratings) for item in items]


def recipe_page_result(
    mealie: MealieFetcher,
    page: Any,
    concise: bool,
    households: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Shape a recipe list page the way get_recipes returns it."""
    return page_result(
        page, recipe_items(mealie, _page_items(page), concise, households)
    )


def _uuid(value: Any, label: str) -> str:
    """Only well-formed UUIDs are interpolated into a query filter."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ValueError(f"Mealie returned an invalid {label} id") from None


def _favorites_filter(user_ids: List[str]) -> str:
    """Mealie's own favorites page filters recipes on favoritedBy.id."""
    quoted = [f'"{user_id}"' for user_id in user_ids]
    if len(quoted) == 1:
        return f"favoritedBy.id = {quoted[0]}"
    return f"favoritedBy.id IN [{', '.join(quoted)}]"


def _household_members(mealie: MealieFetcher) -> List[Dict[str, Any]]:
    """The caller's household members (the caller included), by username.

    /api/households/members needs the manage permission, so the group's
    member list is narrowed to the caller's household instead.
    """
    own = mealie.get_current_user().get("householdId")
    if not own:
        raise ValueError("Mealie did not report your household")
    page = mealie.get_group_members()
    items = page.get("items") if isinstance(page, dict) else None
    if not isinstance(items, list):
        raise ValueError("Mealie returned an unexpected member list")
    members = [
        member
        for member in items
        if isinstance(member, dict)
        and str(member.get("householdId") or "").lower() == str(own).lower()
    ]
    return sorted(members, key=lambda member: str(member.get("username") or ""))


def _household_favorites(
    mealie: MealieFetcher, household_ids: Optional[List[str]]
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]], List[str]]:
    """Collect each household member's favorites with one request per member.

    Returns the merged recipes by id, the usernames that favorited each id,
    and the ids of the members that have at least one favorite.
    """
    recipes: Dict[str, Dict[str, Any]] = {}
    favorited_by: Dict[str, List[str]] = {}
    fans: List[str] = []
    for member in _household_members(mealie):
        member_id = _uuid(member.get("id"), "user")
        page = mealie.get_recipes(
            query_filter=_favorites_filter([member_id]),
            per_page=-1,
            households=household_ids,
        )
        items = _page_items(page)
        if items:
            fans.append(member_id)
        for item in items:
            recipe_id = str(item.get("id"))
            recipes.setdefault(recipe_id, item)
            favorited_by.setdefault(recipe_id, []).append(str(member.get("username")))
    return recipes, favorited_by, fans


def register_recipe_tools(mcp: FastMCP, mealie: MealieFetcher) -> None:
    """Register all recipe-related tools with the MCP server."""

    @mcp.tool()
    def get_recipes(
        search: Optional[str] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        categories: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        require_all_tags: Optional[bool] = None,
        require_all_categories: Optional[bool] = None,
        households: Optional[List[str]] = None,
        concise: bool = True,
        order_by: Optional[str] = None,
        order_direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Provides a paginated list of recipes with optional filtering and sorting.

        Recipes are shared across every household in the Mealie group, so
        without a households filter the results include all households' recipes.
        When the user says "my household", "my recipes", "our recipes", or
        similar, pass households=["self"] to limit results to the caller's own
        household. Meal plans and shopping lists are already scoped to the
        caller's household and need no filter.

        IMPORTANT: When filtering by tags or categories, you MUST use slugs or UUIDs, NOT display names!
        - ✅ Correct: tags=["quick-meals", "vegetarian"]
        - ❌ Wrong: tags=["Quick Meals", "Vegetarian"]

        Use get_tags() or get_categories() first to find the correct slugs.

        Args:
            search: Filters recipes by name, description, or ingredients.
                Without order_by, matches are ordered by relevance (closest
                name match first); an explicit order_by sorts them instead.
            page: Page number for pagination.
            per_page: Number of items per page.
            categories: Filter by category SLUGS (e.g., ["breakfast", "dinner"]).
            tags: Filter by tag SLUGS or UUIDs (e.g., ["quick", "healthy"]).
            require_all_tags: If True, recipe must have ALL specified tags (AND). Default False (OR).
            require_all_categories: If True, recipe must have ALL specified categories (AND).
            households: Filter by household ids or slugs (OR). Use "self" for the
                caller's own household. Unknown households raise an error.
            concise: Return only id, name, slug, tag names, totalTime, rating,
                userRating, and the owning household's slug per recipe
                (default). Tag names are not filter slugs. Set False for the
                full Mealie recipe summaries including tag, category, and tool
                objects, plus userRating.
            order_by: name (default unless searching), rating, date_added, or
                last_made. order_by="rating" sorts by your own rating where
                you set one, otherwise by the recipe rating; unrated recipes
                sort last. last_made uses your household's last-made date;
                never-made recipes sort last.
            order_direction: asc or desc. Defaults to asc for name and desc
                for rating, date_added, and last_made.

        Returns:
            Dict[str, Any]: page, perPage, total, totalPages, and items. Each
                item's rating is the recipe-level rating (null when unrated);
                userRating is the caller's own rating, or null.
        """
        with tool_error_boundary("Error fetching recipes"):
            order = recipe_order(order_by, order_direction, search)
            known_households = None
            household_ids = None
            if households:
                known_households = _households_by_id(mealie)
                household_ids = _resolve_households(
                    mealie, households, known_households
                )
            result = list_recipes(
                mealie,
                search=search,
                order=order,
                page=page,
                per_page=per_page,
                categories=categories,
                tags=tags,
                require_all_tags=require_all_tags,
                require_all_categories=require_all_categories,
                households=household_ids,
            )
            return recipe_page_result(mealie, result, concise, known_households)

    @mcp.tool()
    def get_favorite_recipes(
        page: int = 1,
        per_page: int = 50,
        households: Optional[List[str]] = None,
        concise: bool = True,
        scope: str = "user",
        order_by: Optional[str] = None,
        order_direction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List favorite recipes: the caller's own, or their whole household's.

        Read-only. Favorites are per user; get_recipes does not know about them.
        Favorites whose recipe was deleted are left out.

        - scope="user" (default): the caller's own favorites. Use for
          "my favorites", "favorite recipes", or recipes they starred. Sorted
          by name unless order_by is given.
        - scope="household": every recipe favorited by at least one member of
          the caller's household (the caller included), merged without
          duplicates. Use for "our favorites", "my family's favorites", or
          household meal planning. Each item adds favoritedBy (the usernames
          of the members who favorited it) and favoriteCount (how many did).
          Sorted by favoriteCount, most first, then name, unless order_by is
          given. Pagination runs over the full merged list.

        Args:
            page: Page number, starting at 1.
            per_page: Number of recipes per page (at least 1).
            households: Keep only favorites owned by these household ids or
                slugs (OR), in either scope. Use "self" for the caller's own
                household. Unknown households raise an error.
            concise: Return only id, name, slug, tag names, totalTime, rating,
                userRating, and the owning household's slug per recipe
                (default). Set False for the full Mealie recipe summaries
                including tag, category, and tool objects, plus userRating.
            scope: "user" or "household".
            order_by: name, rating, date_added, or last_made. order_by="rating"
                sorts by your own rating where you set one, otherwise by the
                recipe rating; unrated recipes sort last. last_made uses your
                household's last-made date; never-made recipes sort last.
            order_direction: asc or desc. Defaults to asc for name and desc
                for rating, date_added, and last_made.

        Returns:
            Dict[str, Any]: page, perPage, total, totalPages, and items. Each
                item's rating is the recipe-level rating (null when unrated);
                userRating is the caller's own rating, or null.
        """
        with tool_error_boundary("Error fetching favorite recipes"):
            if page < 1:
                raise ValueError("page must be at least 1")
            if per_page < 1:
                raise ValueError("per_page must be at least 1")
            scope_key = scope.strip().lower()
            if scope_key not in ("user", "household"):
                raise ValueError(f"Unknown scope: {scope}. Use user or household")
            explicit_order = order_by is not None
            # Without a search there is always an order.
            order = recipe_order(order_by, order_direction, None) or {}
            known_households = None
            household_ids = None
            if households:
                known_households = _households_by_id(mealie)
                household_ids = _resolve_households(
                    mealie, households, known_households
                )
            if scope_key == "user":
                own_id = _uuid(mealie.get_current_user().get("id"), "user")
                result = mealie.get_recipes(
                    query_filter=_favorites_filter([own_id]),
                    page=page,
                    per_page=per_page,
                    households=household_ids,
                    **order,
                )
                return recipe_page_result(mealie, result, concise, known_households)

            recipes, favorited_by, fans = _household_favorites(mealie, household_ids)
            if explicit_order and fans:
                # Mealie sorts and pages the merged favorites itself, so the
                # rating and last-made rules match get_recipes exactly.
                result = mealie.get_recipes(
                    query_filter=_favorites_filter(fans),
                    page=page,
                    per_page=per_page,
                    households=household_ids,
                    **order,
                )
            else:
                merged = sorted(
                    recipes.values(),
                    key=lambda recipe: (
                        -len(favorited_by[str(recipe.get("id"))]),
                        str(recipe.get("name") or "").casefold(),
                        str(recipe.get("id")),
                    ),
                )
                result = paginate(merged, page, per_page)
            page_items = _page_items(result)
            items = recipe_items(mealie, page_items, concise, known_households)
            for source, item in zip(page_items, items):
                fans_of = favorited_by.get(str(source.get("id")), [])
                item["favoritedBy"] = fans_of
                item["favoriteCount"] = len(fans_of)
            return page_result(result, items)

    @mcp.tool()
    def get_recipe(slug: str, concise: bool = False) -> Dict[str, Any]:
        """Retrieve a recipe, optionally returning only a concise summary.

        Args:
            slug: The unique text identifier for the recipe, typically found in recipe URLs
                or from get_recipes results.
            concise: Return essential fields for meal planning instead of full
                ingredients, instructions, nutrition, notes, and metadata.

        Returns:
            Dict[str, Any]: Full recipe details by default, or a concise summary.
                Both include rating (the recipe-level rating, null when unrated)
                and userRating (the caller's own rating, or null).
        """
        with tool_error_boundary("Error fetching recipe"):
            recipe_json = mealie.get_recipe(slug)
            user_rating = _user_rating(recipe_json, user_ratings(mealie))
            if not concise:
                return {**recipe_json, "userRating": user_rating}
            recipe = Recipe.model_validate(recipe_json)
            summary = recipe.model_dump(
                include={
                    "name",
                    "slug",
                    "recipeServings",
                    "recipeYieldQuantity",
                    "recipeYield",
                    "totalTime",
                    "orgURL",
                    "tags",
                    "tools",
                    "recipeIngredient",
                    "lastMade",
                },
                exclude_none=True,
            )
            return {
                **summary,
                "rating": _rating(recipe.rating),
                "userRating": user_rating,
            }

    @mcp.tool()
    def import_recipe_from_url(
        url: str, include_tags: bool = False
    ) -> Dict[str, Any]:
        """Import a recipe into Mealie by scraping a URL.

        Prefer this for ordinary recipe webpages.

        Uses Mealie's server-side scraper (the `recipe-scrapers` library), which
        has built-in adapters for many recipe sites and falls back to
        JSON-LD/Schema.org parsing for sites without a dedicated adapter.
        Mealie may fall back to AI when configured. Coverage varies by Mealie
        version; URLs Mealie cannot parse return an error.

        The created recipe is fetched and returned so the caller can verify the
        scrape result. Some sources (notably paywalled URLs that redirect) can
        cause the scraper to land on the wrong page and silently return the
        wrong recipe — always confirm the returned `name` matches what was
        requested.

        Args:
            url: Source URL of the recipe to import.
            include_tags: If True, import tags Mealie extracts from the source.
                Defaults to False.

        Returns:
            Dict[str, Any]: The created recipe, including slug, name,
            ingredients, and instructions.

        If fetching the imported recipe fails, the error includes created_slug
        and stage so the recipe can be retrieved without importing it again.
        """
        with tool_error_boundary("Error importing recipe from URL"):
            slug = mealie.import_recipe_from_url(url, include_tags=include_tags)
            with _created_recipe_stage(slug, "fetch"):
                return mealie.get_recipe(slug)

    @mcp.tool()
    def create_recipe(
        name: str,
        instructions: List[Union[str, RecipeInstructionInput]],
        description: Optional[str] = None,
        org_url: Optional[str] = None,
        total_time: Optional[str] = None,
        prep_time: Optional[str] = None,
        cook_time: Optional[str] = None,
        perform_time: Optional[str] = None,
        recipe_yield: Optional[str] = None,
        servings: Optional[float] = None,
        image_url: Optional[str] = None,
        ingredients: Optional[List[Union[str, RecipeIngredientInput]]] = None,
        tags: Optional[List[OrganizerRef]] = None,
        tools: Optional[List[OrganizerRef]] = None,
        nutrition: Optional[RecipeNutrition] = None,
        settings: Optional[RecipeSettingsInput] = None,
    ) -> Dict[str, Any]:
        """Create a recipe and populate all of its content in one call.

        Use when the recipe is already composed into ingredients and instructions.
        name and instructions are required; pass instructions=[] for a recipe
        that intentionally has no steps (Mealie's placeholder step is always
        replaced). At least one ingredient or instruction must be given. Include
        metadata, nutrition, and display settings for a complete recipe.
        Omitted or null fields keep Mealie's defaults. An image_url is scraped
        after content is saved.

        Ingredients and instructions each accept either a plain string or a
        structured object:

        - An ingredient string (e.g. "200 g basmati rice") is resolved by
          Mealie's ingredient parser into quantity and an existing unit and
          food. Decimal commas ("0,25 tl") are read as decimal points. When no
          existing food matches, quantity and unit are kept and the unmatched
          text goes into the note; such lines are listed in
          unmatched_ingredients. To get a suggested food per line first, run
          the strings through parse_ingredients (see its suggestion field)
          and pass the confirmed results here as structured ingredients.
        - An ingredient object can set quantity, note, title, an existing
          Mealie unit/food (by id and name), and a referenceId for step links.
        - An instruction string is the step text; an instruction object can
          also carry a summary (heading shown in place of "Step N"), a title
          (section banner above the step), and ingredientReferences for
          cook-mode highlights.

        Args:
            name: The name of the new recipe.
            description: Recipe description.
            org_url: Source URL for the recipe (shown as a link in the Mealie UI).
            total_time: Total time as free text, e.g. "35 min" or ISO-8601 "PT35M".
            prep_time: Preparation time, same format as total_time.
            cook_time: Cooking time, same format as total_time.
            perform_time: Hands-on/active time, same format as total_time.
            recipe_yield: Yield as free text, e.g. "4 Portionen".
            servings: Number of servings (numeric).
            image_url: URL of an image to scrape and set as the recipe image.
            instructions: Instruction strings and/or structured instruction
                objects; [] means the recipe intentionally has no steps.
            ingredients: Ingredient strings and/or structured ingredient objects.
            tags: Existing Mealie tags (id+name) to assign; look up with get_tags.
            tools: Existing Mealie tools (id+name) to assign; look up with get_tools.
            nutrition: Per-serving nutrition values (calories in kcal, sodium and
                cholesterol in mg, the rest in grams). Omitted keys stay empty.
            settings: Display toggles to override on the new recipe. Only the
                toggles you pass are changed; the rest keep the defaults Mealie
                seeds from the household preferences. Set showAssets here when
                you know you are about to attach a file.

        Returns:
            Dict[str, Any]: The created recipe details. When some ingredient
            strings did not match an existing food (or unit), the result also has
            unmatched_ingredients: a list of {index, input, food, unit} giving the
            ingredient position, the original line, and the unmatched text.
            Look them up with resolve_ingredients, then update_recipe.

        If a later API step fails, the error includes created_slug and stage.
        Inspect that recipe and resume the failed step instead of creating again.
        """
        with tool_error_boundary("Error creating recipe"):
            _require_text(name, "Recipe name")
            if image_url is not None:
                _require_text(image_url, "Image URL")
            if not ingredients and not instructions:
                raise ValueError("Provide at least one ingredient or instruction")
            changes, unmatched = _parsed_recipe_changes(
                mealie,
                description=description,
                org_url=org_url,
                total_time=total_time,
                prep_time=prep_time,
                cook_time=cook_time,
                perform_time=perform_time,
                recipe_yield=recipe_yield,
                servings=servings,
                ingredients=ingredients,
                instructions=instructions,
                tags=tags,
                tools=tools,
                nutrition=nutrition,
                settings=settings,
            )
            return _with_unmatched(
                _create_populated_recipe(mealie, name, changes, image_url), unmatched
            )

    @mcp.tool()
    def update_recipe(
        slug: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        recipe_yield: Optional[str] = None,
        servings: Optional[float] = None,
        total_time: Optional[str] = None,
        prep_time: Optional[str] = None,
        cook_time: Optional[str] = None,
        perform_time: Optional[str] = None,
        org_url: Optional[str] = None,
        tags: Optional[List[OrganizerRef]] = None,
        tools: Optional[List[OrganizerRef]] = None,
        nutrition: Optional[RecipeNutrition] = None,
        settings: Optional[RecipeSettingsInput] = None,
        ingredients: Optional[List[Union[str, RecipeIngredientInput]]] = None,
        instructions: Optional[List[Union[str, RecipeInstructionInput]]] = None,
    ) -> Dict[str, Any]:
        """Partially update a recipe (only updates provided fields).

        Omitted or null fields are unchanged. Provided ingredient/instruction
        lists replace those fields; empty lists clear them. Content changes use
        a read-merge-write to preserve other recipe fields; metadata-only changes
        use PATCH.

        Args:
            slug: The unique text identifier for the recipe to be updated.
            name: New name for the recipe.
            description: New description for the recipe.
            recipe_yield: Yield as free text, e.g. "4 Portionen".
            servings: Number of servings (numeric).
            total_time: Total time as free text, e.g. "35 min" or ISO-8601 "PT35M".
            prep_time: Preparation time, same format as total_time.
            cook_time: Cooking time, same format as total_time.
            perform_time: Hands-on/active time, same format as total_time.
            org_url: Source URL for the recipe (shown as a link in the Mealie UI).
            tags: Existing Mealie tags (id+name) to set; look up with get_tags.
            tools: Existing Mealie tools (id+name) to set; look up with get_tools.
            nutrition: Per-serving nutrition values (calories in kcal, sodium and
                cholesterol in mg, the rest in grams). Mealie replaces the whole
                nutrition object, so pass every value you want to keep — omitted
                keys are cleared, not preserved.
            settings: Display toggles to change, e.g. showAssets to make an
                uploaded asset visible in the UI, or showNutrition to reveal
                stored nutrition. Only the toggles you pass are changed: the
                current settings are read first and merged, because Mealie does
                not reliably preserve toggles left out of a settings PATCH.
            ingredients: Ingredient strings and/or structured objects, as in
                create_recipe (strings are parsed the same way). Replaces the
                ingredient list when provided.
            instructions: Step strings and/or structured objects, as in
                create_recipe. Replaces the instruction list when provided.

        Returns:
            Dict[str, Any]: The updated recipe details, plus
            unmatched_ingredients as in create_recipe when some ingredient
            strings did not match an existing food or unit.
        """
        with tool_error_boundary("Error updating recipe"):
            recipe_data, unmatched = _parsed_recipe_changes(
                mealie,
                name=name,
                description=description,
                recipe_yield=recipe_yield,
                servings=servings,
                total_time=total_time,
                prep_time=prep_time,
                cook_time=cook_time,
                perform_time=perform_time,
                org_url=org_url,
                tags=tags,
                tools=tools,
                nutrition=nutrition,
                settings=settings,
                ingredients=ingredients,
                instructions=instructions,
            )
            if not recipe_data:
                raise ValueError("At least one field must be provided to update")
            if ingredients is not None or instructions is not None:
                current = mealie.get_recipe(slug)
                return _with_unmatched(
                    mealie.update_recipe(slug, _compose_recipe(current, recipe_data)),
                    unmatched,
                )
            if settings is not None:
                # Mealie drops some toggles omitted from a settings PATCH, so
                # send the complete object built from the recipe's current one
                current = mealie.get_recipe(slug).get("settings") or {}
                recipe_data["settings"] = {
                    **current,
                    **settings.model_dump(exclude_none=True),
                }

            return mealie.patch_recipe(slug, recipe_data)

    @mcp.tool()
    def duplicate_recipe(slug: str, name: Optional[str] = None) -> Dict[str, Any]:
        """Duplicate an existing recipe, creating a copy with a new slug.

        Args:
            slug: The unique text identifier for the recipe to duplicate.
            name: Optional new name for the duplicate (if not provided, uses original name with copy indicator).

        Returns:
            Dict[str, Any]: The newly created duplicate recipe details.
        """
        with tool_error_boundary("Error duplicating recipe"):
            return mealie.duplicate_recipe(slug, name)

    @mcp.tool()
    def mark_recipe_last_made(slug: str) -> Dict[str, Any]:
        """Mark a recipe as having been made today (updates last made timestamp).

        Args:
            slug: The unique text identifier for the recipe.

        Returns:
            Dict[str, Any]: The updated recipe details.
        """
        with tool_error_boundary("Error updating recipe last made"):
            return mealie.update_recipe_last_made(slug)

    @mcp.tool()
    def set_recipe_image_from_url(slug: str, image_url: str) -> Dict[str, Any]:
        """Set a recipe's image by scraping it from a URL.

        Args:
            slug: The unique text identifier for the recipe.
            image_url: URL of the image to scrape and use as the recipe image.

        Returns:
            Dict[str, Any]: Confirmation that the image was set.
        """
        with tool_error_boundary("Error setting recipe image from URL"):
            return mealie.scrape_recipe_image_from_url(slug, image_url)

    @mcp.tool()
    def upload_recipe_image_file(
        slug: str, image_path: str, extension: Optional[str] = None
    ) -> Dict[str, Any]:
        """Upload an image file for a recipe.

        Attach a photo to an existing recipe; this does not extract recipe text
        from the image or create a new recipe.
        Args:
            slug: The unique text identifier for the recipe.
            image_path: Local file path to the image to upload.
            extension: Image extension such as "jpg". Derived from image_path when omitted.

        Returns:
            Dict[str, Any]: Confirmation that the image was uploaded.
        """
        with tool_error_boundary("Error uploading recipe image"):
            import os

            if not os.path.exists(image_path):
                raise ValueError("Image file not found")

            with open(image_path, "rb") as f:
                image_data = f.read()

            filename = os.path.basename(image_path)
            return mealie.upload_recipe_image(slug, image_data, filename, extension)

    @mcp.tool()
    def upload_recipe_asset_file(
        slug: str,
        asset_path: str,
        name: Optional[str] = None,
        icon: Optional[str] = None,
        extension: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload an asset file (document, PDF, etc.) for a recipe.

        Args:
            slug: The unique text identifier for the recipe.
            asset_path: Local file path to the asset to upload.
            name: Display name for the asset. Derived from the file name when omitted.
            icon: Material Design icon name, for example "mdi-file-pdf-box".
            extension: Asset extension such as "pdf". Derived from asset_path when omitted.

        Returns:
            Dict[str, Any]: Details of the uploaded asset.
        """
        with tool_error_boundary("Error uploading recipe asset"):
            import os

            if not os.path.exists(asset_path):
                raise ValueError("Asset file not found")

            with open(asset_path, "rb") as f:
                asset_data = f.read()

            filename = os.path.basename(asset_path)
            return mealie.upload_recipe_asset(
                slug, asset_data, filename, name=name, icon=icon, extension=extension
            )

    @mcp.tool()
    def update_recipe_categories_and_tags(
        slug: str,
        category_ids: Optional[List[str]] = None,
        tag_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Update categories and/or tags for a recipe in a single API call.
        Use get_categories() and get_tags() first to find valid IDs.
        Passing an empty list for either field will clear it; omitting (None) leaves it unchanged.

        Args:
            slug: The unique text identifier for the recipe.
            category_ids: List of category UUIDs to assign (omit to leave unchanged).
            tag_ids: List of tag UUIDs to assign (omit to leave unchanged).

        Returns:
            Dict[str, Any]: The updated recipe details.
        """
        with tool_error_boundary("Error updating recipe categories and tags"):
            return mealie.set_recipe_categories_and_tags(slug, category_ids=category_ids, tag_ids=tag_ids)

    @mcp.tool()
    def delete_recipe(slug: str) -> Dict[str, Any]:
        """Delete a recipe permanently.

        Args:
            slug: The unique text identifier for the recipe to delete.

        Returns:
            Dict[str, Any]: Confirmation of deletion.
        """
        with tool_error_boundary("Error deleting recipe"):
            return mealie.delete_recipe(slug)
