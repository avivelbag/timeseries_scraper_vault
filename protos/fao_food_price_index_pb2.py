from dataclasses import dataclass


@dataclass
class FaoFoodPriceRecord:
    """Schema stub matching FaoFoodPriceRecord in fao_food_price_index.proto."""

    date: str = ""
    commodity_group: str = ""
    index_value: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
