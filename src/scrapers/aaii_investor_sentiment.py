"""AAII Investor Sentiment Survey weekly HTML scraper.

Fetches the AAII Sentiment Survey historical results page and parses its
Bull/Bear/Neutral table going back to 1987. Each row maps to one weekly
survey reading.

The page has a single primary HTML table with columns:
  Date | Bullish | Neutral | Bearish | Total | Bull-Bear Spread |
  8-week Mov Avg | Bull Average | Bear Average

Percentage suffixes are stripped before float conversion. Rows where
bullish + neutral + bearish does not sum within 1% of 100 are discarded.
"""

import math
import time
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup

from protos.aaii_investor_sentiment_pb2 import AaiiInvestorSentimentRecord  # type: ignore[attr-defined]
from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch

SOURCE_URL = "https://www.aaii.com/sentimentsurvey/sent_results"

_NA_VALUES = frozenset({"N/A", "n/a", "NA", "na", "", "--", "-", ".", "n.a."})


def _parse_pct(text: str) -> Optional[float]:
    """Convert a percentage cell value to a plain float.

    Strips trailing '%' and surrounding whitespace before conversion.
    Returns None for N/A spellings or values that cannot be parsed.

    Args:
        text: Raw cell text, possibly containing '%', whitespace, or N/A markers.

    Returns:
        Float (nominally 0–100 for percentage columns), or None if unparseable.
    """
    clean = text.strip().rstrip("%").strip()
    if clean in _NA_VALUES:
        return None
    try:
        return float(clean)
    except ValueError:
        return None


def _parse_date(text: str) -> str:
    """Convert an AAII date string to ISO YYYY-MM-DD format.

    Tries M/D/YY and M/D/YYYY formats in order. Two-digit years are
    interpreted by Python's strptime (00–68 → 2000–2068, 69–99 → 1969–1999).

    Args:
        text: Raw date string from the AAII table (e.g. '5/15/25').

    Returns:
        ISO date string 'YYYY-MM-DD'.

    Raises:
        ValueError: When text cannot be parsed under any supported format.
    """
    clean = text.strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(clean, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Cannot parse AAII date: {text!r}")


def run(html: str, source_url: str = SOURCE_URL) -> list[AaiiInvestorSentimentRecord]:
    """Parse the AAII Sentiment Survey results table from page HTML.

    Locates the first <table> element and iterates its <tr> rows. Header rows
    (any row containing a <th> element) are skipped. Rows with fewer than 9
    cells are skipped. Rows where bullish + neutral + bearish does not sum
    within 1% of 100 are discarded. Rows whose date field cannot be parsed are
    skipped individually without raising.

    Column indices used:
      0: Date, 1: Bullish%, 2: Neutral%, 3: Bearish%, 4: Total% (ignored),
      5: Bull-Bear Spread%, 6: 8-week Mov Avg% (ignored),
      7: Bull Average%, 8: Bear Average%

    Args:
        html: Raw HTML of the AAII sentiment results page.
        source_url: Stored verbatim in each record's source_url field.

    Returns:
        List of AaiiInvestorSentimentRecord instances in table order.

    Raises:
        ValueError: When html is empty, no table is found, or no valid rows
            can be extracted.
    """
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to AAII sentiment parser")

    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        raise ValueError("No table found in AAII sentiment page")

    fetch_ts = datetime.now(timezone.utc).isoformat()
    records: list[AaiiInvestorSentimentRecord] = []

    for row in table.find_all("tr"):
        if row.find("th"):
            continue

        cells = row.find_all("td")
        if len(cells) < 9:
            continue

        date_text = cells[0].get_text(strip=True)
        try:
            date_str = _parse_date(date_text)
        except ValueError:
            continue

        bullish = _parse_pct(cells[1].get_text(strip=True))
        neutral = _parse_pct(cells[2].get_text(strip=True))
        bearish = _parse_pct(cells[3].get_text(strip=True))

        if bullish is None or neutral is None or bearish is None:
            continue
        if not math.isclose(bullish + neutral + bearish, 100.0, abs_tol=1.0):
            continue

        bull_bear_spread = _parse_pct(cells[5].get_text(strip=True))
        bullish_average = _parse_pct(cells[7].get_text(strip=True))
        bearish_average = _parse_pct(cells[8].get_text(strip=True))

        records.append(
            AaiiInvestorSentimentRecord(
                date=date_str,
                bullish_pct=bullish,
                neutral_pct=neutral,
                bearish_pct=bearish,
                bull_bear_spread=bull_bear_spread if bull_bear_spread is not None else 0.0,
                bullish_average=bullish_average if bullish_average is not None else 0.0,
                bearish_average=bearish_average if bearish_average is not None else 0.0,
                source_url=source_url,
                fetch_time=fetch_ts,
            )
        )

    if not records:
        raise ValueError("No valid rows extracted from AAII sentiment table")

    return records


def scrape() -> list[AaiiInvestorSentimentRecord]:
    """Fetch the AAII sentiment results page and parse its historical table.

    Delegates HTTP retrieval to http_client.fetch, which enforces robots.txt
    compliance, applies a 2–5 s polite delay before the request, and retries
    429/5xx responses with exponential backoff (base 2 s, cap 120 s, up to 5
    attempts). An additional 2 s sleep after the response ensures the minimum
    inter-request gap required by the acceptance criteria.

    Returns:
        List of AaiiInvestorSentimentRecord instances.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(2)
    return run(resp.text, source_url=SOURCE_URL)


def main() -> int:
    """Scrape AAII sentiment data and upload to BigQuery.

    Uploads to the aaii_investor_sentiment table, deduplicating by date so
    re-runs for dates already in the table are idempotent.

    Returns:
        Count of rows successfully inserted.
    """
    records = scrape()
    return upload_rows("aaii_investor_sentiment", records, date_column="date")
