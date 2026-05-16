# Hand-written stub for protos/cftc_cot.proto.
# Mirrors the proto field names so parse_html() can assign attributes
# without importing protoc-generated code.
from dataclasses import dataclass


@dataclass
class CotRecord:
    """Schema stub matching CotRecord in cftc_cot.proto."""

    report_date: str = ""
    commodity_name: str = ""
    cftc_contract_market_code: str = ""
    noncommercial_long: int = 0
    noncommercial_short: int = 0
    commercial_long: int = 0
    commercial_short: int = 0
    total_reportable_long: int = 0
    total_reportable_short: int = 0
    nonreportable_long: int = 0
    nonreportable_short: int = 0
    source_url: str = ""
    fetch_time: str = ""
