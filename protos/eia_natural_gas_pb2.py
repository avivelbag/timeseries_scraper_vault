from dataclasses import dataclass


@dataclass
class EiaNaturalGasRecord:
    """Schema stub matching EiaNaturalGasRecord in eia_natural_gas.proto."""

    period_date: str = ""
    price_usd_per_mmbtu: float = 0.0
    location: str = ""
    source_url: str = ""
    fetch_time: str = ""
