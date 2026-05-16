"""Federal Reserve H.8 Assets and Liabilities of Commercial Banks scraper.

Fetches the H.8 release page from federalreserve.gov and parses the summary
HTML tables into per-series records covering the most-recent week's balance-
sheet aggregates for U.S. commercial banks.  One record is emitted per target
series per table (SA and NSA tables processed separately).

Target series: Total loans and leases, Commercial and industrial loans,
Real estate loans, Consumer loans, Total deposits, Securities.
"""

import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.fed_h8_bank_assets_pb2 import FedH8BankAssets  # type: ignore[attr-defined]

SOURCE_URL = "https://www.federalreserve.gov/releases/h8/current/"

_TARGET_SERIES = [
    "Total loans and leases",
    "Commercial and industrial loans",
    "Real estate loans",
    "Consumer loans",
    "Total deposits",
    "Securities",
]

_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\.?\s*(\d{1,2}),\s*(\d{4})",
    re.IGNORECASE,
)


def _parse_column_date(text: str) -> str | None:
    """Parse an H.8 column-header date string into ISO-8601 YYYY-MM-DD.

    Handles abbreviated months with or without a trailing period
    (e.g. "Apr. 24, 2025", "Apr 24, 2025", "April 24, 2025") and collapses
    internal whitespace before matching.

    Args:
        text: Raw text from a table column header cell.

    Returns:
        ISO-8601 date string "YYYY-MM-DD", or None when no date is found.
    """
    normalized = re.sub(r"\s+", " ", text).strip()
    m = _DATE_RE.search(normalized)
    if not m:
        return None
    month_str = m.group(1).rstrip(".")
    day_str = m.group(2)
    year_str = m.group(3)
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(f"{month_str} {day_str} {year_str}", fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _seasonal_adjustment_label(table: Tag) -> str:
    """Return 'SA' or 'NSA' based on the table caption or preceding heading.

    Checks the table's <caption> element first, then walks backwards through
    preceding sibling elements.  'Not seasonally adjusted' is tested before
    'seasonally adjusted' because the former is a substring of the latter.
    Defaults to 'SA' when no label is found.

    Args:
        table: BeautifulSoup Tag representing the <table> element.

    Returns:
        "SA" if the table is seasonally adjusted, "NSA" otherwise.
    """
    caption = table.find("caption")
    if caption:
        text = caption.get_text().lower()
        if "not seasonally adjusted" in text:
            return "NSA"
        if "seasonally adjusted" in text:
            return "SA"

    for sibling in table.previous_siblings:
        if not hasattr(sibling, "get_text"):
            continue
        text = sibling.get_text().lower()
        if "not seasonally adjusted" in text:
            return "NSA"
        if "seasonally adjusted" in text:
            return "SA"

    return "SA"


def _matches_target(row_label: str) -> str | None:
    """Return the matching target series name if row_label starts with it.

    Normalises both strings to lowercase and collapsed whitespace, then checks
    whether the row label begins with any target series name.  The prefix check
    prevents 'Securities' from matching rows like 'Other securities'.

    Args:
        row_label: Text from the first cell of a data row.

    Returns:
        The canonical target series name string, or None if no match.
    """
    norm_label = re.sub(r"\s+", " ", row_label).strip().lower()
    for target in _TARGET_SERIES:
        norm_target = target.lower()
        if norm_label == norm_target or norm_label.startswith(norm_target):
            return target
    return None


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse an H.8 HTML page into per-(series, table) bank asset records.

    For each table that contains parseable date column headers, this function:
    - determines the seasonal-adjustment label ('SA' or 'NSA');
    - takes the rightmost date column as the most-recent week-ending date;
    - scans every data row for labels that prefix-match a target series;
    - emits one record per matched series.

    Rows with asterisks, blank labels, or non-numeric values are silently
    skipped.  A ValueError is raised when no records are extracted.

    Args:
        html: Raw HTML string of the H.8 release page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: week_ending, series_label, value_millions_usd,
        seasonal_adjustment, source_url, units.

    Raises:
        ValueError: When no records could be extracted from the page.
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

        date_entries = [
            (i, _parse_column_date(c.get_text(strip=True)))
            for i, c in enumerate(header_cells[1:], start=1)
        ]
        valid_dates = [(i, d) for i, d in date_entries if d is not None]
        if not valid_dates:
            continue

        # Rightmost valid date column is the most recent week.
        latest_col_idx, latest_date = valid_dates[-1]
        seasonal_adj = _seasonal_adjustment_label(table)

        tbody = table.find("tbody")
        if tbody is None:
            continue

        for tr in tbody.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue

            row_label = cells[0].get_text(strip=True)
            matched_target = _matches_target(row_label)
            if matched_target is None:
                continue

            if latest_col_idx >= len(cells):
                continue

            raw = cells[latest_col_idx].get_text(strip=True).replace(",", "")
            try:
                value = float(raw)
            except ValueError:
                continue

            records.append(
                {
                    "week_ending": latest_date,
                    "series_label": matched_target,
                    "value_millions_usd": value,
                    "seasonal_adjustment": seasonal_adj,
                    "source_url": source_url,
                    "units": "millions_usd",
                }
            )

    if not records:
        raise ValueError("No H.8 bank asset records extracted from page")

    return records


def scrape() -> list[dict]:
    """Fetch the live H.8 page and return parsed bank asset records.

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


def _record_to_proto(record: dict) -> FedH8BankAssets:
    msg = FedH8BankAssets()
    msg.week_ending = record["week_ending"]
    msg.series_label = record["series_label"]
    msg.value_millions_usd = record["value_millions_usd"]
    msg.seasonal_adjustment = record["seasonal_adjustment"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    msg.units = record["units"]
    return msg


def main() -> int:
    """Scrape H.8 bank asset data and upload records to BigQuery.

    Calls scrape(), converts each record to a FedH8BankAssets proto, and
    uploads via upload_rows to the 'fed_h8_bank_assets' table.

    Returns:
        Count of successfully inserted rows.

    Raises:
        ValueError: When scrape() extracts zero records.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("fed_h8_bank_assets", messages)


if __name__ == "__main__":
    main()
