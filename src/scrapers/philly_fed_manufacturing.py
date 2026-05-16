"""Philadelphia Fed Business Outlook Survey (Manufacturing) monthly HTML scraper.

Fetches the Philly Fed Manufacturing Business Outlook Survey results page,
which publishes a diffusion-index table where rows are sub-indices (General
Activity, New Orders, Shipments, etc.) and columns are Current Month, Prior
Month, and Six-Month Forecast.

Unlike the ISM and Empire State scrapers, this produces one record per
indicator per survey month rather than one record per month.  The indicator
name becomes a first-class field so all rows can be stored in a single flat
BigQuery table without wide sparse columns.

The report date is extracted from the page heading, which follows the pattern
"May 2026 Manufacturing Business Outlook Survey".  Footnote markers (asterisks,
commas used as thousands separators) are stripped before float conversion.
"""

import re
import time
from datetime import datetime, timezone
from typing import Optional

from bs4 import BeautifulSoup, Tag

from protos.philly_fed_manufacturing_pb2 import PhillyFedManufacturingRecord  # type: ignore[attr-defined]
from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch

SOURCE_URL = (
    "https://www.philadelphiafed.org/surveys-and-data/regional-economic-analysis/"
    "manufacturing-business-outlook-survey"
)

_NA_VALUES = frozenset({"N/A", "n/a", "NA", "na", "", "--", "-", ".", "n.a."})

# Strips footnote markers and thousands-separator commas before float parsing.
_CLEAN_RE = re.compile(r"[*†‡§¶#,]")

# Matches headings like "May 2026 Manufacturing Business Outlook Survey".
_HEADING_RE = re.compile(
    r"([A-Za-z]+ \d{4})\s+Manufacturing\s+Business\s+Outlook\s+Survey",
    re.IGNORECASE,
)


def _parse_float(text: str) -> Optional[float]:
    """Convert a cell string to float, stripping footnote markers first.

    Handles negative diffusion-index values (common in downturns) and strips
    commas used as thousands separators in large values.  Returns None for
    N/A spellings or unparseable text.

    Args:
        text: Raw cell text (may include surrounding whitespace or footnote markers).

    Returns:
        Parsed float, or None if the text signals a missing value.
    """
    clean = _CLEAN_RE.sub("", text).strip()
    if clean in _NA_VALUES:
        return None
    try:
        return float(clean)
    except ValueError:
        return None


def _parse_report_date(soup: BeautifulSoup) -> str:
    """Extract the survey month from a page heading and return a YYYY-MM-01 date.

    Scans all heading tags (h1–h4) for the pattern
    "<Month> <Year> Manufacturing Business Outlook Survey".  The first match
    wins and is parsed via strptime.

    Args:
        soup: Parsed BeautifulSoup document.

    Returns:
        ISO date string "YYYY-MM-01".

    Raises:
        ValueError: When no heading matches the expected pattern.
    """
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        text = tag.get_text(strip=True)
        match = _HEADING_RE.search(text)
        if match:
            month_str = match.group(1)
            for fmt in ("%B %Y", "%b %Y"):
                try:
                    return datetime.strptime(month_str, fmt).strftime("%Y-%m-01")
                except ValueError:
                    continue
    raise ValueError("Could not parse report date from page headings")


def run(html: str, source_url: str = SOURCE_URL) -> list[PhillyFedManufacturingRecord]:
    """Parse the Philly Fed Manufacturing BOS diffusion-index table from page HTML.

    Locates the first <table> on the page and iterates its <tr> rows.  Rows
    that contain any <th> element are treated as header rows and skipped.
    Rows with fewer than four <td> cells (indicator + three value columns) are
    also skipped.  Rows whose current_index cell does not parse to a finite
    float are skipped.

    The report date is taken from the first heading that matches
    "Month Year Manufacturing Business Outlook Survey".

    Args:
        html: Raw HTML of the Philly Fed BOS current-results page.
        source_url: Stored verbatim in each record's source_url field.

    Returns:
        List of PhillyFedManufacturingRecord instances, one per indicator row,
        in the order they appear in the table.

    Raises:
        ValueError: When html is empty, no table is found, the report date
            cannot be parsed, or no indicator rows are extracted.
    """
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to Philly Fed Manufacturing parser")

    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table")
    if not table:
        raise ValueError("No table found in Philly Fed Manufacturing page")

    report_date = _parse_report_date(soup)
    fetch_ts = datetime.now(timezone.utc).isoformat()

    records: list[PhillyFedManufacturingRecord] = []

    for row in table.find_all("tr"):
        if row.find("th"):
            continue

        cells: list[Tag] = row.find_all("td")
        if len(cells) < 4:
            continue

        indicator_name = cells[0].get_text(strip=True)
        if not indicator_name:
            continue

        current_index = _parse_float(cells[1].get_text(strip=True))
        if current_index is None:
            continue

        prior_month_index = _parse_float(cells[2].get_text(strip=True))
        six_month_forecast = _parse_float(cells[3].get_text(strip=True))

        records.append(
            PhillyFedManufacturingRecord(
                report_date=report_date,
                indicator_name=indicator_name,
                current_index=current_index,
                prior_month_index=prior_month_index,
                six_month_forecast=six_month_forecast,
                source_url=source_url,
                fetch_time=fetch_ts,
            )
        )

    if not records:
        raise ValueError("No indicator rows extracted from Philly Fed Manufacturing table")

    return records


def scrape() -> list[PhillyFedManufacturingRecord]:
    """Fetch the Philly Fed Manufacturing BOS page and parse its diffusion-index table.

    Delegates HTTP retrieval to ``http_client.fetch``, which enforces
    robots.txt compliance, applies a 2–5 s polite delay before the request,
    and retries 429/5xx responses with exponential backoff (base 2 s, cap
    120 s, up to 5 attempts).  An additional 3 s sleep after the fetch
    maintains courteous crawl behaviour consistent with the 3 s minimum
    specified in the acceptance criteria.

    Returns:
        List of PhillyFedManufacturingRecord instances.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def main() -> int:
    """Scrape Philly Fed Manufacturing data and upload to BigQuery.

    Uploads records to the ``philly_fed_manufacturing`` table, deduplicating
    by report_date so re-runs for the same survey month are idempotent.

    Returns:
        Count of rows successfully inserted.
    """
    records = scrape()
    return upload_rows("philly_fed_manufacturing", records, date_column="report_date")
