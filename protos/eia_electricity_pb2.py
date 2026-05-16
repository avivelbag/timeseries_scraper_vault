from dataclasses import dataclass


@dataclass
class EiaElectricityRecord:
    """Schema stub matching EiaElectricityRecord in eia_electricity.proto."""

    week_ending_date: str = ""
    fuel_type: str = ""
    generation_thousand_mwh: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
