"""Registry mapping each scraper module to its live URL and proto class."""

from datetime import datetime, timezone

from protos.bls_cpi_pb2 import BLSCpiRecord  # type: ignore[attr-defined]
from protos.eia_electricity_pb2 import EiaElectricityRecord  # type: ignore[attr-defined]
from protos.eia_natural_gas_pb2 import EiaNaturalGasRecord  # type: ignore[attr-defined]
from protos.eia_petroleum_prices_pb2 import PetroleumPriceRecord  # type: ignore[attr-defined]
from protos.fao_food_price_index_pb2 import FaoFoodPriceRecord  # type: ignore[attr-defined]
from protos.fed_h15_rates_pb2 import FedH15Record  # type: ignore[attr-defined]
from protos.treasury_yield_curve_pb2 import TreasuryYieldRecord  # type: ignore[attr-defined]
from protos.usda_crop_progress_pb2 import UsdaCropProgressRecord  # type: ignore[attr-defined]
from protos.usgs_streamflow_pb2 import UsgsStreamflowRecord  # type: ignore[attr-defined]
from src.scrapers import bls_cpi
from src.scrapers import eia_electricity
from src.scrapers import eia_natural_gas
from src.scrapers import eia_petroleum
from src.scrapers import fao_food_price_index
from src.scrapers import fed_h15_rates
from src.scrapers import treasury_yield_curve
from src.scrapers import usda_crop_progress
from src.scrapers import usgs_streamflow

# treasury_yield_curve uses a monthly URL template; compute the current month at
# import time so callers always get a valid URL without needing to pass arguments.
_TREASURY_URL = treasury_yield_curve.SOURCE_URL_TEMPLATE.format(
    year_month=datetime.now(timezone.utc).strftime("%Y%m")
)

REGISTRY: list[dict] = [
    {
        "scraper_module": bls_cpi,
        "url": bls_cpi.SOURCE_URL,
        "proto_class": BLSCpiRecord,
    },
    {
        "scraper_module": eia_electricity,
        "url": eia_electricity.SOURCE_URL,
        "proto_class": EiaElectricityRecord,
    },
    {
        "scraper_module": eia_natural_gas,
        "url": eia_natural_gas.SOURCE_URL,
        "proto_class": EiaNaturalGasRecord,
    },
    {
        "scraper_module": eia_petroleum,
        "url": eia_petroleum.SOURCE_URL,
        "proto_class": PetroleumPriceRecord,
    },
    {
        "scraper_module": fao_food_price_index,
        "url": fao_food_price_index.SOURCE_URL,
        "proto_class": FaoFoodPriceRecord,
    },
    {
        "scraper_module": fed_h15_rates,
        "url": fed_h15_rates.SOURCE_URL,
        "proto_class": FedH15Record,
    },
    {
        "scraper_module": treasury_yield_curve,
        "url": _TREASURY_URL,
        "proto_class": TreasuryYieldRecord,
    },
    {
        "scraper_module": usda_crop_progress,
        "url": usda_crop_progress.SOURCE_URL,
        "proto_class": UsdaCropProgressRecord,
    },
    {
        "scraper_module": usgs_streamflow,
        "url": usgs_streamflow.SOURCE_URL,
        "proto_class": UsgsStreamflowRecord,
    },
]
