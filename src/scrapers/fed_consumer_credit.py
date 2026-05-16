"""Federal Reserve Consumer Credit G.19 HTML table scraper.

Fetches the G.19 release page from federalreserve.gov and parses HTML tables
into per-(series, date) records covering Total, Revolving, and Non-revolving
consumer credit outstanding in billions of dollars.  Two table variants are
processed: Seasonally Adjusted and Not Seasonally Adjusted.
"""

import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.fed_consumer_credit_pb2 import FedConsumerCreditRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.federalreserve.gov/releases/g19/current/default.htm"

_MONTH_YEAR = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
    re.IGNORECASE,
)


def _parse_month_year(text: str) -> str | None:
    """Convert a month-year string to a YYYY-MM-DD date (first day of month).

    Args:
        text: String containing a full month name followed by a 4-digit year,
              e.g. "January 2025" or "Data for February 2025".

    Returns:
        ISO-8601 date string like "2025-01-01", or None if no match found.
    """
    m = _MONTH_YEAR.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)} 1 {m.group(2)}", "%B %d %Y").date().isoformat()
    except ValueError:
        return None


def _is_seasonally_adjusted(table: Tag) -> bool:
    """Determine whether a table contains seasonally adjusted data.

    Checks the table caption first, then walks backwards through preceding
    sibling elements (headings, paragraphs) looking for a seasonal-adjustment
    label.  "Not seasonally adjusted" is tested before "seasonally adjusted"
    because the former is a substring of the latter.

    Args:
        table: BeautifulSoup Tag representing the <table> element.

    Returns:
        True if the table is labelled "Seasonally adjusted", False otherwise
        (including the default when no label is found).
    """
    caption = table.find("caption")
    if caption:
        text = caption.get_text().lower()
        if "not seasonally adjusted" in text:
            return False
        if "seasonally adjusted" in text:
            return True

    for sibling in table.previous_siblings:
        if not hasattr(sibling, "get_text"):
            continue
        text = sibling.get_text().lower()
        if "not seasonally adjusted" in text:
            return False
        if "seasonally adjusted" in text:
            return True

    return False


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse a G.19 HTML page into per-(series, date) consumer credit records.

    Locates all <table> elements, extracts date column headers formatted as
    "Month YYYY", and iterates data rows to produce one record per (series,
    date) pair.  Only tables with at least one parseable date column are
    processed.  Rows with empty series labels or unparseable amount cells
    are silently skipped.  Amounts containing thousands-separator commas
    (e.g. "5,082.5") are normalised before parsing.

    Args:
        html: Raw HTML string of the G.19 release page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: release_date, series_name,
        amount_billions_usd, seasonally_adjusted, source_url.

    Raises:
        ValueError: When no records could be extracted from the page,
                    which aborts any subsequent upload.
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
        if len(header_cells) < 2:
            continue

        dates = [_parse_month_year(c.get_text(strip=True)) for c in header_cells[1:]]
        if not any(d is not None for d in dates):
            continue

        seasonally_adjusted = _is_seasonally_adjusted(table)

        tbody = table.find("tbody")
        if tbody is None:
            continue

        for tr in tbody.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            series_name = cells[0].get_text(strip=True)
            if not series_name:
                continue

            for i, cell in enumerate(cells[1:]):
                if i >= len(dates) or dates[i] is None:
                    continue
                raw = cell.get_text(strip=True).replace(",", "")
                try:
                    amount = float(raw)
                except ValueError:
                    continue

                records.append(
                    {
                        "release_date": dates[i],
                        "series_name": series_name,
                        "amount_billions_usd": amount,
                        "seasonally_adjusted": seasonally_adjusted,
                        "source_url": source_url,
                    }
                )

    if not records:
        raise ValueError("No consumer credit records extracted from G.19 page")

    return records


def scrape() -> list[dict]:
    """Fetch the live G.19 page and return parsed consumer credit records.

    Delegates HTTP fetching to src.scrapers.http_client.fetch, which enforces
    a User-Agent header, a 2-5 s polite delay, and exponential backoff on
    429/5xx responses.  An additional 3-second sleep is applied as a courtesy
    to the Federal Reserve server.

    Returns:
        Same structure as run().

    Raises:
        ValueError: Propagated from run() when no records are extracted.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def _record_to_proto(record: dict) -> FedConsumerCreditRecord:
    msg = FedConsumerCreditRecord()
    msg.release_date = record["release_date"]
    msg.series_name = record["series_name"]
    msg.amount_billions_usd = record["amount_billions_usd"]
    msg.seasonally_adjusted = record["seasonally_adjusted"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape G.19 consumer credit data and upload records to BigQuery.

    Calls scrape(), converts each record to a FedConsumerCreditRecord proto,
    and uploads via upload_rows.  ValueError from scrape() propagates to
    abort the upload when zero records are extracted.

    Returns:
        Count of successfully inserted rows.

    Raises:
        ValueError: When scrape() extracts zero records.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("fed_consumer_credit", messages)


if __name__ == "__main__":
    main()
