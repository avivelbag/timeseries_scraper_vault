"""ISM Manufacturing PMI monthly HTML table scraper.

Fetches the ISM Report on Business PMI page, which publishes a transposed
HTML table where rows are sub-indices (PMI, New Orders, Production, etc.)
and columns are month groups.  Each month group has four sub-columns:
Series Index, Series Direction, Rate of Change, and Trend (Months).

The scraper parses the two-row header to map column positions to months
and identify which sub-column in each group carries the numeric Series
Index value.  It then transposes the table to produce one
IsmManufacturingPmiRecord per month.
"""

import re
import time
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup, Tag

from protos.ism_manufacturing_pmi_pb2 import IsmManufacturingPmiRecord  # type: ignore[attr-defined]
from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch

SOURCE_URL = (
    "https://www.ismworld.org/supply-management-news-and-reports/"
    "reports/ism-report-on-business/pmi/current/"
)

_NA_VALUES = frozenset({"N/A", "n/a", "NA", "na", "", "--", "-", ".", "n.a."})

# Maps normalised row-label text to the corresponding proto field name.
_FIELD_MAP: dict[str, str] = {
    "manufacturing pmi": "pmi",
    "pmi composite": "pmi",
    "pmi": "pmi",
    "new orders": "new_orders",
    "production": "production",
    "employment": "employment",
    "supplier deliveries": "supplier_deliveries",
    "inventories": "inventories",
    "customers' inventories": "customer_inventories",
    "customers inventories": "customer_inventories",
    "customer inventories": "customer_inventories",
    "prices": "prices",
    "backlog of orders": "backlog_of_orders",
    "new export orders": "new_export_orders",
    "imports": "imports",
}


def _normalize_label(text: str) -> str:
    """Strip trademark symbols and normalise whitespace for field-map lookup.

    Args:
        text: Raw cell text from the row-label column.

    Returns:
        Lowercase, trimmed string with ®/™/© removed.
    """
    return re.sub(r"[®™©]", "", text).strip().lower()


def _parse_float(text: str) -> Optional[float]:
    """Convert a cell string to float, returning None for N/A or unparseable values.

    Args:
        text: Raw cell text (may contain surrounding whitespace).

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


def _parse_month_header(text: str) -> Optional[str]:
    """Parse a month-year header string into a YYYY-MM-01 ISO date.

    Tries both full-month ("January 2025") and abbreviated ("Jan 2025") forms.

    Args:
        text: Raw header cell text.

    Returns:
        ISO date string "YYYY-MM-01", or None if the text is not a recognisable
        month-year value.
    """
    stripped = text.strip()
    for fmt in ("%B %Y", "%b %Y", "%B, %Y", "%b, %Y"):
        try:
            return datetime.strptime(stripped, fmt).strftime("%Y-%m-01")
        except ValueError:
            continue
    return None


def _header_rows(table: Tag) -> list[Tag]:
    """Extract header rows from a table element.

    Prefers an explicit <thead>; falls back to leading rows that contain at
    least one <th> element.

    Args:
        table: BeautifulSoup Tag for the <table> element.

    Returns:
        List of <tr> Tags that constitute the header (may be empty).
    """
    thead = table.find("thead")
    if thead:
        return list(thead.find_all("tr"))

    headers: list[Tag] = []
    for row in table.find_all("tr"):
        if row.find("th"):
            headers.append(row)
        else:
            break
    return headers


def _build_column_map(header_rows: list[Tag]) -> list[tuple[Optional[str], bool]]:
    """Build a (month_date, is_value_col) entry for every column position.

    Processes two header rows:

    * Row 1 – month group headers, typically using ``colspan`` to span several
      sub-columns per month.  A cell with ``rowspan>=2`` is the row-label column
      and contributes (None, False) entries; it does not appear in row 2.

    * Row 2 – sub-column labels within each month group.  A cell whose text
      contains "index" (case-insensitive) and not "direction" identifies the
      numeric Series Index sub-column (is_value=True); all others are False.

    When only one header row is present, every column that has a recognised
    month_date is treated as a value column.

    Args:
        header_rows: List of <tr> Tag elements from the table's <thead>.

    Returns:
        List of (month_date, is_value_col) tuples indexed by column position.
        The row-label column (position 0) always has (None, False).
    """
    if not header_rows:
        return []

    row1 = header_rows[0]
    col_months: list[Optional[str]] = []
    # Columns filled by rowspan=2 cells in row 1 do not appear in row 2.
    row2_skip_cols = 0

    for cell in row1.find_all(["th", "td"]):
        rowspan = int(cell.get("rowspan", 1))
        colspan = int(cell.get("colspan", 1))
        month = _parse_month_header(cell.get_text(strip=True))

        if rowspan >= 2:
            col_months.extend([None] * colspan)
            row2_skip_cols += colspan
        else:
            col_months.extend([month] * colspan)

    if len(header_rows) < 2:
        return [(m, m is not None) for m in col_months]

    row2 = header_rows[1]
    # Seed with False entries for the columns already claimed by rowspan=2 cells.
    col_is_value: list[bool] = [False] * row2_skip_cols

    for cell in row2.find_all(["th", "td"]):
        colspan = int(cell.get("colspan", 1))
        label = cell.get_text(strip=True).lower()
        # "Series Index" → value column; Direction / Rate of Change / Trend → skip.
        is_value = "index" in label and "direction" not in label
        col_is_value.extend([is_value] * colspan)

    length = max(len(col_months), len(col_is_value))
    col_months.extend([None] * (length - len(col_months)))
    col_is_value.extend([False] * (length - len(col_is_value)))

    return list(zip(col_months, col_is_value))


def run(html: str, source_url: str = SOURCE_URL) -> list[IsmManufacturingPmiRecord]:
    """Parse the ISM Manufacturing PMI table from raw page HTML.

    Locates the first <table> on the page, derives a column map from the
    two-row header, then iterates the data rows.  Each row is mapped to a
    proto field via _FIELD_MAP.  Only columns where is_value_col=True and
    month_date is not None contribute to the output.  The resulting per-month
    dicts are assembled into IsmManufacturingPmiRecord instances and returned
    sorted by report_date ascending.

    Args:
        html: Raw HTML of the ISM PMI current-data page.
        source_url: Stored verbatim in each record's source_url field.

    Returns:
        List of IsmManufacturingPmiRecord instances, one per calendar month,
        sorted ascending by report_date.

    Raises:
        ValueError: When html is empty, no table is found, or the table
            contains no extractable records.
    """
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to ISM PMI parser")

    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table")
    if not table:
        raise ValueError("No table found in ISM PMI page")

    hdrs = _header_rows(table)
    col_map = _build_column_map(hdrs)

    month_data: dict[str, dict[str, Optional[float]]] = {}

    tbody = table.find("tbody")
    if tbody:
        rows = list(tbody.find_all("tr"))
    else:
        rows = [r for r in table.find_all("tr") if not r.find("th")]

    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        label = _normalize_label(cells[0].get_text(strip=True))
        field_name = _FIELD_MAP.get(label)
        if field_name is None:
            continue

        for i, cell in enumerate(cells):
            if i >= len(col_map):
                break
            month_date, is_value = col_map[i]
            if not is_value or month_date is None:
                continue

            value = _parse_float(cell.get_text(strip=True))
            month_data.setdefault(month_date, {})[field_name] = value

    if not month_data:
        raise ValueError("No records extracted from ISM PMI table")

    fetch_ts = datetime.now(timezone.utc).isoformat()
    records: list[IsmManufacturingPmiRecord] = []

    for month_date in sorted(month_data.keys()):
        d = month_data[month_date]
        records.append(
            IsmManufacturingPmiRecord(
                report_date=month_date,
                pmi=d.get("pmi"),
                new_orders=d.get("new_orders"),
                production=d.get("production"),
                employment=d.get("employment"),
                supplier_deliveries=d.get("supplier_deliveries"),
                inventories=d.get("inventories"),
                customer_inventories=d.get("customer_inventories"),
                prices=d.get("prices"),
                backlog_of_orders=d.get("backlog_of_orders"),
                new_export_orders=d.get("new_export_orders"),
                imports=d.get("imports"),
                source_url=source_url,
                fetch_time=fetch_ts,
            )
        )

    return records


def scrape() -> list[IsmManufacturingPmiRecord]:
    """Fetch the ISM Manufacturing PMI page and parse its data table.

    Delegates HTTP retrieval to ``http_client.fetch``, which enforces
    robots.txt compliance, applies a 2–5 s polite delay before the request,
    and retries 429/5xx responses with exponential backoff (base 2 s, cap
    120 s, up to 5 attempts).  An additional 3 s sleep after the fetch
    maintains courteous crawl behaviour.

    Returns:
        List of IsmManufacturingPmiRecord instances.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def main() -> int:
    """Scrape ISM Manufacturing PMI data and upload to BigQuery.

    Uploads records to the ``ism_manufacturing_pmi`` table, deduplicating
    by report_date so re-runs are idempotent.

    Returns:
        Count of rows successfully inserted.
    """
    records = scrape()
    return upload_rows("ism_manufacturing_pmi", records, date_column="report_date")
