"""BLS Employment Situation HTML-table scraper.

Fetches the monthly Employment Situation press release from the BLS website
and parses multi-level HTML tables (colspan/rowspan headers) to extract four
key seasonally-adjusted series: total nonfarm payrolls, unemployment rate,
average hourly earnings, and average weekly hours.

BLS-specific parsing details:
- Value cells carry trailing footnote markers: 'p' (preliminary) or 'r' (revised).
  These are stripped before float conversion; 'p' sets preliminary=True.
- Cells containing '(1)' denote suppressed data and are skipped.
- Row labels use &nbsp; characters for subcategory indentation; these are
  normalised to plain spaces so keyword matching is reliable.
- The page header uses a two-row structure: the first row holds year values with
  colspan attributes; the second row holds month abbreviations (e.g. 'Oct.',
  'Mar.p'). The flatten-then-match approach in _build_header_grid handles this.
"""

import re
import time
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup, Tag

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.bls_employment_situation_pb2 import BLSEmploymentRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.bls.gov/news.release/empsit.htm"

REQUIRED_FIELDS: list[str] = [
    "period_year", "period_month", "series_id", "series_label", "value",
]

_MONTH_ABBR: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_PRELIMINARY_RE = re.compile(r"p$", re.IGNORECASE)
_FOOTNOTE_SUFFIX = re.compile(r"[a-zA-Z]+$")

_TARGET_SERIES: list[dict[str, Any]] = [
    {
        "keywords": ["total nonfarm"],
        "series_id": "CES0000000001",
        "series_label": "Total nonfarm payrolls",
        "units": "thousands",
    },
    {
        "keywords": ["unemployment rate"],
        "series_id": "LNS14000000",
        "series_label": "Unemployment rate",
        "units": "percent",
    },
    {
        "keywords": ["average hourly earnings"],
        "series_id": "CES0500000003",
        "series_label": "Average hourly earnings, private",
        "units": "dollars",
    },
    {
        "keywords": ["average weekly hours"],
        "series_id": "CES0500000002",
        "series_label": "Average weekly hours, private",
        "units": "hours",
    },
]


def _build_header_grid(thead_rows: list[Tag]) -> dict[tuple[int, int], str]:
    """Flatten a multi-row HTML table header into a (row, col) -> text mapping.

    Processes rowspan and colspan attributes so that each logical grid position
    maps to the text of the cell that occupies it.  When a cell spans multiple
    rows or columns, the same text is written to every covered position.

    Args:
        thead_rows: Ordered list of <tr> Tag elements from the table's <thead>.

    Returns:
        Dict keyed by (row_index, col_index) tuples with cell text as values.
    """
    grid: dict[tuple[int, int], str] = {}
    for row_idx, tr in enumerate(thead_rows):
        col_idx = 0
        for cell in tr.find_all(["th", "td"]):
            while (row_idx, col_idx) in grid:
                col_idx += 1
            rowspan = int(str(cell.get("rowspan") or 1))
            colspan = int(str(cell.get("colspan") or 1))
            text = cell.get_text(separator=" ", strip=True)
            for r in range(rowspan):
                for c in range(colspan):
                    grid[(row_idx + r, col_idx + c)] = text
            col_idx += colspan
    return grid


def _col_periods(
    grid: dict[tuple[int, int], str], n_header_rows: int
) -> dict[int, tuple[int, int]]:
    """Map column indices to (year, month) pairs by scanning the header grid.

    Reads the last header row for month abbreviations (e.g. 'Oct.', 'Mar.p').
    For each month column, searches upward through earlier rows for a four-digit
    year.  Falls back to extracting a year embedded in the month cell itself
    (e.g. 'Jan 2025').  Columns with no identifiable (year, month) are omitted.

    Args:
        grid: Header grid produced by _build_header_grid.
        n_header_rows: Total number of header rows.

    Returns:
        Dict mapping col_index -> (year, month) for columns that carry time-
        series data.
    """
    last_row = n_header_rows - 1
    last_row_cols: dict[int, str] = {
        col: text for (row, col), text in grid.items() if row == last_row
    }

    periods: dict[int, tuple[int, int]] = {}
    for col_idx, text in sorted(last_row_cols.items()):
        alpha_only = re.sub(r"[^a-zA-Z]", "", text)
        month = _MONTH_ABBR.get(alpha_only[:3].lower())
        if month is None:
            continue

        year: int | None = None
        for row_idx in range(last_row - 1, -1, -1):
            val = grid.get((row_idx, col_idx), "")
            m = re.search(r"\b(19|20)\d{2}\b", val)
            if m:
                year = int(m.group())
                break

        if year is None:
            m = re.search(r"\b(19|20)\d{2}\b", text)
            if m:
                year = int(m.group())

        if year is not None:
            periods[col_idx] = (year, month)

    return periods


def _parse_cell_value(raw: str) -> tuple[float | None, bool]:
    """Parse a BLS table value cell, stripping BLS footnote markers.

    Cells containing '(1)' (suppressed data) or blank cells return (None, False).
    Trailing 'p' (preliminary) and 'r' (revised) letters are stripped before
    float conversion.  Commas used as thousands separators are also removed.

    Args:
        raw: Raw text content of a single table data cell.

    Returns:
        Tuple (value, preliminary) where value is the parsed float or None when
        the cell carries no numeric data, and preliminary is True only when the
        'p' marker was present.
    """
    text = raw.strip()
    if not text or text in ("(1)", "—", "–", "...", "N/A"):
        return None, False

    preliminary = bool(_PRELIMINARY_RE.search(text))
    text = _FOOTNOTE_SUFFIX.sub("", text)
    text = text.replace(",", "").strip()

    if not text:
        return None, False
    try:
        return float(text), preliminary
    except ValueError:
        return None, False


def _match_series(label: str) -> dict[str, Any] | None:
    """Return the matching target series config for a data row label, or None.

    Normalises the label by converting &nbsp; to spaces, stripping surrounding
    whitespace, and lowercasing before comparing against keyword lists.

    Args:
        label: Text content of the first cell in a table body row.

    Returns:
        The matching entry from _TARGET_SERIES, or None if no keywords match.
    """
    normalised = label.replace("\xa0", " ").strip().lower()
    for series in _TARGET_SERIES:
        if all(kw in normalised for kw in series["keywords"]):
            return series
    return None


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse a BLS Employment Situation HTML page into a flat list of records.

    Scans all <table> elements that have a multi-row <thead> (at least two <tr>
    rows).  For each such table, builds a column-to-period mapping and then
    iterates tbody rows to identify target series by keyword matching on the
    first cell.  Records are deduplicated by (period_year, period_month,
    series_id); the first occurrence (from earlier tables on the page) is kept.

    Cells that are blank, contain '(1)', or cannot be converted to float are
    silently skipped.  Trailing 'p'/'r' markers are stripped; 'p' sets the
    preliminary flag.

    Args:
        html: Raw HTML string of the BLS employment situation press release.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: period_year, period_month, series_id,
        series_label, value, units, preliminary, source_url.
        Empty list when no matching tables or series are found.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []
    seen: set[tuple[int, int, str]] = set()

    for table in soup.find_all("table"):
        thead = table.find("thead")
        if thead is None:
            continue

        thead_rows: list[Tag] = thead.find_all("tr")
        if len(thead_rows) < 2:
            continue

        grid = _build_header_grid(thead_rows)
        periods = _col_periods(grid, len(thead_rows))
        if not periods:
            continue

        tbody = table.find("tbody")
        if tbody is None:
            continue

        for tr in tbody.find_all("tr"):
            cells: list[Tag] = tr.find_all(["td", "th"])
            if not cells:
                continue

            label_text = cells[0].get_text(separator=" ")
            series_info = _match_series(label_text)
            if series_info is None:
                continue

            for col_idx, (year, month) in periods.items():
                dedup_key = (year, month, series_info["series_id"])
                if dedup_key in seen:
                    continue
                if col_idx >= len(cells):
                    continue

                cell_text = cells[col_idx].get_text(strip=True)
                value, preliminary = _parse_cell_value(cell_text)
                if value is None:
                    continue

                seen.add(dedup_key)
                records.append(
                    {
                        "period_year": year,
                        "period_month": month,
                        "series_id": series_info["series_id"],
                        "series_label": series_info["series_label"],
                        "value": value,
                        "units": series_info["units"],
                        "preliminary": preliminary,
                        "source_url": source_url,
                    }
                )

    return records


def scrape() -> list[dict]:
    """Fetch the live BLS Employment Situation page and return parsed records.

    Applies a mandatory 3-second post-fetch delay per BLS rate-limit guidance.
    HTTP fetching is delegated to src.scrapers.http_client.fetch, which enforces
    the User-Agent header, a 2–5 s polite pre-request delay, and exponential
    backoff on 429/5xx responses (up to 5 retries, base 5 s).

    Returns:
        Same structure as run().

    Raises:
        RuntimeError: If robots.txt disallows the URL.
        requests.HTTPError: On a non-retryable HTTP error.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def _record_to_proto(record: dict) -> BLSEmploymentRecord:
    """Convert a record dict into a BLSEmploymentRecord proto message.

    Sets fetch_time to the current UTC instant in ISO-8601 format.

    Args:
        record: Dict as returned by run(), containing all REQUIRED_FIELDS.

    Returns:
        Populated BLSEmploymentRecord instance.
    """
    msg = BLSEmploymentRecord()
    msg.period_year = record["period_year"]
    msg.period_month = record["period_month"]
    msg.series_id = record["series_id"]
    msg.series_label = record["series_label"]
    msg.value = record["value"]
    msg.units = record["units"]
    msg.preliminary = record["preliminary"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape BLS Employment Situation data and upload records to BigQuery.

    Returns:
        Count of successfully inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("bls_employment_situation", messages)


if __name__ == "__main__":
    main()
