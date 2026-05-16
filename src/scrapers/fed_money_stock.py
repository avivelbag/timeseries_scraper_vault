"""Federal Reserve H.6 Money Stock Measures (M1/M2) weekly scraper.

Fetches the H.6 release page from federalreserve.gov and parses the HTML table
into per-date records containing seasonally-adjusted M1 and M2 money stock
figures in billions of dollars.  Each table row represents one weekly
observation; footnote rows (blank dates, asterisk prefixes) are skipped.
"""

import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.fed_money_stock_pb2 import FedMoneyStockRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.federalreserve.gov/releases/h6/current/default.htm"


def _parse_date(date_str: str) -> str | None:
    """Normalise an H.6 row date string to ISO-8601 YYYY-MM-DD.

    Collapses internal whitespace before parsing so that strings like
    "May  6, 2026" (double space before single-digit day) are handled
    correctly.  Returns None for blank strings, strings starting with an
    asterisk (footnote markers), or strings that match no recognised format.

    Args:
        date_str: Raw text from the first cell of a table row.

    Returns:
        ISO-8601 date string "YYYY-MM-DD", or None when parsing fails.
    """
    normalized = re.sub(r"\s+", " ", date_str).strip()
    if not normalized or normalized.startswith("*"):
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(normalized, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _find_column_indices(header_cells: list) -> tuple[int | None, int | None]:
    """Locate M1 and M2 column positions within a header row.

    Scans header cells for text containing "M1" and "M2" as word-boundary
    tokens (case-insensitive).  Returns the absolute column indices of the
    first occurrence of each.

    Args:
        header_cells: List of BeautifulSoup Tag objects representing the
                      cells of a <thead> row.

    Returns:
        Tuple (m1_idx, m2_idx) where each element is the absolute column
        index of that series header, or None if the header was not found.
    """
    m1_idx: int | None = None
    m2_idx: int | None = None
    for i, cell in enumerate(header_cells):
        text = cell.get_text(strip=True).upper()
        if m1_idx is None and re.search(r"\bM1\b", text):
            m1_idx = i
        if m2_idx is None and re.search(r"\bM2\b", text):
            m2_idx = i
    return m1_idx, m2_idx


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse a Federal Reserve H.6 HTML page into per-date money stock records.

    Locates the first table whose header row contains both M1 and M2 column
    headers.  Iterates all body rows, reading the series date from column 0
    and the M1/M2 values from the mapped column indices.  Rows with an absent
    or unparseable date (blank, asterisk-prefixed, unrecognised format) are
    silently skipped, as are rows where M1 or M2 cannot be parsed as float.

    Args:
        html: Raw HTML string of the H.6 release page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: series_date, m1_seasonally_adjusted_billions,
        m2_seasonally_adjusted_billions, source_url.  Empty list when no
        qualifying table is found or all rows are skipped.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    for table in soup.find_all("table"):
        thead = table.find("thead")
        if thead is None:
            continue
        header_row = thead.find("tr")
        if header_row is None:
            continue

        header_cells = header_row.find_all(["th", "td"])
        if len(header_cells) < 3:
            continue

        m1_idx, m2_idx = _find_column_indices(header_cells)
        if m1_idx is None or m2_idx is None:
            continue

        tbody = table.find("tbody")
        if tbody is None:
            continue

        for tr in tbody.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue

            series_date = _parse_date(cells[0].get_text(strip=True))
            if series_date is None:
                continue

            if m1_idx >= len(cells) or m2_idx >= len(cells):
                continue

            try:
                m1 = float(cells[m1_idx].get_text(strip=True).replace(",", ""))
                m2 = float(cells[m2_idx].get_text(strip=True).replace(",", ""))
            except ValueError:
                continue

            records.append(
                {
                    "series_date": series_date,
                    "m1_seasonally_adjusted_billions": m1,
                    "m2_seasonally_adjusted_billions": m2,
                    "source_url": source_url,
                }
            )

        if records:
            break

    return records


def scrape() -> list[dict]:
    """Fetch the live H.6 page and return parsed money stock records.

    Delegates HTTP fetching to src.scrapers.http_client.fetch, which enforces
    a User-Agent header, a 2–5 s polite delay, and exponential backoff on
    429/5xx responses.  An additional 3-second sleep is applied as a courtesy
    to the Federal Reserve server.

    Returns:
        Same structure as run().
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def _record_to_proto(record: dict) -> FedMoneyStockRecord:
    msg = FedMoneyStockRecord()
    msg.series_date = record["series_date"]
    msg.m1_seasonally_adjusted_billions = record["m1_seasonally_adjusted_billions"]
    msg.m2_seasonally_adjusted_billions = record["m2_seasonally_adjusted_billions"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape H.6 money stock data and upload records to BigQuery.

    Calls scrape(), converts each record to a FedMoneyStockRecord proto, and
    uploads via upload_rows to the 'fed_money_stock' table.

    Returns:
        Count of successfully inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("fed_money_stock", messages)


if __name__ == "__main__":
    main()
