from typing import Literal, Optional

from pydantic import BaseModel, Field


class IngredientResolution(BaseModel):
    """One unmatched food text and what resolve_ingredients should do with it."""

    text: str = Field(
        description=(
            "Food text the parser could not match, e.g. the food value from "
            "create_recipe's unmatched_ingredients."
        )
    )
    action: Literal["suggest", "alias", "create"] = Field(
        default="suggest",
        description=(
            "suggest: look up candidate foods without writing anything. "
            "alias: add an alias to the existing food food_id. "
            "create: create a new food."
        ),
    )
    food_id: Optional[str] = Field(
        default=None, description="alias only: UUID of the existing food."
    )
    alias: Optional[str] = Field(
        default=None,
        description="alias only: alias to add; defaults to text.",
    )
    name: Optional[str] = Field(
        default=None,
        description="create only: name of the new food; defaults to text.",
    )
