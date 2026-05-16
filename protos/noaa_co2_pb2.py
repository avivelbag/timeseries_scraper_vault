# Hand-written stub for protos/noaa_co2.proto.
# Mirrors the proto field names so parse_lines() can assign attributes
# without importing protoc-generated code.
from dataclasses import dataclass


@dataclass
class NoaaCo2Record:
    """Schema stub matching NoaaCo2Record in noaa_co2.proto."""

    year: int = 0
    month: int = 0
    decimal_year: float = 0.0
    monthly_avg_ppm: float = 0.0
    deseasonalized_ppm: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
