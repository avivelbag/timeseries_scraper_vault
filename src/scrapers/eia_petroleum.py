"""EIA weekly petroleum retail price scraper.

Parses the HTML table published at SOURCE_URL, which lists US gasoline and
diesel prices by grade and region going back to 1993.  No API key required.
"""

from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.http_client import fetch
from protos.eia_petroleum_prices_pb2 import PetroleumPriceRecord

SOURCE_URL = "https://www.eia.gov/dnav/pet/pet_pri_gnd_dcus_nus_w.htm"
_UNITS = "USD/gallon"


def run(html: str) -> list[dict]:
    """Parse EIA petroleum price HTML into a list of price records.

    Selects every ``table.DataTable`` element, infers the grade/product label
    from the nearest preceding ``<h2>`` or ``<h3>``, then yields one dict per
    (date, region) cell that contains a numeric price.

    Args:
        html: Raw HTML string of the EIA petroleum prices page.

    Returns:
        List of dicts with keys matching PetroleumPriceRecord field names:
        source_url, period_date, product, region, price_usd_per_gallon,
        grade, units.  fetch_time is omitted — callers that need it should
        add it after calling this function.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    for table in soup.select("table.DataTable"):
        caption = table.find_previous("h2") or table.find_previous("h3")
        grade_product = caption.get_text(strip=True) if caption else "unknown"

        headers = [th.get_text(strip=True) for th in table.select("thead th")]
        regions = headers[1:]  # first column header is "Date"

        for tr in table.select("tbody tr"):
            cells = [td.get_text(strip=True) for td in tr.select("td")]
            if not cells or not cells[0]:
                continue
            try:
                period_date = datetime.strptime(cells[0], "%m/%d/%Y").date().isoformat()
            except ValueError:
                continue

            for region, raw_price in zip(regions, cells[1:]):
                if raw_price in ("", "--"):
                    continue
                records.append(
                    {
                        "source_url": SOURCE_URL,
                        "period_date": period_date,
                        "product": "petroleum",
                        "region": region,
                        "price_usd_per_gallon": float(raw_price),
                        "grade": grade_product,
                        "units": _UNITS,
                    }
                )

    return records


def scrape() -> list[dict]:
    """Fetch the live EIA petroleum prices page and return parsed records.

    Delegates HTTP fetching to ``src.http_client.fetch``, which enforces
    robots.txt compliance, a polite 2–5 s delay, and exponential backoff on
    transient errors.

    Returns:
        Same structure as ``run()``.
    """
    resp = fetch(SOURCE_URL)
    return run(resp.text)


def _record_to_proto(record: dict) -> PetroleumPriceRecord:
    """Convert a parsed record dict to a PetroleumPriceRecord proto message.

    Sets fetch_time to the current UTC time. All other fields are copied
    directly from the dict returned by ``run()``.
    """
    msg = PetroleumPriceRecord()
    msg.source_url = record["source_url"]
    msg.period_date = record["period_date"]
    msg.product = record["product"]
    msg.region = record["region"]
    msg.price_usd_per_gallon = record["price_usd_per_gallon"]
    msg.grade = record["grade"]
    msg.units = record["units"]
    msg.fetch_time.FromDatetime(datetime.now(timezone.utc))
    return msg


def main() -> int:
    """Scrape EIA petroleum prices and upload records to BigQuery.

    Calls scrape(), converts each record to a PetroleumPriceRecord proto, and
    uploads via upload_rows. Returns the count of successfully inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("eia_petroleum_prices", messages)


if __name__ == "__main__":
    main()
