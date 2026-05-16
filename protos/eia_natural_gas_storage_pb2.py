# Hand-written stub for protos/eia_natural_gas_storage.proto.
from dataclasses import dataclass


@dataclass
class EiaNaturalGasStorageRecord:
    """Schema stub matching EiaNaturalGasStorageRecord in eia_natural_gas_storage.proto."""

    region: str = ""
    storage_bcf: float = 0.0
    year_ago_bcf: float = 0.0
    five_year_avg_bcf: float = 0.0
    report_date: str = ""
    source_url: str = ""
    fetch_time: str = ""
