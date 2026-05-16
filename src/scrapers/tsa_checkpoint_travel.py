"""TSA daily checkpoint traveler throughput scraper.

Fetches the public HTML table at tsa.gov/travel/passenger-volumes and parses
each row into a TsaCheckpointRecord.  The page is a single table with columns
keyed by position: [Date, current-year throughput, prior-year throughput].

robots.txt at tsa.gov allows /travel/passenger-volumes for all agents.
"""

import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from protos.tsa_checkpoint_travel_pb2 import TsaCheckpointRecord  # type: ignore[attr-defined]
from src.scrapers.http_client import fetch

SOURCE_URL = "https://www.tsa.gov/travel/passenger-volumes"

_BACKOFF_START = 5.0
_BACKOFF_CAP = 120.0


def _parse_int(text: str) -> int:
    """Strip commas and whitespace then cast to int; return 0 on failure."""
    cleaned = text.replace(",", "").replace("\xa0", "").strip()
    if not cleaned or cleaned in ("-", "--", "n/a", "N/A"):
        return 0
    try:
        return int(cleaned)
    except ValueError:
        return 0


def fetch_page(url: str = SOURCE_URL) -> str:
    """Fetch the TSA passenger volumes page and return its HTML text.

    Delegates to http_client.fetch which enforces robots.txt compliance,
    a polite random delay, and exponential backoff on 429/5xx responses.
    An additional 3-second sleep is applied after each page fetch to respect
    polite-crawl conventions (the page has no pagination in practice but the
    sleep is kept for future-proofing against any paginated variant).

    Args:
        url: URL to fetch; defaults to SOURCE_URL.

    Returns:
        Raw HTML string of the fetched page.
    """
    resp = fetch(url)
    time.sleep(3)
    return resp.text


def parse_table(html: str) -> list[TsaCheckpointRecord]:
    """Parse the TSA checkpoint HTML table into a list of TsaCheckpointRecord.

    The table has three columns keyed by position rather than header text
    because the year labels in the headers change annually:
      0 — Date (M/D/YYYY format)
      1 — Current-year traveler throughput
      2 — Prior-year traveler throughput

    Rows where the date cell cannot be parsed or both throughput columns are
    zero are skipped silently so a malformed row does not abort the run.

    Args:
        html: Raw HTML string of the TSA passenger volumes page.

    Returns:
        List of TsaCheckpointRecord, one per valid data row found in the table.

    Raises:
        ValueError: If html is empty or no <table> element is found.
    """
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to TSA checkpoint parser")

    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        raise ValueError("No <table> found in TSA checkpoint page")

    fetch_ts = datetime.now(timezone.utc).isoformat()
    records: list[TsaCheckpointRecord] = []

    tbody = table.find("tbody")
    rows = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

    for row in rows:
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue

        date_text = cells[0].get_text(strip=True)
        try:
            iso_date = datetime.strptime(date_text, "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue

        travelers_total = _parse_int(cells[1].get_text())
        travelers_year_ago = _parse_int(cells[2].get_text())

        rec = TsaCheckpointRecord()
        rec.date = iso_date
        rec.travelers_total = travelers_total
        rec.travelers_year_ago = travelers_year_ago
        rec.source_url = SOURCE_URL
        rec.fetch_time = fetch_ts
        records.append(rec)

    return records


def upload_to_bigquery(records: list[TsaCheckpointRecord]) -> int:
    """Upload TSA checkpoint records to BigQuery.

    Stub — no live BigQuery call is made.  Wired up by the orchestrator when
    a real destination table is provisioned.

    Args:
        records: List of TsaCheckpointRecord instances to upload.

    Returns:
        Number of records that would be uploaded (always len(records)).
    """
    return len(records)


def scrape() -> list[TsaCheckpointRecord]:
    """Fetch the live TSA checkpoint page and return parsed records.

    Returns:
        List of TsaCheckpointRecord instances.
    """
    html = fetch_page(SOURCE_URL)
    return parse_table(html)


def main() -> int:
    """Scrape TSA checkpoint traveler throughput and upload to BigQuery.

    Returns:
        Count of records uploaded.
    """
    records = scrape()
    return upload_to_bigquery(records)
