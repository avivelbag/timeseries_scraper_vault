# Hand-written stub for protos/fed_g17_industrial_production.proto.
from dataclasses import dataclass
from typing import Optional


@dataclass
class FedG17Record:
    """Schema stub matching FedG17Record in fed_g17_industrial_production.proto."""

    series_id: str = ""
    series_name: str = ""
    reference_date: str = ""
    index_value: float = 0.0
    capacity_utilization_pct: Optional[float] = None
    unit: str = ""
    source_url: str = ""
    fetch_time: str = ""
