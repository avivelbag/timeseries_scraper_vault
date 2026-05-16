# Hand-written stub for protos/ism_manufacturing_pmi.proto.
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class IsmManufacturingPmiRecord:
    """Schema stub matching IsmManufacturingPmiRecord in ism_manufacturing_pmi.proto."""

    report_date: str = ""
    pmi: Optional[float] = None
    new_orders: Optional[float] = None
    production: Optional[float] = None
    employment: Optional[float] = None
    supplier_deliveries: Optional[float] = None
    inventories: Optional[float] = None
    customer_inventories: Optional[float] = None
    prices: Optional[float] = None
    backlog_of_orders: Optional[float] = None
    new_export_orders: Optional[float] = None
    imports: Optional[float] = None
    source_url: str = ""
    fetch_time: str = ""
