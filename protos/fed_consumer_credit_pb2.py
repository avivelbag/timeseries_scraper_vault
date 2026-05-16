# Hand-written stub for protos/fed_consumer_credit.proto.
# Mirrors the proto field names so run() can assign attributes
# without importing protoc-generated code.
from dataclasses import dataclass


@dataclass
class FedConsumerCreditRecord:
    """Schema stub matching FedConsumerCreditRecord in fed_consumer_credit.proto."""

    release_date: str = ""
    series_name: str = ""
    amount_billions_usd: float = 0.0
    seasonally_adjusted: bool = False
    source_url: str = ""
    fetch_time: str = ""
