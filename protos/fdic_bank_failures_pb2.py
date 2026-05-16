# Hand-written stub for protos/fdic_bank_failures.proto.
from dataclasses import dataclass
from typing import Optional


@dataclass
class FdicBankFailureRecord:
    """Schema stub matching FdicBankFailureRecord in fdic_bank_failures.proto."""

    cert: int = 0
    institution_name: str = ""
    city: str = ""
    state: str = ""
    failure_date: str = ""
    approx_assets_usd_millions: Optional[float] = None
    approx_deposits_usd_millions: Optional[float] = None
    estimated_loss_usd_millions: Optional[float] = None
    source_url: str = ""
    fetch_time: str = ""
