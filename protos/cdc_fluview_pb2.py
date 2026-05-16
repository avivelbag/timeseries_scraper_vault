# Hand-written stub for protos/cdc_fluview.proto.
# Mirrors proto field names so _record_to_proto() can assign attributes
# without importing protoc-generated code.
from dataclasses import dataclass


@dataclass
class CdcFluviewRecord:
    """Schema stub matching CdcFluviewRecord in cdc_fluview.proto."""

    week_ending_date: str = ""
    year: int = 0
    week: int = 0
    region: str = ""
    ili_percent: float = 0.0
    total_patients: int = 0
    ili_patients: int = 0
    source_url: str = ""
    fetch_time: str = ""
