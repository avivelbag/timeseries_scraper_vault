from dataclasses import dataclass


@dataclass
class UsdaCropProgressRecord:
    """Schema stub matching UsdaCropProgressRecord in usda_crop_progress.proto."""

    report_week: str = ""
    state: str = ""
    crop: str = ""
    stage: str = ""
    pct_complete: float = 0.0
    condition_category: str = ""
    pct_condition: float = 0.0
    source_url: str = ""
    fetch_time: str = ""
