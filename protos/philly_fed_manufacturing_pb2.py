# Hand-written stub for protos/philly_fed_manufacturing.proto.
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class PhillyFedManufacturingRecord:
    """Schema stub matching PhillyFedManufacturingRecord in philly_fed_manufacturing.proto."""

    report_date: str = ""
    indicator_name: str = ""
    current_index: Optional[float] = None
    prior_month_index: Optional[float] = None
    six_month_forecast: Optional[float] = None
    source_url: str = ""
    fetch_time: str = ""
