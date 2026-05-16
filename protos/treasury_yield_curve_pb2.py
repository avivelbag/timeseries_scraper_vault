from dataclasses import dataclass


@dataclass
class TreasuryYieldRecord:
    """Schema stub matching TreasuryYieldRecord in treasury_yield_curve.proto."""

    date: str = ""
    maturity_1m: float = 0.0
    maturity_3m: float = 0.0
    maturity_6m: float = 0.0
    maturity_1y: float = 0.0
    maturity_2y: float = 0.0
    maturity_5y: float = 0.0
    maturity_10y: float = 0.0
    maturity_30y: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
