from dataclasses import dataclass


@dataclass
class FedH15Record:
    """Schema stub matching FedH15Record in fed_h15_rates.proto."""

    period_date: str = ""
    series_name: str = ""
    maturity: str = ""
    frequency: str = ""
    rate: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
