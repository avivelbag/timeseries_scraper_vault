"""Chicago Fed National Activity Index (CFNAI) scraper.

Fetches the CFNAI current-data page, which publishes a monthly HTML table
of the composite index (weighted average of 85 economic indicators) plus
four category sub-indices: Production & Income, Employment/Unemployment/
Hours, Personal Consumption & Housing, and Sales/Orders/Inventories.

The HTML table uses ``id="cfnai-data"``; a first-table fallback handles
layout changes. Columns are mapped positionally in the order the Chicago
Fed publishes them: Date, CFNAI, CFNAI-MA3, then the four sub-indices.
N/A and empty cells become None rather than raising.
"""

import re
import time
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup, Tag

from protos.cfnai_pb2 import CfnaiRecord  # type: ignore[attr-defined]
from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch

SOURCE_URL = "https://www.chicagofed.org/research/data/cfnai/current-data"

_NA_VALUES = frozenset({"N/A", "n/a", "NA", "na", "", "--", "-", ".", "n.a."})

# Matches the YYYY-MM date format used in CFNAI table rows.
_DATE_RE = re.compile(r"^\d{4}-\d{2}$")


def _parse_float(text: str) -> Optional[float]:
    """Convert a cell string to float, returning None for N/A or unparseable values.

    Args:
        text: Raw cell text (may include surrounding whitespace).

    Returns:
        Parsed float, or None if the text signals a missing value.
    """
    clean = text.strip()
    if clean in _NA_VALUES:
        return None
    try:
        return float(clean)
    except ValueError:
        return None


def _parse_date(text: str) -> str:
    """Convert a YYYY-MM string to YYYY-MM-DD (first day of month).

    Args:
        text: Date string in YYYY-MM format.

    Returns:
        ISO date string YYYY-MM-DD.

    Raises:
        ValueError: If text does not match YYYY-MM.
    """
    return datetime.strptime(text.strip(), "%Y-%m").strftime("%Y-%m-%d")


def _data_rows(table: Tag) -> list[Tag]:
    """Extract data <tr> elements from a table, skipping any header rows.

    Prefers explicit <tbody>; otherwise scans forward through <tr> elements
    until the first one whose initial cell matches the YYYY-MM date pattern,
    treating everything before it as a header.

    Args:
        table: BeautifulSoup Tag representing the <table> element.

    Returns:
        List of <tr> Tags that contain data (not header) rows.
    """
    tbody = table.find("tbody")
    if tbody:
        return list(tbody.find_all("tr"))

    all_rows = table.find_all("tr")
    for i, row in enumerate(all_rows):
        cells = row.find_all(["td", "th"])
        if cells and _DATE_RE.match(cells[0].get_text(strip=True)):
            return list(all_rows[i:])
    return []


def run(html: str, source_url: str = SOURCE_URL) -> list[CfnaiRecord]:
    """Parse the CFNAI data table from the Chicago Fed HTML page.

    Locates the table by ``id="cfnai-data"`` or falls back to the first
    ``<table>`` on the page.  Maps each data row to a CfnaiRecord using
    positional column indexing (Date=0, CFNAI=1, MA3=2, Production=3,
    Employment=4, Consumption=5, Sales=6).  N/A and empty cells produce
    None fields rather than raising.

    Args:
        html: Raw HTML of the CFNAI current-data page.
        source_url: Stored verbatim in each record's source_url field.

    Returns:
        List of CfnaiRecord instances, one per calendar-month data row.

    Raises:
        ValueError: When html is empty, no table is found, or no records
            can be extracted from the table.
    """
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to CFNAI parser")

    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table", {"id": "cfnai-data"}) or soup.find("table")
    if not table:
        raise ValueError("No table found in CFNAI page")

    fetch_ts = datetime.now(timezone.utc).isoformat()
    rows = _data_rows(table)

    records: list[CfnaiRecord] = []
    for row in rows:
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if len(cells) < 7:
            continue
        if not cells[0] or not _DATE_RE.match(cells[0]):
            continue

        try:
            series_date = _parse_date(cells[0])
        except ValueError:
            continue

        records.append(CfnaiRecord(
            series_date=series_date,
            cfnai=_parse_float(cells[1]),
            cfnai_ma3=_parse_float(cells[2]),
            production_and_income=_parse_float(cells[3]),
            employment_unemployment_hours=_parse_float(cells[4]),
            personal_consumption_housing=_parse_float(cells[5]),
            sales_orders_inventories=_parse_float(cells[6]),
            source_url=source_url,
            fetch_time=fetch_ts,
        ))

    if not records:
        raise ValueError("No records extracted from CFNAI table")

    return records


def scrape() -> list[CfnaiRecord]:
    """Fetch the Chicago Fed CFNAI page and parse its data table.

    Delegates to ``http_client.fetch``, which enforces robots.txt, applies
    a 2–5 s polite delay, and retries 429/5xx with exponential backoff.
    An additional 3 s sleep follows the initial fetch to stay courteous.

    Returns:
        List of CfnaiRecord instances.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def main() -> int:
    """Scrape CFNAI data and upload records to BigQuery table ``cfnai``.

    Returns:
        Count of successfully inserted rows.
    """
    records = scrape()
    return upload_rows("cfnai", records, date_column="series_date")
