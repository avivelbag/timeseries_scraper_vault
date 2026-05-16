"""Federal Reserve G.17 Industrial Production and Capacity Utilization scraper.

Fetches the G.17 statistical release HTML page, which publishes monthly
industrial production indexes and capacity utilization rates in multi-level
HTML tables. Each table has a two-row thead: the first row labels column
groups (e.g., "Total Industry", "Manufacturing") with colspan, and the second
row labels sub-series (e.g., "Index", "% of capacity"). Data rows start with
a date string (YYYY-MM format) in the first <td>.

One FedG17Record is emitted per (date, column_group, sub_series) triple.
Cells containing "n.a." or footnote markers yield index_value=-1.0.
"""

import logging
import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from protos.fed_g17_industrial_production_pb2 import FedG17Record  # type: ignore[attr-defined]
from src.scrapers.http_client import fetch

SOURCE_URL = "https://www.federalreserve.gov/releases/G17/current/default.htm"

_log = logging.getLogger(__name__)

_FOOTNOTE_RE = re.compile(r"^\d+/")
_DATE_RE = re.compile(r"^\d{4}-\d{2}$")

_SENTINEL = -1.0

_CAPACITY_KEYWORDS = ("% of capacity", "percent of capacity", "utilization")
_INDEX_KEYWORDS = ("index",)


def _clean(text: str) -> str:
    """Strip whitespace and non-breaking spaces from a cell's text."""
    return text.replace("\xa0", " ").strip()


def _is_suppressed(text: str) -> bool:
    """Return True when a cell value is missing/suppressed (n.a., --, blank)."""
    return text in ("", "n.a.", "na", "NA", "--", "-", "N.A.")


def _parse_value(raw: str) -> float:
    """Parse a numeric cell, returning _SENTINEL for suppressed/footnote values.

    Args:
        raw: Cleaned cell text, e.g. '102.4' or 'n.a.' or '1/ preliminary'.

    Returns:
        Float value on success, or _SENTINEL when cell is suppressed or
        starts with a footnote marker.
    """
    clean = _clean(raw).replace(",", "")
    if _is_suppressed(clean) or _FOOTNOTE_RE.match(clean):
        _log.warning("Suppressed or footnote cell value: %r — using sentinel %s", raw, _SENTINEL)
        return _SENTINEL
    try:
        return float(clean)
    except ValueError:
        _log.warning("Cannot parse cell value %r as float — using sentinel %s", raw, _SENTINEL)
        return _SENTINEL


def _classify_subseries(sub_label: str) -> str:
    """Determine whether a sub-series label describes an index or capacity utilization.

    Args:
        sub_label: Cleaned text from the second header row, e.g. 'Index' or
            '% of capacity'.

    Returns:
        'index' for production index columns, 'utilization' for capacity
        utilization columns, or the raw label lowercased for anything else.
    """
    lower = sub_label.lower()
    for kw in _CAPACITY_KEYWORDS:
        if kw in lower:
            return "utilization"
    for kw in _INDEX_KEYWORDS:
        if kw in lower:
            return "index"
    return lower


def _build_column_schema(header_row1: Tag, header_row2: Tag) -> list[tuple[str, str, int]]:
    """Build a list of (group_label, sub_label, col_index) tuples from two header rows.

    Reads the first header row for column groups (with colspan); reads the
    second header row for sub-series labels. Skips the leading rowspan cell
    (the Period/Date label column at index 0).

    Args:
        header_row1: BeautifulSoup Tag for the first <tr> of <thead>, containing
            column group headers with colspan attributes.
        header_row2: BeautifulSoup Tag for the second <tr> of <thead>, containing
            individual sub-series labels.

    Returns:
        List of (group_label, sub_label, col_index) tuples where col_index is
        the 0-based index into a data row's cells list (including the date cell
        at index 0). Returns empty list when no valid groups are found.
    """
    group_cells = header_row1.find_all(["th", "td"])
    sub_cells = header_row2.find_all(["th", "td"])

    groups: list[tuple[str, int]] = []
    for cell in group_cells:
        label = _clean(cell.get_text())
        if not label:
            continue
        colspan = int(cell.get("colspan", 1))
        rowspan = int(cell.get("rowspan", 1))
        if rowspan > 1:
            continue
        groups.append((label, colspan))

    schema: list[tuple[str, str, int]] = []
    col_idx = 1
    sub_idx = 0
    for group_label, colspan in groups:
        for _ in range(colspan):
            if sub_idx < len(sub_cells):
                sub_label = _clean(sub_cells[sub_idx].get_text())
                sub_idx += 1
            else:
                sub_label = ""
            schema.append((group_label, sub_label, col_idx))
            col_idx += 1

    return schema


def _parse_table(table: Tag, source_url: str, fetch_ts: str) -> list[FedG17Record]:
    """Parse one G.17 data table into a list of FedG17Record instances.

    Identifies the two-row thead and then iterates over tbody rows. Rows whose
    first cell is not in YYYY-MM format are skipped (footnotes, blank rows).
    For each data cell, emits a FedG17Record with the appropriate fields. Cells
    classified as 'index' populate index_value; cells classified as
    'utilization' populate capacity_utilization_pct on the preceding index
    record when the series_id prefix matches.

    Because an 'index' and 'utilization' column pair share the same group
    label, we post-process: for each (date, group) pair we merge the index
    and utilization values into a single record, yielding one record per
    (date, group) tuple.

    Args:
        table: BeautifulSoup Tag for a <table> element.
        source_url: URL the page was fetched from.
        fetch_ts: ISO-8601 timestamp string for when the page was fetched.

    Returns:
        List of FedG17Record instances. Empty list when the table lacks a
        recognisable two-row header or no date rows.
    """
    thead = table.find("thead")
    tbody = table.find("tbody")

    if not thead or not tbody:
        return []

    header_rows = thead.find_all("tr")
    if len(header_rows) < 2:
        return []

    schema = _build_column_schema(header_rows[0], header_rows[1])
    if not schema:
        return []

    records_map: dict[tuple[str, str], dict] = {}

    for row in tbody.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        date_text = _clean(cells[0].get_text())
        if not _DATE_RE.match(date_text):
            continue

        for group_label, sub_label, col_idx in schema:
            if col_idx >= len(cells):
                continue
            raw = _clean(cells[col_idx].get_text())
            value = _parse_value(raw)
            kind = _classify_subseries(sub_label)

            series_id = f"{group_label}.{sub_label}"
            key = (date_text, group_label)

            if key not in records_map:
                records_map[key] = {
                    "group_label": group_label,
                    "date": date_text,
                    "index_value": _SENTINEL,
                    "capacity_utilization_pct": None,
                }

            entry = records_map[key]
            if kind == "index":
                entry["index_value"] = value
                entry["index_series_id"] = series_id
                entry["index_sub_label"] = sub_label
            elif kind == "utilization":
                entry["capacity_utilization_pct"] = value if value != _SENTINEL else None
                if value == _SENTINEL:
                    entry["capacity_utilization_pct"] = None
            else:
                entry["index_value"] = value
                entry.setdefault("index_series_id", series_id)
                entry.setdefault("index_sub_label", sub_label)

    result: list[FedG17Record] = []
    for (date_text, group_label), entry in records_map.items():
        series_id = entry.get("index_series_id", f"{group_label}.Index")
        sub_label = entry.get("index_sub_label", "Index")
        rec = FedG17Record()
        rec.series_id = series_id
        rec.series_name = f"{group_label} — {sub_label}"
        rec.reference_date = date_text
        rec.index_value = entry["index_value"]
        rec.capacity_utilization_pct = entry["capacity_utilization_pct"]
        rec.unit = "Index 2017=100"
        rec.source_url = source_url
        rec.fetch_time = fetch_ts
        result.append(rec)

    return result


def run(html: str, source_url: str = SOURCE_URL) -> list[FedG17Record]:
    """Parse G.17 release HTML into a list of FedG17Record instances.

    Finds all <table> elements on the page and delegates each to _parse_table.
    Tables without a recognisable two-row thead are silently skipped.

    Args:
        html: Raw HTML string of the G.17 release page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of FedG17Record dataclass instances.

    Raises:
        ValueError: When html is empty or no records could be extracted.
    """
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to G.17 parser")

    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")

    if not tables:
        raise ValueError("No tables found in G.17 page")

    fetch_ts = datetime.now(timezone.utc).isoformat()
    records: list[FedG17Record] = []
    for table in tables:
        records.extend(_parse_table(table, source_url, fetch_ts))

    if not records:
        raise ValueError("No records extracted from G.17 page")

    return records


def scrape() -> list[FedG17Record]:
    """Fetch the live G.17 release page and return parsed FedG17Record instances.

    Delegates HTTP fetching to src.scrapers.http_client.fetch, which enforces
    robots.txt compliance, a polite 2–5 s delay, and exponential backoff on
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
