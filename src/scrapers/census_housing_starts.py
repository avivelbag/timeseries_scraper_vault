"""US Census Bureau Monthly New Residential Construction (Housing Starts) scraper.

Fetches the Census NRC press release HTML, which publishes multi-table data with
regional breakdowns. Each table covers one region (or US totals) with rows for
structure type (1-unit, 2-4 units, 5+ units, Total) and columns organized as
metric groups (Housing Starts, Permits, Completions, Under Construction), each
spanning three period columns (current month, prior month, year-ago month).

One record is emitted per (region, structure_type, period_date) triple.
"""

import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.census_housing_starts_pb2 import CensusHousingStartsRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.census.gov/construction/nrc/www/newresconst.html"

_PARENTHETICAL = re.compile(r"\s*\([^)]*\)")
_FOOTNOTE_START = re.compile(r"^\d+/")

_METRIC_FIELD_MAP: dict[str, str] = {
    "starts": "starts_thousands",
    "permits": "permits_thousands",
    "completions": "completions_thousands",
    "under construction": "under_construction_thousands",
}


def _strip_parens(text: str) -> str:
    """Remove parenthetical suffixes such as (p), (r), or confidence intervals."""
    return _PARENTHETICAL.sub("", text).strip()


def _parse_period_date(raw: str) -> str:
    """Convert a month/year header cell (e.g. 'Jan 2024 (p)') to 'YYYY-MM-01'.

    Strips parenthetical annotations before parsing. Returns the first calendar
    day of the reported month, matching the proto field convention.

    Args:
        raw: Raw cell text from the period header row, e.g. 'Jan 2024 (p)'.

    Returns:
        ISO-8601 date string for the first day of the reported month.

    Raises:
        ValueError: When raw cannot be parsed as a month/year after stripping.
    """
    clean = _strip_parens(raw)
    for fmt in ("%b %Y", "%B %Y"):
        try:
            return datetime.strptime(clean, fmt).strftime("%Y-%m-01")
        except ValueError:
            continue
    raise ValueError(f"Cannot parse period date: {raw!r}")


def _parse_numeric(raw: str) -> float | None:
    """Parse a numeric table cell, stripping commas and parentheticals.

    Args:
        raw: Raw cell text, e.g. '1,234' or '56 (r)' or '--'.

    Returns:
        Float value, or None when the cell is blank/dash/non-numeric.
    """
    clean = _strip_parens(raw).replace(",", "").strip()
    if clean in ("", "-", "--", "NA", "N/A"):
        return None
    try:
        return float(clean)
    except ValueError:
        return None


def _is_footnote_row(cells: list) -> bool:
    """Return True when a table row is a footnote that should be skipped.

    Footnote rows either start with a digit (e.g. '1/ See notes...') or have
    only blank/dash values in all non-label cells.

    Args:
        cells: List of BeautifulSoup Tag objects for all cells in the row.

    Returns:
        True when the row should be skipped as a footnote.
    """
    if not cells:
        return True
    first_text = cells[0].get_text(strip=True)
    if _FOOTNOTE_START.match(first_text):
        return True
    non_label = cells[1:]
    if not non_label:
        return False
    return all(c.get_text(strip=True) in ("", "-", "--", "NA", "N/A") for c in non_label)


def _build_column_map(
    metric_row, period_row
) -> tuple[dict[int, tuple[str, int]], list[str]]:
    """Derive a column index → (field_name, period_idx) mapping from two header rows.

    Reads metric names (with colspan > 1) from metric_row to determine which
    field each column group represents. Reads the first n_periods parseable date
    strings from period_row to build the period_dates list.

    Args:
        metric_row: BeautifulSoup Tag for the row containing metric group headers
            (e.g. 'Housing Starts', 'Building Permits') with colspan attributes.
        period_row: BeautifulSoup Tag for the row containing period date strings
            (e.g. 'Jan 2024 (p)', 'Dec 2023 (r)', 'Jan 2023').

    Returns:
        A tuple of:
            col_map: dict mapping 1-based column index to (field_name, period_idx).
                period_idx 0 = current month, 1 = prior month, 2 = year-ago month.
            period_dates: list of up to n_periods ISO-8601 date strings.
    """
    metric_cells = metric_row.find_all(["th", "td"])
    period_cells = period_row.find_all(["th", "td"])

    metrics_sequence: list[tuple[str, int]] = []
    n_periods = 3
    for cell in metric_cells:
        colspan = int(cell.get("colspan", 1))
        if colspan <= 1:
            continue
        text = _strip_parens(cell.get_text(strip=True)).lower()
        field_name = None
        for pattern, fname in _METRIC_FIELD_MAP.items():
            if pattern in text:
                field_name = fname
                break
        if field_name is not None:
            metrics_sequence.append((field_name, colspan))
            n_periods = colspan

    period_dates: list[str] = []
    for cell in period_cells:
        raw = cell.get_text(strip=True)
        if not raw:
            continue
        try:
            period_dates.append(_parse_period_date(raw))
        except ValueError:
            continue
        if len(period_dates) >= n_periods:
            break

    col_map: dict[int, tuple[str, int]] = {}
    col_idx = 1
    for field_name, n_cols in metrics_sequence:
        for period_idx in range(n_cols):
            col_map[col_idx] = (field_name, period_idx)
            col_idx += 1

    return col_map, period_dates


def _parse_table(table, source_url: str) -> list[dict]:
    """Parse one region table into a list of per-(period, structure_type) records.

    Identifies the region heading (a single `<th colspan>` in the first header
    row), then reads the two subsequent header rows to build a column map, then
    iterates over data rows. Footnote rows are skipped. Rows with missing or
    non-numeric cells emit records with 0.0 for those fields.

    Args:
        table: BeautifulSoup Tag for the `<table>` element.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of record dicts. Empty list when the table lacks a recognisable
        region heading or valid header rows.
    """
    rows = table.find_all("tr")

    region = ""
    metric_row = None
    period_row = None
    data_start_idx = 0
    header_phase = 0

    for i, row in enumerate(rows):
        ths = row.find_all("th")
        if header_phase == 0:
            if len(ths) == 1 and ths[0].get("colspan"):
                region = _strip_parens(ths[0].get_text(strip=True))
                header_phase = 1
        elif header_phase == 1:
            metric_row = row
            header_phase = 2
        elif header_phase == 2:
            period_row = row
            data_start_idx = i + 1
            break

    if not region or metric_row is None or period_row is None:
        return []

    col_map, period_dates = _build_column_map(metric_row, period_row)
    if not col_map or not period_dates:
        return []

    records: list[dict] = []
    for row in rows[data_start_idx:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        if _is_footnote_row(cells):
            continue

        structure_type = cells[0].get_text(strip=True)
        if not structure_type:
            continue

        period_data: dict[int, dict[str, float]] = {}
        for col_idx, (field_name, period_idx) in col_map.items():
            if col_idx >= len(cells):
                continue
            val = _parse_numeric(cells[col_idx].get_text(strip=True))
            if val is None:
                continue
            if period_idx not in period_data:
                period_data[period_idx] = {}
            period_data[period_idx][field_name] = val

        for period_idx, metrics in period_data.items():
            if period_idx >= len(period_dates):
                continue
            period_date = period_dates[period_idx]
            if not period_date:
                continue
            records.append(
                {
                    "period_date": period_date,
                    "region": region,
                    "structure_type": structure_type,
                    "starts_thousands": metrics.get("starts_thousands", 0.0),
                    "permits_thousands": metrics.get("permits_thousands", 0.0),
                    "completions_thousands": metrics.get("completions_thousands", 0.0),
                    "under_construction_thousands": metrics.get(
                        "under_construction_thousands", 0.0
                    ),
                    "source_url": source_url,
                }
            )

    return records


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse Census NRC press release HTML into per-(region, structure, period) records.

    Iterates over all `<table>` elements, calling _parse_table for each. Tables
    without a recognised region heading (single `<th colspan>` first row) are
    silently skipped. Footnote rows within tables are also silently skipped.

    Args:
        html: Raw HTML string of the Census NRC press release page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: period_date, region, structure_type,
        starts_thousands, permits_thousands, completions_thousands,
        under_construction_thousands, source_url.

    Raises:
        ValueError: When html is empty or no records could be extracted.
    """
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to Census housing starts parser")

    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")

    if not tables:
        raise ValueError("No tables found in Census housing starts page")

    records: list[dict] = []
    for table in tables:
        records.extend(_parse_table(table, source_url))

    if not records:
        raise ValueError("No records extracted from Census housing starts page")

    return records


def scrape() -> list[dict]:
    """Fetch the live Census NRC press release page and return parsed records.

    Delegates HTTP fetching to src.scrapers.http_client.fetch, which enforces
    robots.txt compliance, a polite 2-5 s delay, and exponential backoff on
    429/5xx responses. An additional 3-second courtesy sleep is applied after
    the response is received.

    Returns:
        Same structure as run().

    Raises:
        ValueError: Propagated from run() when no records are extracted.
        RuntimeError: When robots.txt disallows the target URL.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def _record_to_proto(record: dict) -> CensusHousingStartsRecord:
    """Convert a parsed record dict to a CensusHousingStartsRecord dataclass.

    Args:
        record: Dict as returned by run().

    Returns:
        Populated CensusHousingStartsRecord instance with fetch_time set to the
        current UTC time in ISO-8601 format.
    """
    msg = CensusHousingStartsRecord()
    msg.period_date = record["period_date"]
    msg.region = record["region"]
    msg.structure_type = record["structure_type"]
    msg.starts_thousands = record["starts_thousands"]
    msg.permits_thousands = record["permits_thousands"]
    msg.completions_thousands = record["completions_thousands"]
    msg.under_construction_thousands = record["under_construction_thousands"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape Census housing starts data and upload records to BigQuery.

    Calls scrape(), converts each record to a CensusHousingStartsRecord proto
    stub, and uploads via upload_rows with period_date as the dedup column.

    Returns:
        Count of successfully inserted rows.

    Raises:
        ValueError: When scrape() extracts zero records.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("census_housing_starts", messages, date_column="period_date")


if __name__ == "__main__":
    main()
