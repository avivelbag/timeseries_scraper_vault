# Hand-written stub for protos/tsa_checkpoint_travel.proto.
from dataclasses import dataclass


@dataclass
class TsaCheckpointRecord:
    """Schema stub matching TsaCheckpointRecord in tsa_checkpoint_travel.proto."""

    date: str = ""
    travelers_total: int = 0
    travelers_year_ago: int = 0
    source_url: str = ""
    fetch_time: str = ""
