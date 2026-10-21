from dataclasses import dataclass
from decimal import Decimal

from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel


class PlatformModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True
    )


class ListingPrice(PlatformModel):
    price: Decimal = Field(ge=0, allow_inf_nan=False)


class ListingStatus(PlatformModel):
    item_status: int


class ListingData(ListingStatus):
    id: str = Field(
        min_length=1,
        pattern=r"^[0-9]+$",
        validation_alias=AliasChoices("id", AliasPath("detailParams", "itemId")),
        coerce_numbers_to_str=True,
    )
    title: str = Field(min_length=1)
    price_info: ListingPrice


class ListingCard(PlatformModel):
    card_type: int
    card_data: dict[str, JsonValue] | None = None


class ListingGroup(PlatformModel):
    group_id: int | str
    group_name: str
    default_group: bool = False
    group_type: int = 0


class ListingPageData(PlatformModel):
    card_list: list[ListingCard]
    next_page: bool
    next_page_model: JsonValue = None
    next_page_num: int | str | None = None
    item_group_list: list[ListingGroup] = Field(default_factory=list)


@dataclass(frozen=True)
class FishListing:
    item_id: str
    title: str
    price: Decimal


@dataclass(frozen=True)
class FishListingPage:
    items: tuple[FishListing, ...]
    has_more: bool
    next_page_model: JsonValue = None
    next_page_num: int | str | None = None
    group: ListingGroup | None = None
