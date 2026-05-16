# Hand-written stub for protos/bls_employment_situation.proto.
from dataclasses import dataclass


@dataclass
class BLSEmploymentRecord:
    """Schema stub matching BLSEmploymentRecord in bls_employment_situation.proto."""

    period_year: int = 0
    period_month: int = 0
    series_id: str = ""
    series_label: str = ""
    value: float = 0.0
    units: str = ""
    preliminary: bool = False
    source_url: str = ""
    fetch_time: str = ""
