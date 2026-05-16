# Hand-written stub for protos/bls_jolts.proto.
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
class BlsJoltsRecord:
    """Schema stub matching BlsJoltsRecord in bls_jolts.proto."""

    series_id: str = ""
    period: str = ""
    data_type: str = ""
    industry: str = ""
    level_thousands: float = 0.0
    rate_pct: float = 0.0
    source_url: str = ""
    fetch_time: _Timestamp = field(default_factory=_Timestamp)
