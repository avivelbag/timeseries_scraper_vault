"""US Treasury daily par yield curve HTML scraper.

Fetches the monthly yield curve page from home.treasury.gov for the given
year/month and parses the HTML table into TreasuryYieldRecord protos.
The page requires no authentication and is publicly permitted by robots.txt.
"""

from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.treasury_yield_curve_pb2 import TreasuryYieldRecord  # type: ignore[attr-defined]

SOURCE_URL_TEMPLATE = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "TextView?type=daily_treasury_yield_curve&field_tdr_date_value={year_month}"
)

LABEL_TO_FIELD: dict[str, str] = {
    "1 Mo": "maturity_1m",
    "3 Mo": "maturity_3m",
    "6 Mo": "maturity_6m",
    "1 Yr": "maturity_1y",
    "2 Yr": "maturity_2y",
    "5 Yr": "maturity_5y",
    "10 Yr": "maturity_10y",
    "30 Yr": "maturity_30y",
}

REQUIRED_FIELDS: list[str] = ["date", "maturity_10y"]


def run(html: str, source_url: str = "") -> list[dict]:
    """Parse Treasury par yield curve HTML into a list of yield records.

    Locates the first ``<table>`` whose header row contains at least one
    maturity label from LABEL_TO_FIELD, then parses one record per data row.

    ``N/A`` and empty cells map to the ``-1.0`` sentinel.  Dates in
    ``MM/DD/YYYY`` format are converted to ISO-8601 ``YYYY-MM-DD``; rows
    with unparseable dates are silently skipped.

    Args:
        html: Raw HTML string of the Treasury yield curve page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: date, maturity_1m, maturity_3m, maturity_6m,
        maturity_1y, maturity_2y, maturity_5y, maturity_10y, maturity_30y,
        source_url.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        headers = [cell.get_text(strip=True) for cell in header_row.find_all(["th", "td"])]

        if not any(h in LABEL_TO_FIELD for h in headers):
            continue

        col_map: dict[str, int] = {h: i for i, h in enumerate(headers) if h in LABEL_TO_FIELD}

        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if not cells:
                continue

            try:
                iso_date = datetime.strptime(cells[0], "%m/%d/%Y").date().isoformat()
            except ValueError:
                continue

            record: dict = {"date": iso_date, "source_url": source_url}

            for label, field_name in LABEL_TO_FIELD.items():
                col_idx = col_map.get(label)
                if col_idx is None or col_idx >= len(cells):
                    record[field_name] = -1.0
                    continue
                raw = cells[col_idx]
                if raw in ("N/A", ""):
                    record[field_name] = -1.0
                else:
                    try:
                        record[field_name] = float(raw)
                    except ValueError:
                        record[field_name] = -1.0

            records.append(record)

        if records:
            break

    return records


def scrape(year_month: str | None = None) -> list[dict]:
    """Fetch the live Treasury yield curve page and return parsed records.

    Uses the current UTC month by default; pass ``year_month`` as ``"YYYYMM"``
    to fetch a specific month.  Delegates HTTP fetching to
    ``src.http_client.fetch``, which enforces robots.txt compliance, a polite
    delay, and exponential backoff on transient errors.

    Args:
        year_month: Optional six-digit string (e.g. ``"202505"``) selecting the
            month to fetch.  Defaults to the current UTC month.

    Returns:
        Same structure as ``run()``.
    """
    if year_month is None:
        year_month = datetime.now(timezone.utc).strftime("%Y%m")
    url = SOURCE_URL_TEMPLATE.format(year_month=year_month)
    resp = fetch(url)
    return run(resp.text, source_url=url)


def _record_to_proto(record: dict) -> TreasuryYieldRecord:
    msg = TreasuryYieldRecord()
    msg.date = record["date"]
    msg.maturity_1m = record["maturity_1m"]
    msg.maturity_3m = record["maturity_3m"]
    msg.maturity_6m = record["maturity_6m"]
    msg.maturity_1y = record["maturity_1y"]
    msg.maturity_2y = record["maturity_2y"]
    msg.maturity_5y = record["maturity_5y"]
    msg.maturity_10y = record["maturity_10y"]
    msg.maturity_30y = record["maturity_30y"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape Treasury yield curve data and upload records to BigQuery.

    Fetches the current month's daily par yield curve, converts each record to
    a TreasuryYieldRecord proto, and uploads via upload_rows.  Returns the
    count of successfully inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("treasury_yield_curve", messages, date_column="date")


if __name__ == "__main__":
    main()
