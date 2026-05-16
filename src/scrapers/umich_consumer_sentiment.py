"""University of Michigan Survey of Consumers (SCA) sentiment scraper.

Fetches the monthly HTML table at SOURCE_URL and parses the three sub-indices:
Index of Consumer Sentiment (ICS), Index of Consumer Expectations (ICE), and
Index of Current Economic Conditions (ICC). One record is emitted per month
per reading (preliminary or final). Month strings like "May 2026 (P)" — where
the "(P)" suffix marks a preliminary reading — are normalised to "YYYY-MM".

All HTTP work is delegated to http_client.fetch, which enforces robots.txt
compliance and exponential backoff on 429/5xx. An explicit sleep of ≥3 s is
applied after each page fetch to satisfy polite-crawl requirements.
"""

import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from protos.umich_consumer_sentiment_pb2 import (  # type: ignore[attr-defined]
    FINAL,
    PRELIMINARY,
    UmichConsumerSentimentRecord,
)
from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch

SOURCE_URL = "http://www.sca.isr.umich.edu/"

_MONTH_FMT = "%B %Y"


def _parse_month(text: str) -> tuple[str, int]:
    """Parse a month cell text into a (survey_month, reading_type) pair.

    Strips the optional "(P)" preliminary marker, then parses the remaining
    month name and year using the format "%B %Y".

    Args:
        text: Raw text of a month cell, e.g. "May 2026 (P)" or "April 2026".

    Returns:
        A tuple of (survey_month, reading_type) where survey_month is a
        "YYYY-MM" string and reading_type is PRELIMINARY (0) or FINAL (1).

    Raises:
        ValueError: When the text cannot be parsed as a month after stripping
            the preliminary marker.
    """
    text = text.strip()
    is_preliminary = bool(re.search(r"\(P\)", text, re.IGNORECASE))
    clean = re.sub(r"\s*\(P\)\s*", "", text, flags=re.IGNORECASE).strip()
    dt = datetime.strptime(clean, _MONTH_FMT)
    return dt.strftime("%Y-%m"), PRELIMINARY if is_preliminary else FINAL


def _safe_float(cell) -> float | None:
    """Extract a float from a BeautifulSoup cell, returning None for missing values.

    Treats empty text, "--", "NA", and "N/A" as absent data. Commas are
    stripped before conversion.

    Args:
        cell: BeautifulSoup Tag for a table cell.

    Returns:
        Parsed float value, or None if the cell contains a missing-data marker.
    """
    text = cell.get_text(strip=True).replace(",", "")
    if not text or text in ("--", "NA", "N/A", "n/a"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _find_summary_table(soup: BeautifulSoup):
    """Locate the consumer-sentiment summary table in a parsed SCA page.

    Scans all <table> elements for one whose text contains both
    "consumer sentiment" and "consumer expectations", which uniquely
    identifies the ICS/ICE/ICC summary table on the SCA page.

    Args:
        soup: BeautifulSoup object for the full page.

    Returns:
        The matching BeautifulSoup Tag, or None if not found.
    """
    for table in soup.find_all("table"):
        text = table.get_text().lower()
        if "consumer sentiment" in text and "consumer expectations" in text:
            return table
    return None


def _find_column_indices(table) -> tuple[int, int, int]:
    """Determine the column positions of ICS, ICE, and ICC within the table.

    Iterates over header rows and matches cells by keyword. Falls back to
    positional defaults (1, 2, 3) when headers cannot be unambiguously matched.

    Args:
        table: BeautifulSoup Tag for the <table> element.

    Returns:
        A (ics_col, ice_col, icc_col) tuple of zero-based column indices.
    """
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        texts = [c.get_text(" ", strip=True).lower() for c in cells]
        ics_idx = ice_idx = icc_idx = None
        for i, t in enumerate(texts):
            if "sentiment" in t and ics_idx is None:
                ics_idx = i
            elif "expectation" in t and ice_idx is None:
                ice_idx = i
            elif "current" in t and "condition" in t and icc_idx is None:
                icc_idx = i
        if ics_idx is not None and ice_idx is not None and icc_idx is not None:
            return ics_idx, ice_idx, icc_idx
    return 1, 2, 3


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse the UMich SCA HTML page into per-month consumer-sentiment records.

    Locates the summary table, identifies column positions from the header,
    then iterates data rows. Rows whose first cell cannot be parsed as a
    month string are silently skipped (headers, footnotes, etc.). Rows where
    all three index cells contain missing-data markers are also skipped.

    Args:
        html: Raw HTML string of the SCA page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: survey_month, reading_type, index_value,
        expectations_index, current_conditions_index, source_url, fetch_time.

    Raises:
        ValueError: When html is empty or no consumer-sentiment table is found.
    """
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to UMich SCA parser")

    soup = BeautifulSoup(html, "lxml")
    table = _find_summary_table(soup)
    if table is None:
        raise ValueError("No consumer sentiment table found in SCA page")

    ics_col, ice_col, icc_col = _find_column_indices(table)
    max_col = max(ics_col, ice_col, icc_col)

    fetch_ts = datetime.now(timezone.utc).isoformat()
    records: list[dict] = []

    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) <= max_col:
            continue

        month_text = cells[0].get_text(strip=True)
        try:
            survey_month, reading_type = _parse_month(month_text)
        except ValueError:
            continue

        ics = _safe_float(cells[ics_col])
        ice = _safe_float(cells[ice_col])
        icc = _safe_float(cells[icc_col])

        if ics is None and ice is None and icc is None:
            continue

        records.append(
            {
                "survey_month": survey_month,
                "reading_type": reading_type,
                "index_value": ics or 0.0,
                "expectations_index": ice or 0.0,
                "current_conditions_index": icc or 0.0,
                "source_url": source_url,
                "fetch_time": fetch_ts,
            }
        )

    return records


def scrape() -> list[dict]:
    """Fetch the live SCA page and return parsed consumer-sentiment records.

    Delegates HTTP to http_client.fetch (robots.txt compliance, exponential
    backoff on 429/5xx). Sleeps ≥3 s after the fetch to satisfy the polite-
    crawl acceptance criterion, matching the pattern in tsa_checkpoint_travel.

    Returns:
        Same structure as run().

    Raises:
        ValueError: Propagated from run() when no table is found.
        RuntimeError: When robots.txt disallows SOURCE_URL.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def scrape_range(start_year: int, end_year: int) -> list[dict]:
    """Fetch records for a contiguous year range by filtering SOURCE_URL results.

    Fetches SOURCE_URL once (http_client.fetch handles the polite delay) and
    returns only records whose survey_month falls within [start_year, end_year].
    Suitable for lightweight backfill when the main page carries enough history;
    extend to iterate additional archive URLs for deeper historical coverage.

    Args:
        start_year: First year of the range (inclusive).
        end_year: Last year of the range (inclusive).

    Returns:
        Subset of run() records filtered to the requested year range.
    """
    resp = fetch(SOURCE_URL)
    records = run(resp.text, source_url=SOURCE_URL)
    return [r for r in records if start_year <= int(r["survey_month"][:4]) <= end_year]


def _record_to_proto(record: dict) -> UmichConsumerSentimentRecord:
    """Convert a parsed record dict to a UmichConsumerSentimentRecord stub.

    Args:
        record: Dict as returned by run().

    Returns:
        Populated UmichConsumerSentimentRecord dataclass with fetch_time set
        to the current UTC time in ISO-8601 format.
    """
    msg = UmichConsumerSentimentRecord()
    msg.survey_month = record["survey_month"]
    msg.reading_type = record["reading_type"]
    msg.index_value = record["index_value"]
    msg.expectations_index = record["expectations_index"]
    msg.current_conditions_index = record["current_conditions_index"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape UMich consumer-sentiment data and upload records to BigQuery.

    Calls scrape(), converts each record to a UmichConsumerSentimentRecord
    proto stub, and uploads via upload_rows with no dedup column. Omitting
    date_column (matching bea_gdp.py) is intentional: preliminary and final
    readings for the same survey_month must both be persisted, and the shared
    uploader cannot express a composite (survey_month + reading_type) key.

    Returns:
        Count of successfully inserted rows.

    Raises:
        ValueError: When scrape() finds no records.
        KeyError: When BQ_PROJECT or BQ_DATASET environment variables are unset.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("umich_consumer_sentiment", messages)


if __name__ == "__main__":
    main()
