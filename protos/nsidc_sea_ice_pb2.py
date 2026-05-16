# Hand-written stub for protos/nsidc_sea_ice.proto.
# Mirrors the proto field names so the scraper can assign attributes
# without importing protoc-generated code.
from dataclasses import dataclass


@dataclass
class NsidcSeaIceRecord:
    """Schema stub matching NsidcSeaIceRecord in nsidc_sea_ice.proto."""

    year: int = 0
    month: int = 0
    extent_million_sq_km: float = 0.0
    area_million_sq_km: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
