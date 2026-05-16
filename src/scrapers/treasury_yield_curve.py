"""US Treasury daily par yield curve HTML scraper.

Fetches the monthly yield curve page from home.treasury.gov for the given
year/month and parses the HTML table into TreasuryYieldRecord protos.
The page requires no authentication and is publicly permitted by robots.txt.
"""

import argparse
import logging
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

_log = logging.getLogger(__name__)

# Pre-built lookup from normalized label (lowercase, no spaces/hyphens) to field name.
# Allows _header_to_field() to match variants like "1-Mo", "10Yr", "10-Yr" without
# maintaining an ever-growing alias list.
_FIELD_BY_NORMALIZED: dict[str, str] = {
    k.lower().replace(" ", "").replace("-", ""): v for k, v in LABEL_TO_FIELD.items()
}


def _header_to_field(label: str) -> str | None:
    if label in LABEL_TO_FIELD:
        return LABEL_TO_FIELD[label]
    norm = label.strip().lower().replace(" ", "").replace("-", "")
    return _FIELD_BY_NORMALIZED.get(norm)


def run(html: str, source_url: str = "") -> list[dict]:
    """Parse Treasury par yield curve HTML into a list of yield records.

    Locates the first ``<table>`` whose header row contains at least one
    maturity label from LABEL_TO_FIELD, then parses one record per data row.

    ``N/A`` and empty cells map to the ``-1.0`` sentinel.  Dates in
    ``MM/DD/YYYY`` format are converted to ISO-8601 ``YYYY-MM-DD``; rows
    with unparseable dates are silently skipped.  Header labels are resolved
    via ``_header_to_field`` so minor layout drift (e.g. "10-Yr" vs "10 Yr")
    is handled gracefully.

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

        if not any(_header_to_field(h) is not None for h in headers):
            continue

        # Key the column map by proto field name so drift-normalised headers land
        # in the same slot as their canonical equivalents.
        col_map: dict[str, int] = {
            _header_to_field(h): i
            for i, h in enumerate(headers)
            if _header_to_field(h) is not None
        }

        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if not cells:
                continue

            try:
                iso_date = datetime.strptime(cells[0], "%m/%d/%Y").date().isoformat()
            except ValueError:
                continue

            record: dict = {"date": iso_date, "source_url": source_url}

            for field_name in LABEL_TO_FIELD.values():
                col_idx = col_map.get(field_name)
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


def _iter_months(start_month: str, end_month: str):
    """Yield YYYYMM strings from start_month to end_month inclusive.

    Args:
        start_month: First month in YYYY-MM format.
        end_month: Last month in YYYY-MM format.
    """
    cur = datetime.strptime(start_month, "%Y-%m")
    end = datetime.strptime(end_month, "%Y-%m")
    while cur <= end:
        yield cur.strftime("%Y%m")
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)


def backfill(start_month: str, end_month: str) -> list[dict]:
    """Scrape yield curve records for each month in [start_month, end_month].

    Iterates month-by-month and calls ``scrape()`` for each.  The underlying
    ``http_client.fetch`` enforces a ≥2 s polite delay and exponential backoff
    on 429/5xx, so no additional sleep is needed here.  Months where ``scrape``
    raises are logged and skipped so a single bad page does not abort the run.

    Args:
        start_month: First month in YYYY-MM format (inclusive).
        end_month: Last month in YYYY-MM format (inclusive).

    Returns:
        Flat list of all parsed records across all fetched months.
    """
    all_records: list[dict] = []
    for ym in _iter_months(start_month, end_month):
        try:
            records = scrape(ym)
            all_records.extend(records)
        except Exception as exc:
            _log.warning("backfill: skipping %s: %s", ym, exc)
    return all_records


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
    parser = argparse.ArgumentParser(description="Treasury yield curve scraper / backfiller")
    parser.add_argument("--start-month", metavar="YYYY-MM", help="Start month for backfill (inclusive)")
    parser.add_argument("--end-month", metavar="YYYY-MM", help="End month for backfill (inclusive)")
    args = parser.parse_args()

    if args.start_month or args.end_month:
        if not (args.start_month and args.end_month):
            parser.error("--start-month and --end-month must both be provided")
        records = backfill(args.start_month, args.end_month)
        messages = [_record_to_proto(r) for r in records]
        upload_rows("treasury_yield_curve", messages, date_column="date")
    else:
        main()
