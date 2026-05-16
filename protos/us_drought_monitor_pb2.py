# Hand-written stub for protos/us_drought_monitor.proto.
# Mirrors the proto field names so _record_to_proto() can assign attributes
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
class DroughtRecord:
    """Schema stub matching DroughtRecord in us_drought_monitor.proto."""

    release_date: str = ""
    region: str = ""
    d0_percent: float = 0.0
    d1_percent: float = 0.0
    d2_percent: float = 0.0
    d3_percent: float = 0.0
    d4_percent: float = 0.0
    source_url: str = ""
    fetch_time: _Timestamp = field(default_factory=_Timestamp)
