# Hand-written stub for protos/us_bankruptcy_filings.proto.
from dataclasses import dataclass


@dataclass
class UsCourtsBankruptcyRecord:
    """Schema stub matching UsCourtsBankruptcyRecord in us_bankruptcy_filings.proto."""

    period_year: int = 0
    period_quarter: int = 0
    chapter: int = 0
    filings: int = 0
    source_url: str = ""
    fetch_time: str = ""
