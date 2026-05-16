# Hand-written stub for protos/aar_weekly_rail_traffic.proto.
from dataclasses import dataclass


@dataclass
class AarWeeklyRailTrafficRecord:
    """Schema stub matching AarWeeklyRailTrafficRecord in aar_weekly_rail_traffic.proto."""

    week_ending_date: str = ""
    commodity_group: str = ""
    carloads: int = 0
    carloads_yoy_pct: float = 0.0
    intermodal_units: int = 0
    intermodal_yoy_pct: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
