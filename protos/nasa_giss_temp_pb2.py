# Hand-written stub for protos/nasa_giss_temp.proto.
# Mirrors the proto field names so parse_lines() can assign attributes
# without importing protoc-generated code.
from dataclasses import dataclass


@dataclass
class NasaGissTempRecord:
    """Schema stub matching NasaGissTempRecord in nasa_giss_temp.proto."""

    year: int = 0
    month: int = 0
    year_month: str = ""
    anomaly_c: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
