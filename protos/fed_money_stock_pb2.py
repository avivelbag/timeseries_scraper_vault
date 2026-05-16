# Hand-written stub for protos/fed_money_stock.proto.
# Mirrors the proto field names so the scraper can assign attributes
# without importing protoc-generated code.
from dataclasses import dataclass


@dataclass
class FedMoneyStockRecord:
    """Schema stub matching FedMoneyStockRecord in fed_money_stock.proto."""

    series_date: str = ""
    m1_seasonally_adjusted_billions: float = 0.0
    m2_seasonally_adjusted_billions: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
