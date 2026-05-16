# Hand-written stub for protos/empire_state_manufacturing.proto.
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class EmpireStateManufacturingRecord:
    """Schema stub matching EmpireStateManufacturingRecord in empire_state_manufacturing.proto."""

    survey_date: str = ""
    general_business_conditions: Optional[float] = None
    new_orders: Optional[float] = None
    shipments: Optional[float] = None
    unfilled_orders: Optional[float] = None
    delivery_time: Optional[float] = None
    inventories: Optional[float] = None
    prices_paid: Optional[float] = None
    prices_received: Optional[float] = None
    number_of_employees: Optional[float] = None
    avg_workweek: Optional[float] = None
    capital_expenditures: Optional[float] = None
    technology_spending: Optional[float] = None
    source_url: str = ""
    fetch_time: str = ""
