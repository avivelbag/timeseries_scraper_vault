# Hand-written stub for protos/fed_z1_financial_accounts.proto.
from dataclasses import dataclass


@dataclass
class FedZ1Record:
    """Schema stub matching FedZ1Record in fed_z1_financial_accounts.proto."""

    period_date: str = ""
    series_name: str = ""
    value_billions_usd: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
