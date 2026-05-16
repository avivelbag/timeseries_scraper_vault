# Hand-written stub for protos/bls_ppi.proto.
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
class BLSPpiRecord:
    """Schema stub matching BLSPpiRecord in bls_ppi.proto."""

    series_id: str = ""
    commodity_description: str = ""
    period: str = ""
    index_value: float = 0.0
    preliminary: bool = False
    percent_change_1m: float = 0.0
    percent_change_12m: float = 0.0
    source_url: str = ""
    fetch_time: _Timestamp = field(default_factory=_Timestamp)
