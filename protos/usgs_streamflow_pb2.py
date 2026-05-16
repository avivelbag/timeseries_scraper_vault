# Hand-written stub for protos/usgs_streamflow.proto.
# Mirrors the proto field names so _record_to_proto() can assign attributes
# without importing protoc-generated code.  upload_rows() in main() requires a
# real protobuf Message; this stub is intentionally schema documentation only.
from dataclasses import dataclass, field
from datetime import datetime


class _Timestamp:
    """Minimal stand-in for google.protobuf.Timestamp supporting FromDatetime."""

    def __init__(self) -> None:
        self._dt: datetime | None = None

    def FromDatetime(self, dt: datetime) -> None:
        self._dt = dt


@dataclass
class UsgsStreamflowRecord:
    """Schema stub matching UsgsStreamflowRecord in usgs_streamflow.proto."""

    site_no: str = ""
    site_name: str = ""
    date: str = ""
    discharge_cfs: float = 0.0
    approval_status: str = ""
    source_url: str = ""
    fetch_time: _Timestamp = field(default_factory=_Timestamp)
