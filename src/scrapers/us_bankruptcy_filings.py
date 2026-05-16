"""US Bankruptcy Court Filing Statistics scraper.

Fetches the bankruptcy statistics page from uscourts.gov and parses quarterly
HTML tables into per-(year, quarter, chapter) filing records. Each column
labelled "Chapter N" becomes one record per row, identified by its chapter
number, while non-chapter columns (e.g. "Total") are ignored.
"""

import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.us_bankruptcy_filings_pb2 import UsCourtsBankruptcyRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.uscourts.gov/statistics-reports/bankruptcies-statistics"

_CHAPTER_RE = re.compile(r"chapter\s+(\d+)", re.IGNORECASE)
_QUARTER_RE = re.compile(r"Q?(\d)$", re.IGNORECASE)


def _parse_chapter(header: str) -> int | None:
    """Extract chapter number from a column header like 'Chapter 7'.

    Args:
        header: Raw text of a table column header cell.

    Returns:
        Integer chapter number, or None when the header does not match
        the pattern 'Chapter <number>'.
    """
    m = _CHAPTER_RE.search(header)
    return int(m.group(1)) if m else None


def _parse_quarter(text: str) -> int | None:
    """Parse a quarter value from a cell, accepting '1'–'4' or 'Q1'–'Q4'.

    Args:
        text: Raw cell text from the quarter column.

    Returns:
        Integer 1–4, or None when the text cannot be parsed as a valid quarter.
    """
    m = _QUARTER_RE.search(text.strip())
    if not m:
        return None
    q = int(m.group(1))
    return q if 1 <= q <= 4 else None


def _parse_filings(text: str) -> int | None:
    """Parse an integer filing count, stripping commas and surrounding whitespace.

    Args:
        text: Raw cell text such as '95,234' or '1532'.

    Returns:
        Integer filing count, or None when the text cannot be converted.
    """
    cleaned = text.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse the US Courts bankruptcy statistics HTML into per-(year, quarter, chapter) records.

    Locates the first table whose header row contains at least one column
    matching 'Chapter N'.  Column roles (year, quarter, chapter) are detected
    from header text.  One record is emitted per (year, quarter, chapter)
    combination.  Year values are propagated forward across rows with a missing
    year cell, which handles HTML rowspan tables without explicit year repetition.
    Rows with unparseable years, quarters, or filing counts are silently skipped.

    Args:
        html: Raw HTML string of the US Courts bankruptcy statistics page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: period_year, period_quarter, chapter,
        filings, source_url.

    Raises:
        ValueError: When no records could be extracted from the page, which
                    aborts any subsequent BigQuery upload.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    for table in soup.find_all("table"):
        thead = table.find("thead")
        header_source = thead if thead is not None else table
        header_row = header_source.find("tr")
        if header_row is None:
            continue

        headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])]
        if len(headers) < 3:
            continue

        year_col: int | None = None
        quarter_col: int | None = None
        chapter_cols: dict[int, int] = {}

        for i, h in enumerate(headers):
            h_lower = h.lower()
            if "year" in h_lower:
                year_col = i
            elif "quarter" in h_lower or h_lower in ("q", "qtr"):
                quarter_col = i
            else:
                ch = _parse_chapter(h)
                if ch is not None:
                    chapter_cols[i] = ch

        if not chapter_cols:
            continue

        if year_col is None:
            year_col = 0
        if quarter_col is None:
            quarter_col = 1

        tbody = table.find("tbody")
        row_source = tbody if tbody is not None else table

        last_year: int | None = None
        for tr in row_source.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue

            # Detect whether this row has its own year cell or relies on rowspan
            # from a previous row. Two rowspan patterns exist:
            #   (a) Empty year cell present: offset stays 0, reuse last_year.
            #   (b) Year cell truly absent (HTML rowspan): cells has one fewer
            #       element; the value at year_col is the quarter, which fails
            #       the 1900-2100 range check and triggers offset=-1.
            offset = 0
            if year_col < len(cells):
                year_text = cells[year_col].get_text(strip=True)
                if year_text:
                    try:
                        candidate = int(year_text)
                        if 1900 <= candidate <= 2100:
                            last_year = candidate
                        else:
                            offset = -1
                    except ValueError:
                        offset = -1
                # Empty year_text: keep offset=0 and reuse last_year (pattern a)
            else:
                offset = -1

            if last_year is None:
                continue

            quarter_adj = quarter_col + offset
            if quarter_adj < 0 or quarter_adj >= len(cells):
                continue
            quarter = _parse_quarter(cells[quarter_adj].get_text(strip=True))
            if quarter is None:
                continue

            for col_idx, chapter in chapter_cols.items():
                adj = col_idx + offset
                if adj < 0 or adj >= len(cells):
                    continue
                count = _parse_filings(cells[adj].get_text(strip=True))
                if count is None:
                    continue
                records.append(
                    {
                        "period_year": last_year,
                        "period_quarter": quarter,
                        "chapter": chapter,
                        "filings": count,
                        "source_url": source_url,
                    }
                )

        if records:
            break

    if not records:
        raise ValueError("No bankruptcy filing records extracted from US Courts page")

    return records


def scrape() -> list[dict]:
    """Fetch the live US Courts bankruptcy statistics page and return parsed records.

    Delegates HTTP fetching to src.scrapers.http_client.fetch, which enforces
    a User-Agent header, a 2–5 s polite delay, and exponential backoff on
    429/5xx responses. An additional 3-second courtesy sleep is applied.

    Returns:
        Same structure as run().

    Raises:
        ValueError: Propagated from run() when no records are extracted.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def _record_to_proto(record: dict) -> UsCourtsBankruptcyRecord:
    msg = UsCourtsBankruptcyRecord()
    msg.period_year = record["period_year"]
    msg.period_quarter = record["period_quarter"]
    msg.chapter = record["chapter"]
    msg.filings = record["filings"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape US Courts bankruptcy filing data and upload records to BigQuery.

    Calls scrape(), converts each record to a UsCourtsBankruptcyRecord proto,
    and uploads via upload_rows. ValueError from scrape() propagates to abort
    the upload when zero records are extracted.

    Returns:
        Count of successfully inserted rows.

    Raises:
        ValueError: When scrape() extracts zero records.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("us_bankruptcy_filings", messages)


if __name__ == "__main__":
    main()
