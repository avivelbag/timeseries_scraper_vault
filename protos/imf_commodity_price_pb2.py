from dataclasses import dataclass


@dataclass
class ImfCommodityPrice:
    """Schema stub matching ImfCommodityPrice in imf_commodity_price.proto."""

    commodity_name: str = ""
    commodity_code: str = ""
    date: str = ""
    price_usd: float = 0.0
    units: str = ""
    source_url: str = ""
    fetch_time: str = ""
