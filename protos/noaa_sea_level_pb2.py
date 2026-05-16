from dataclasses import dataclass


@dataclass
class SeaLevelRecord:
    """Schema stub matching SeaLevelRecord in noaa_sea_level.proto."""

    station_id: str = ""
    station_name: str = ""
    year: int = 0
    month: int = 0
    mean_sea_level_mm: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
