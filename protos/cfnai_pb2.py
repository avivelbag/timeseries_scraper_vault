# Hand-written stub for protos/cfnai.proto.
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CfnaiRecord:
    """Schema stub matching CfnaiRecord in cfnai.proto."""

    series_date: str = ""
    cfnai: Optional[float] = None
    cfnai_ma3: Optional[float] = None
    production_and_income: Optional[float] = None
    employment_unemployment_hours: Optional[float] = None
    personal_consumption_housing: Optional[float] = None
    sales_orders_inventories: Optional[float] = None
    source_url: str = ""
    fetch_time: str = ""
