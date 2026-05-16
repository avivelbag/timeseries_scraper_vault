# Hand-written stub for protos/nsidc_sea_ice.proto.
# Mirrors the proto field names so the scraper can assign attributes
# without importing protoc-generated code.
from dataclasses import dataclass, field
from datetime import datetime


class _Timestamp:
    """Minimal stand-in for google.protobuf.Timestamp supporting FromDatetime."""

    def __init__(self) -> None:
        self._dt: datetime | None = None

    def FromDatetime(self, dt: datetime) -> None:
        self._dt = dt


@dataclass
class NsidcSeaIceRecord:
    """Schema stub matching NsidcSeaIceRecord in nsidc_sea_ice.proto."""

    year: int = 0
    month: int = 0
    extent_million_sq_km: float = 0.0
    area_million_sq_km: float = 0.0
    source_url: str = ""
    fetch_time: _Timestamp = field(default_factory=_Timestamp)
