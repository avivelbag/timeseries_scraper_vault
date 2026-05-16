# Hand-written stub for protos/bea_gdp.proto.
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
class GdpRecord:
    """Schema stub matching GdpRecord in bea_gdp.proto."""

    period_date: str = ""
    component: str = ""
    value_billions_usd: float = 0.0
    pct_change_annualized: float = 0.0
    source_url: str = ""
    fetch_time: _Timestamp = field(default_factory=_Timestamp)
    schema_version: int = 1
