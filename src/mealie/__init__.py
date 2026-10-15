from .categories import CategoriesMixin
from .client import MealieClient
from .cookbooks import CookbooksMixin
from .foods import FoodsMixin
from .group import GroupMixin
from .mealplan import MealplanMixin
from .parser import ParserMixin
from .recipe import RecipeMixin
from .shopping_list import ShoppingListMixin
from .tags import TagsMixin
from .tools import ToolsMixin
from .units import UnitsMixin
from .user import UserMixin


class MealieFetcher(
    RecipeMixin,
    CategoriesMixin,
    TagsMixin,
    CookbooksMixin,
    FoodsMixin,
    UnitsMixin,
    ToolsMixin,
    ParserMixin,
    ShoppingListMixin,
    MealplanMixin,
    UserMixin,
    GroupMixin,
    MealieClient,
):
    pass
