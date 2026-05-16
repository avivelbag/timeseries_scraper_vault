"""BLS Consumer Price Index (CPI-U) HTML-table scraper.

Fetches the CPI-U annual average table for series CUSR0000SA0 from the BLS
website and parses the multi-column year/month HTML table into BLSCpiRecord
protos.  One record is emitted per (year, month) cell that contains a
parseable numeric value; Annual and HALF columns are skipped.
"""

import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.bls_cpi_pb2 import BLSCpiRecord  # type: ignore[attr-defined]

SOURCE_URL = (
    "https://data.bls.gov/timeseries/CUSR0000SA0/output-type=column&"
    "years_option=all_years&periods_option=specific_periods&periods=Annual+Data%2C+Jan%2C"
    "Feb%2CMar%2CApr%2CMay%2CJun%2CJul%2CAug%2CSep%2COct%2CNov%2CDec"
)

SERIES_ID = "CUSR0000SA0"
UNITS = "index 1982-84=100"

_MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

# Columns that carry aggregate data rather than a specific month.
_SKIP_COLUMNS = {"Annual", "HALF1", "HALF2", "HalfYear1", "HalfYear2"}

# Matches any character that is not a digit or decimal point, used to strip
# footnote superscripts (e.g. "P" for preliminary) and HTML artefacts.
_NON_NUMERIC = re.compile(r"[^\d.]")

REQUIRED_FIELDS: list[str] = ["series_id", "year", "month", "value"]


def _parse_value(raw: str) -> float | None:
    """Strip non-numeric characters and convert to float.

    Returns None when the resulting string is empty or cannot be cast,
    so callers can skip missing or malformed cells without crashing.

    Args:
        raw: Raw text from a table cell, possibly containing superscripts or
            preliminary markers (e.g. "314.540P", "296.808<sup>1</sup>").

    Returns:
        Parsed float, or None if the cell carries no numeric value.
    """
    cleaned = _NON_NUMERIC.sub("", raw).strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse a BLS CPI HTML page into a list of CPI records.

    Finds the first ``<table>`` whose header row contains at least one column
    matching a standard month abbreviation.  For each data row the year is read
    from the first cell; subsequent cells are matched to a month by their column
    index.  Cells that are empty, non-numeric, or belong to Annual/HALF columns
    are silently skipped.

    Args:
        html: Raw HTML string of the BLS CPI page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: series_id, year, month, value, source_url.
        fetch_time is omitted — callers that need it should add it after calling
        this function.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue

        header_cells = header_row.find_all(["th", "td"])
        for cell in header_cells:
            for sup in cell.find_all("sup"):
                sup.decompose()
        headers = [cell.get_text(strip=True) for cell in header_cells]

        # Build mapping from column index to month number (1-based) for columns
        # that correspond to a calendar month; skip non-month columns entirely.
        month_col_map: dict[int, int] = {}
        for idx, header in enumerate(headers):
            normalized = header.strip()
            if normalized in _SKIP_COLUMNS:
                continue
            if normalized in _MONTH_NAMES:
                month_col_map[idx] = _MONTH_NAMES.index(normalized) + 1

        if not month_col_map:
            continue

        for tr in table.find_all("tr")[1:]:
            row_cells = tr.find_all(["td", "th"])
            for cell in row_cells:
                for sup in cell.find_all("sup"):
                    sup.decompose()
            cells = [td.get_text(strip=True) for td in row_cells]
            if not cells:
                continue

            raw_year = _NON_NUMERIC.sub("", cells[0]).strip()
            if not raw_year:
                continue
            try:
                year = int(raw_year)
            except ValueError:
                continue

            for col_idx, month_num in month_col_map.items():
                if col_idx >= len(cells):
                    continue
                value = _parse_value(cells[col_idx])
                if value is None:
                    continue
                records.append(
                    {
                        "series_id": SERIES_ID,
                        "year": year,
                        "month": month_num,
                        "value": value,
                        "source_url": source_url,
                    }
                )

        if records:
            break

    return records


def scrape() -> list[dict]:
    """Fetch the live BLS CPI page and return parsed records.

    Delegates HTTP fetching to ``src.http_client.fetch``, which enforces a
    User-Agent header, a minimum 2–5 s polite delay, and exponential backoff on
    429/5xx responses.  An additional sleep of at least 3 seconds is applied
    after the fetch to satisfy BLS rate-limit guidance.

    Returns:
        Same structure as ``run()``.
    """
    resp = fetch(SOURCE_URL)
    # BLS requests a minimum 3-second courtesy delay between requests.
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def _record_to_proto(record: dict) -> BLSCpiRecord:
    msg = BLSCpiRecord()
    msg.series_id = record["series_id"]
    msg.year = record["year"]
    msg.month = record["month"]
    msg.value = record["value"]
    msg.source_url = record["source_url"]
    msg.fetch_time.FromDatetime(datetime.now(timezone.utc))
    msg.units = UNITS
    return msg


def main() -> int:
    """Scrape BLS CPI data and upload records to BigQuery.

    Calls scrape(), converts each record to a BLSCpiRecord proto, and uploads
    via upload_rows.  Returns the count of successfully inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("bls_cpi", messages)


if __name__ == "__main__":
    main()
