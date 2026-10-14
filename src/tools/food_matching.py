"""Match free food text against Mealie's food catalogue.

Shared by parse_ingredients (suggestions for lines the parser left without a
food) and resolve_ingredients (suggest, then alias or create). The catalogue is
loaded once per tool call and matched here rather than with Mealie's search,
which only finds substrings: "laurierbladeren" would never find "laurierblad".
Catching such near-duplicates before a food is created matters because
duplicate foods break shopping-list merging.
"""

import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

import httpx

from identity import CredentialError
from mealie import MealieFetcher
from mealie.client import MealieApiError

MAX_CANDIDATES = 5
# Similarity at or above this suggests an alias (plural forms, small typos).
ALIAS_THRESHOLD = 0.85
# A word ending in a food name ("bleekselderij" ends in "selderij") scores at
# least the alias threshold: it is a candidate worth an alias suggestion, never
# an automatic use, because the prefix may change the ingredient (knolselderij).
COMPOUND_SCORE = ALIAS_THRESHOLD
# Shorter names end too many unrelated words to count as a compound suffix.
MIN_COMPOUND_SUFFIX = 3
PERMISSION_MESSAGE = (
    "Mealie denied the change (HTTP 403). Creating foods or aliases needs the "
    "'can organize group data' permission; ask the admin to grant it."
)
# Failures a per-item lookup or write reports instead of failing the whole call.
ITEM_ERRORS = (
    CredentialError,
    MealieApiError,
    httpx.HTTPError,
    TimeoutError,
    ConnectionError,
    ValueError,
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


def food_ref(food: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": food.get("id"), "name": food.get("name")}


def _similarity(name: str, text: str) -> float:
    matcher = SequenceMatcher(None, name, text)
    # quick_ratio is a cheap upper bound; skip the full comparison when it
    # cannot reach the candidate threshold.
    if matcher.quick_ratio() < 0.6:
        return 0.0
    return matcher.ratio()


def _match(food: Dict[str, Any], text: str) -> Optional[Dict[str, Any]]:
    """Score one food against the text; None when it is not a plausible match.

    Each food name is compared with the whole text and with every run of as
    many words as the name has, so "stengel bleekseldery" still finds
    "bleekselderij". The words outside the best run are returned as leftover.
    """
    tokens = text.split()
    words = [_normalize(t) for t in tokens]
    target = " ".join(words)
    best: Optional[Dict[str, Any]] = None

    def offer(score: float, match: str, start: int, length: int) -> None:
        nonlocal best
        if best is None or score > best["score"]:
            best = {
                "score": score,
                "match": match,
                "matched": tokens[start : start + length],
                "leftover": tokens[:start] + tokens[start + length :],
            }

    for name in _food_names(food):
        if name == target:
            return {"score": 1.0, "match": "exact", "matched": tokens, "leftover": []}
        name_words = name.split()
        size = len(name_words)
        for start in range(len(words) - size + 1):
            window = words[start : start + size]
            if window == name_words:
                # Prefer the longest food name contained in the text.
                offer(min(0.9 + 0.01 * size, 0.99), "word", start, size)
                continue
            window_text = " ".join(window)
            if (
                size == 1
                and len(name) >= MIN_COMPOUND_SUFFIX
                and window_text.endswith(name)
            ):
                # Dutch compounds put the head noun last: the more of the word
                # the food name covers, the closer the match.
                offer(
                    COMPOUND_SCORE + 0.05 * len(name) / len(window_text),
                    "compound",
                    start,
                    1,
                )
            ratio = _similarity(name, window_text)
            if ratio >= 0.6:
                offer(ratio, "similar", start, size)
        if size != len(words):
            ratio = _similarity(name, target)
            if ratio >= 0.6:
                offer(ratio, "similar", 0, len(tokens))
    return best


def load_foods(mealie: MealieFetcher) -> List[Dict[str, Any]]:
    """Fetch the whole food catalogue in one request."""
    response = mealie.get_foods(per_page=-1)
    items = response.get("items") if isinstance(response, dict) else None
    if not isinstance(items, list):
        raise ValueError("Mealie returned an unexpected food list")
    return [i for i in items if isinstance(i, dict) and i.get("id")]


def exact_food(foods: List[Dict[str, Any]], text: str) -> Optional[Dict[str, Any]]:
    """Return a food whose name, plural, or alias equals the text."""
    target = _normalize(text)
    return next((food for food in foods if target in _food_names(food)), None)


def suggest(foods: List[Dict[str, Any]], text: str) -> Dict[str, Any]:
    """Rank candidate foods for the text and suggest what to do with it."""
    scored = []
    for food in foods:
        match = _match(food, text)
        if match:
            scored.append((match, food))
    scored.sort(key=lambda pair: pair[0]["score"], reverse=True)
    candidates = [
        {**food_ref(food), "score": round(match["score"], 2), "match": match["match"]}
        for match, food in scored[:MAX_CANDIDATES]
    ]
    if not scored:
        suggestion = {"action": "create", "name": text}
    else:
        match, food = scored[0]
        if match["match"] == "exact":
            suggestion = {"action": "use", "food": food_ref(food)}
        elif match["match"] == "word":
            suggestion = {
                "action": "use_with_note",
                "food": food_ref(food),
                "note": " ".join(match["leftover"]),
            }
        elif match["score"] >= ALIAS_THRESHOLD:
            suggestion = {
                "action": "alias",
                "food": food_ref(food),
                "alias": " ".join(match["matched"]),
            }
            if match["leftover"]:
                suggestion["note"] = " ".join(match["leftover"])
        else:
            suggestion = {"action": "create", "name": text}
    return {"status": "suggested", "candidates": candidates, "suggestion": suggestion}


def failure_message(error: Exception) -> str:
    """Describe a per-item failure without echoing request or response bodies."""
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
