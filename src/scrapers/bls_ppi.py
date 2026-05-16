"""BLS Producer Price Index (PPI) HTML-table scraper.

Fetches the PPI landing page, follows links to the 'All commodities' and
'Final demand' series tables, parses multi-level HTML thead rows, and emits
BLSPpiRecord protos.  One record is emitted per (series_id, year, month) triple
that contains a parseable numeric value.  Annual and HALF columns are skipped.
"""

import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.http_client import fetch
from protos.bls_ppi_pb2 import BLSPpiRecord  # type: ignore[attr-defined]

LANDING_URL = "https://www.bls.gov/ppi/tables.htm"

REQUIRED_FIELDS: list[str] = [
    "series_id",
    "commodity_description",
    "period",
    "index_value",
    "preliminary",
    "percent_change_1m",
    "percent_change_12m",
    "source_url",
]

_MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

_SKIP_COLUMNS = {"Annual", "HALF1", "HALF2", "HalfYear1", "HalfYear2"}

_NON_NUMERIC = re.compile(r"[^\d.]")
_PRELIMINARY_RE = re.compile(r"\(P\)", re.I)
_SERIES_ID_RE = re.compile(r"Series\s+Id[:\s]+([A-Z0-9]{6,})", re.I)
_ITEM_RE = re.compile(r"^Item:\s*(.+?)(?:\s+Base Period|$)", re.MULTILINE | re.I)

_TABLE_LINK_TEXTS = ("all commodities", "final demand")


def _parse_value(raw: str) -> tuple[float | None, bool]:
    """Strip the '(P)' preliminary marker and convert the remaining text to float.

    Returns (None, is_preliminary) when the cell holds a non-numeric placeholder
    such as '—' or is empty, so callers can skip unreported periods without
    crashing.

    Args:
        raw: Raw text from a table cell, possibly containing a '(P)' suffix,
            an em-dash placeholder, or HTML-decoded entities.

    Returns:
        Tuple of (parsed float or None, preliminary flag).
    """
    preliminary = bool(_PRELIMINARY_RE.search(raw))
    cleaned = _NON_NUMERIC.sub("", raw).strip()
    if not cleaned:
        return None, preliminary
    try:
        return float(cleaned), preliminary
    except ValueError:
        return None, preliminary


def _compute_changes(records: list[dict]) -> None:
    """Compute month-over-month and year-over-year percent changes in place.

    Builds an (year, month) lookup from all records on the page, then fills
    percent_change_1m and percent_change_12m for each record.  Records with no
    prior-period value in the lookup receive 0.0.

    Args:
        records: List of partially-built record dicts that must contain '_year',
            '_month', and 'index_value' keys.
    """
    value_map: dict[tuple[int, int], float] = {
        (r["_year"], r["_month"]): r["index_value"] for r in records
    }
    for r in records:
        y, m = r["_year"], r["_month"]
        prev_month_key = (y, m - 1) if m > 1 else (y - 1, 12)
        prev_year_key = (y - 1, m)
        curr = r["index_value"]
        prev_m = value_map.get(prev_month_key)
        prev_y = value_map.get(prev_year_key)
        r["percent_change_1m"] = (
            round((curr - prev_m) / prev_m * 100, 3) if prev_m else 0.0
        )
        r["percent_change_12m"] = (
            round((curr - prev_y) / prev_y * 100, 3) if prev_y else 0.0
        )


def _extract_meta(soup: BeautifulSoup) -> tuple[str, str]:
    """Extract series_id and commodity_description from a BLS data page.

    Searches the full page text for a 'Series Id:' label and an 'Item:' field
    using the paragraph format that BLS data output pages emit.  Falls back to
    empty strings if either pattern is absent.

    Args:
        soup: Parsed BeautifulSoup object for the page.

    Returns:
        Tuple of (series_id, commodity_description).
    """
    # get_text with newline separator keeps 'Item:' on its own line so the
    # multiline regex can terminate the capture at end-of-line rather than
    # scanning across block elements.
    page_text = soup.get_text("\n")

    series_id = ""
    id_match = _SERIES_ID_RE.search(page_text)
    if id_match:
        series_id = id_match.group(1).strip()

    commodity_description = ""
    item_match = _ITEM_RE.search(page_text)
    if item_match:
        commodity_description = item_match.group(1).strip()

    return series_id, commodity_description


def run(html: str, source_url: str = LANDING_URL) -> list[dict]:
    """Parse a BLS PPI HTML table page into a list of PPI records.

    Scans all ``<table>`` elements for the first one whose thead contains a row
    with at least one standard month-name header.  The row immediately above
    the month-header row may carry a colspan description — this is used as
    context but does not affect column mapping.  For each tbody row the year is
    read from the first cell; subsequent cells are matched to months by column
    index.  '(P)' cells are parsed and flagged preliminary.  '—' and empty
    cells are silently skipped.  Annual/HALF columns are excluded.

    After collecting all (year, month, value) triples, month-over-month and
    year-over-year percent changes are computed and stored in each record.

    Args:
        html: Raw HTML string of a BLS PPI table page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys matching REQUIRED_FIELDS.  fetch_time is
        omitted — callers that need it should add it after this function.
    """
    if not html.strip():
        return []

    soup = BeautifulSoup(html, "lxml")
    series_id, commodity_description = _extract_meta(soup)

    raw_records: list[dict] = []

    for table in soup.find_all("table"):
        thead = table.find("thead")
        header_rows = thead.find_all("tr") if thead else table.find_all("tr")[:3]

        month_col_map: dict[int, int] = {}
        for row in header_rows:
            cells = row.find_all(["th", "td"])
            for cell in cells:
                for sup in cell.find_all("sup"):
                    sup.decompose()
            headers = [c.get_text(strip=True) for c in cells]
            col_map: dict[int, int] = {}
            for idx, h in enumerate(headers):
                if h in _SKIP_COLUMNS:
                    continue
                if h in _MONTH_NAMES:
                    col_map[idx] = _MONTH_NAMES.index(h) + 1
            if col_map:
                month_col_map = col_map
                break

        if not month_col_map:
            continue

        # Fall back to extracting series_id from the first thead row if the
        # page-level paragraph did not supply one.
        if not series_id and header_rows:
            first_text = header_rows[0].get_text(" ", strip=True)
            id_match = _SERIES_ID_RE.search(first_text)
            if id_match:
                series_id = id_match.group(1).strip()

        tbody = table.find("tbody")
        data_rows = tbody.find_all("tr") if tbody else table.find_all("tr")

        for tr in data_rows:
            cells_els = tr.find_all(["td", "th"])
            for cell in cells_els:
                for sup in cell.find_all("sup"):
                    sup.decompose()
            cells = [el.get_text(strip=True) for el in cells_els]
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
                val, prelim = _parse_value(cells[col_idx])
                if val is None:
                    continue
                raw_records.append(
                    {
                        "series_id": series_id,
                        "commodity_description": commodity_description,
                        "period": f"{year:04d}-{month_num:02d}",
                        "index_value": val,
                        "preliminary": prelim,
                        "source_url": source_url,
                        "_year": year,
                        "_month": month_num,
                    }
                )

        if raw_records:
            break

    if not raw_records:
        return []

    _compute_changes(raw_records)

    for r in raw_records:
        r.pop("_year")
        r.pop("_month")

    return raw_records


def _find_table_links(html: str, base_url: str = LANDING_URL) -> list[str]:
    """Find PPI table page links from the landing page HTML.

    Searches all anchor elements whose visible text contains 'all commodities'
    or 'final demand' (case-insensitive).  Relative URLs are resolved against
    base_url.  Duplicate URLs are deduplicated while preserving order.

    Args:
        html: Raw HTML of the BLS PPI landing page.
        base_url: Base URL for resolving relative hrefs.

    Returns:
        Ordered list of unique absolute URLs pointing to table pages.
    """
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if any(kw in text for kw in _TABLE_LINK_TEXTS):
            href = urljoin(base_url, a["href"])
            if href not in seen:
                seen.add(href)
                links.append(href)
    return links


def scrape() -> list[dict]:
    """Fetch the PPI landing page, follow table links, and return parsed records.

    Fetches https://www.bls.gov/ppi/tables.htm, discovers links whose text
    contains 'All commodities' or 'Final demand', then fetches and parses each
    linked page.  A 3-second courtesy delay is applied after every request per
    BLS rate-limit guidance.  HTTP retries and robots.txt compliance are handled
    by src.http_client.fetch.

    Returns:
        Combined list of records from all discovered table pages.
    """
    landing_resp = fetch(LANDING_URL)
    time.sleep(3)

    table_urls = _find_table_links(landing_resp.text, LANDING_URL)
    all_records: list[dict] = []

    for url in table_urls:
        resp = fetch(url)
        time.sleep(3)
        all_records.extend(run(resp.text, source_url=url))

    return all_records


def _record_to_proto(record: dict) -> BLSPpiRecord:
    msg = BLSPpiRecord()
    msg.series_id = record["series_id"]
    msg.commodity_description = record["commodity_description"]
    msg.period = record["period"]
    msg.index_value = record["index_value"]
    msg.preliminary = record["preliminary"]
    msg.percent_change_1m = record["percent_change_1m"]
    msg.percent_change_12m = record["percent_change_12m"]
    msg.source_url = record["source_url"]
    msg.fetch_time.FromDatetime(datetime.now(timezone.utc))
    return msg


def main() -> int:
    """Scrape BLS PPI data and upload records to BigQuery.

    Calls scrape(), converts each record to a BLSPpiRecord proto, and uploads
    via upload_rows.  Returns the count of successfully inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("bls_ppi", messages)


if __name__ == "__main__":
    main()
