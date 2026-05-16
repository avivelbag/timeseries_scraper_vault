# Hand-written stub for protos/mba_mortgage_applications.proto.
from dataclasses import dataclass


@dataclass
class MortgageApplicationsRecord:
    """Schema stub matching MortgageApplicationsRecord in mba_mortgage_applications.proto."""

    week_ending_date: str = ""
    index_name: str = ""
    index_value: float = 0.0
    change_pct_week: float | None = None
    change_pct_year: float | None = None
    seasonally_adjusted: bool = False
    source_url: str = ""
    fetch_time: str = ""
