# Hand-written stub for protos/census_retail_sales.proto.
from dataclasses import dataclass


@dataclass
class CensusRetailSalesRecord:
    """Schema stub matching CensusRetailSalesRecord in census_retail_sales.proto."""

    series_name: str = ""
    period_date: str = ""
    sales_millions_usd: float = 0.0
    month_over_month_pct: float = 0.0
    year_over_year_pct: float = 0.0
    revised: bool = False
    source_url: str = ""
    fetch_time: str = ""
