"""NY Fed Empire State Manufacturing Survey monthly HTML scraper.

Fetches the Empire State Manufacturing Survey overview page, which publishes
a transposed HTML table where rows are diffusion-index sub-indices and columns
are months (most recent first).  The table body contains two sections separated
by a full-width header row: "CURRENT" and "SIX-MONTH OUTLOOK".  Only the
current-section rows are parsed; parsing stops as soon as the outlook header
is encountered.

Footnote markers (asterisks, daggers, etc.) are stripped from cell values
before float conversion.  Negative diffusion-index values are valid and common.
"""

import re
import time
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup, Tag

from protos.empire_state_manufacturing_pb2 import EmpireStateManufacturingRecord  # type: ignore[attr-defined]
from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch

SOURCE_URL = "https://www.newyorkfed.org/survey/empire/empiresurvey_overview"

_NA_VALUES = frozenset({"N/A", "n/a", "NA", "na", "", "--", "-", ".", "n.a."})

# Strips trailing asterisks, daggers, and similar footnote markers from values.
_FOOTNOTE_RE = re.compile(r"[*†‡§¶#]+")

# Maps normalised row-label text to EmpireStateManufacturingRecord field names.
_FIELD_MAP: dict[str, str] = {
    "general business conditions": "general_business_conditions",
    "new orders": "new_orders",
    "shipments": "shipments",
    "unfilled orders": "unfilled_orders",
    "delivery time": "delivery_time",
    "inventories": "inventories",
    "prices paid": "prices_paid",
    "prices received": "prices_received",
    "number of employees": "number_of_employees",
    "average employee workweek": "avg_workweek",
    "avg. employee workweek": "avg_workweek",
    "avg workweek": "avg_workweek",
    "average workweek": "avg_workweek",
    "capital expenditures": "capital_expenditures",
    "technology spending": "technology_spending",
}


def _normalize_label(text: str) -> str:
    """Strip extra whitespace and lower-case a row-label string for field-map lookup.

    Args:
        text: Raw cell text from the row-label column.

    Returns:
        Lowercase, single-space-normalised string.
    """
    return re.sub(r"\s+", " ", text).strip().lower()


def _parse_float(text: str) -> Optional[float]:
    """Convert a cell string to float, stripping footnote markers first.

    Handles values like ``"14.3*"`` or ``"-8.1†"`` by removing marker
    characters before attempting conversion.  Returns None for N/A spellings
    or unparseable text.

    Args:
        text: Raw cell text (may include surrounding whitespace or footnote markers).

    Returns:
        Parsed float, or None if the text signals a missing value.
    """
    clean = _FOOTNOTE_RE.sub("", text).strip()
    if clean in _NA_VALUES:
        return None
    try:
        return float(clean)
    except ValueError:
        return None


def _parse_month_header(text: str) -> Optional[str]:
    """Parse a month-year header string into a YYYY-MM-01 ISO date.

    Tries both full-month (``"January 2025"``) and abbreviated (``"Jan 2025"``)
    forms, with and without a trailing comma.

    Args:
        text: Raw header cell text.

    Returns:
        ISO date string ``"YYYY-MM-01"``, or None if not a recognisable month-year.
    """
    stripped = text.strip()
    for fmt in ("%B %Y", "%b %Y", "%B, %Y", "%b, %Y"):
        try:
            return datetime.strptime(stripped, fmt).strftime("%Y-%m-01")
        except ValueError:
            continue
    return None


def _is_outlook_header(text: str) -> bool:
    """Return True if row text marks the start of the six-month outlook section.

    Args:
        text: Normalised row text (full-width header cell content).

    Returns:
        True when the text signals the outlook section boundary.
    """
    lower = text.lower()
    return "six-month" in lower or "six month" in lower or "outlook" in lower or "expectation" in lower


def _parse_months_from_thead(table: Tag) -> list[str]:
    """Extract ordered list of YYYY-MM-01 date strings from the table header row.

    Reads the first ``<tr>`` in ``<thead>`` and parses each cell after the
    first (the row-label column) as a month header.  Cells that do not parse
    as month-year strings are skipped.

    Args:
        table: BeautifulSoup Tag representing the ``<table>`` element.

    Returns:
        List of ISO date strings in the order they appear as columns.

    Raises:
        ValueError: When no ``<thead>`` is found or no month headers can be parsed.
    """
    thead = table.find("thead")
    if not thead:
        raise ValueError("No <thead> found in Empire State Manufacturing table")

    header_row = thead.find("tr")
    if not header_row:
        raise ValueError("No header row in <thead> of Empire State Manufacturing table")

    months: list[str] = []
    cells = header_row.find_all(["th", "td"])
    for cell in cells[1:]:
        date = _parse_month_header(cell.get_text(strip=True))
        if date:
            months.append(date)

    if not months:
        raise ValueError("No month headers parsed from Empire State Manufacturing table")

    return months


def run(html: str, source_url: str = SOURCE_URL) -> list[EmpireStateManufacturingRecord]:
    """Parse the Empire State Manufacturing Survey table from raw page HTML.

    Locates the first ``<table>`` on the page and processes its ``<tbody>``
    rows.  Rows in the "SIX-MONTH OUTLOOK" section (detected by a full-width
    header cell) are excluded; only "CURRENT" section rows are used.  Each
    data row whose label matches ``_FIELD_MAP`` contributes one value per
    month column.  After all rows are processed the per-month dicts are
    assembled into ``EmpireStateManufacturingRecord`` instances sorted by
    ``survey_date`` ascending.

    Args:
        html: Raw HTML of the Empire State Manufacturing Survey overview page.
        source_url: Stored verbatim in each record's ``source_url`` field.

    Returns:
        List of ``EmpireStateManufacturingRecord`` instances, one per calendar
        month, sorted ascending by ``survey_date``.

    Raises:
        ValueError: When html is empty, no table is found, no month headers
            are parsed, or no records can be extracted.
    """
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to Empire State Manufacturing parser")

    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table")
    if not table:
        raise ValueError("No table found in Empire State Manufacturing page")

    months = _parse_months_from_thead(table)

    month_data: dict[str, dict[str, Optional[float]]] = {m: {} for m in months}

    tbody = table.find("tbody")
    rows = list(tbody.find_all("tr")) if tbody else []

    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        first_text = cells[0].get_text(strip=True)

        # A single-cell row (possibly with colspan) is a section header.
        if len(cells) == 1:
            if _is_outlook_header(first_text):
                break
            continue

        label = _normalize_label(first_text)
        field_name = _FIELD_MAP.get(label)
        if field_name is None:
            continue

        for i, month in enumerate(months):
            col_idx = i + 1
            if col_idx < len(cells):
                value = _parse_float(cells[col_idx].get_text(strip=True))
                month_data[month][field_name] = value

    fetch_ts = datetime.now(timezone.utc).isoformat()
    records: list[EmpireStateManufacturingRecord] = []

    for month in sorted(month_data.keys()):
        d = month_data[month]
        if not d:
            continue
        records.append(
            EmpireStateManufacturingRecord(
                survey_date=month,
                general_business_conditions=d.get("general_business_conditions"),
                new_orders=d.get("new_orders"),
                shipments=d.get("shipments"),
                unfilled_orders=d.get("unfilled_orders"),
                delivery_time=d.get("delivery_time"),
                inventories=d.get("inventories"),
                prices_paid=d.get("prices_paid"),
                prices_received=d.get("prices_received"),
                number_of_employees=d.get("number_of_employees"),
                avg_workweek=d.get("avg_workweek"),
                capital_expenditures=d.get("capital_expenditures"),
                technology_spending=d.get("technology_spending"),
                source_url=source_url,
                fetch_time=fetch_ts,
            )
        )

    if not records:
        raise ValueError("No records extracted from Empire State Manufacturing table")

    return records


def scrape() -> list[EmpireStateManufacturingRecord]:
    """Fetch the NY Fed Empire State Manufacturing page and parse its data table.

    Delegates HTTP retrieval to ``http_client.fetch``, which enforces
    robots.txt compliance, applies a 2–5 s polite delay before the request,
    and retries 429/5xx responses with exponential backoff (base 2 s, cap
    120 s, up to 5 attempts).  An additional 3 s sleep after the fetch
    maintains courteous crawl behaviour.

    Returns:
        List of ``EmpireStateManufacturingRecord`` instances.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def main() -> int:
    """Scrape Empire State Manufacturing data and upload to BigQuery.

    Uploads records to the ``empire_state_manufacturing`` table, deduplicating
    by survey_date so re-runs are idempotent.

    Returns:
        Count of rows successfully inserted.
    """
    records = scrape()
    return upload_rows("empire_state_manufacturing", records, date_column="survey_date")
