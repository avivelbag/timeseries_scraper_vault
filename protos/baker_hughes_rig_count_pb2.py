# Hand-written stub for protos/baker_hughes_rig_count.proto.
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RigCountRecord:
    """Schema stub matching RigCountRecord in baker_hughes_rig_count.proto."""

    report_date: str = ""
    region: str = ""
    drill_type: str = ""
    rig_count: int = 0
    week_over_week_change: int = 0
    year_ago_count: int = 0
    source_url: str = ""
    fetch_time: str = ""
