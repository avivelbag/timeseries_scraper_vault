# Hand-written stub for protos/fed_h8_bank_assets.proto.
# Mirrors the proto field names so the scraper can assign attributes
# without importing protoc-generated code.
from dataclasses import dataclass


@dataclass
class FedH8BankAssets:
    """Schema stub matching FedH8BankAssets in fed_h8_bank_assets.proto."""

    week_ending: str = ""
    series_label: str = ""
    value_millions_usd: float = 0.0
    seasonal_adjustment: str = ""
    source_url: str = ""
    fetch_time: str = ""
    units: str = "millions_usd"
