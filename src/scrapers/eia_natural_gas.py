"""EIA monthly Henry Hub natural gas spot price scraper.

Parses the HTML table published at SOURCE_URL, which lists Henry Hub spot
prices in dollars per million Btu going back to 1997.  No API key required.
"""

from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.http_client import fetch
from protos.eia_natural_gas_pb2 import EiaNaturalGasRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.eia.gov/dnav/ng/hist/rngwhhdm.htm"
_LOCATION = "Henry Hub"

REQUIRED_FIELDS: list[str] = [
    "source_url",
    "period_date",
    "price_usd_per_mmbtu",
    "location",
]


def run(html: str) -> list[dict]:
    """Parse EIA Henry Hub natural gas HTML into a list of price records.

    Selects the first ``table.DataTable`` element and extracts one record per
    row whose first cell contains a date in ``MMM-YYYY`` format and whose
    second cell contains a numeric price.  Rows with missing, empty, or
    non-numeric price cells are silently skipped.

    Args:
        html: Raw HTML string of the EIA DNAV natural gas prices page.

    Returns:
        List of dicts with keys: source_url, period_date (YYYY-MM string),
        price_usd_per_mmbtu (float), location.  fetch_time is omitted —
        callers that need it should add it after calling this function.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    for table in soup.select("table.DataTable"):
        for tr in table.select("tbody tr"):
            cells = [td.get_text(strip=True) for td in tr.select("td")]
            if len(cells) < 2 or not cells[0]:
                continue

            try:
                dt = datetime.strptime(cells[0], "%b-%Y")
                period_date = dt.strftime("%Y-%m")
            except ValueError:
                continue

            raw_price = cells[1].replace(",", "").strip()
            if raw_price in ("", "--"):
                continue
            try:
                price = float(raw_price)
            except ValueError:
                continue

            records.append(
                {
                    "source_url": SOURCE_URL,
                    "period_date": period_date,
                    "price_usd_per_mmbtu": price,
                    "location": _LOCATION,
                }
            )

    return records


def scrape() -> list[dict]:
    """Fetch the live EIA Henry Hub natural gas prices page and return parsed records.

    Delegates HTTP fetching to ``src.http_client.fetch``, which enforces
    robots.txt compliance, a polite 2–5 s delay, and exponential backoff on
    transient errors.

    Returns:
        Same structure as ``run()``.
    """
    resp = fetch(SOURCE_URL)
    return run(resp.text)


def _record_to_proto(record: dict) -> EiaNaturalGasRecord:
    msg = EiaNaturalGasRecord()
    msg.source_url = record["source_url"]
    msg.period_date = record["period_date"]
    msg.price_usd_per_mmbtu = record["price_usd_per_mmbtu"]
    msg.location = record["location"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape EIA Henry Hub natural gas prices and upload records to BigQuery.

    Calls scrape(), converts each record to an EiaNaturalGasRecord proto, and
    uploads via upload_rows. Returns the count of successfully inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("eia_natural_gas", messages, date_column="period_date")


if __name__ == "__main__":
    main()
