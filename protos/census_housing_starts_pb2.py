# Hand-written stub for protos/census_housing_starts.proto.
from dataclasses import dataclass


@dataclass
class CensusHousingStartsRecord:
    """Schema stub matching CensusHousingStartsRecord in census_housing_starts.proto."""

    period_date: str = ""
    region: str = ""
    structure_type: str = ""
    starts_thousands: float = 0.0
    permits_thousands: float = 0.0
    completions_thousands: float = 0.0
    under_construction_thousands: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
