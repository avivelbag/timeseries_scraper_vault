"""CFTC Commitments of Traders (COT) Legacy Futures-Only scraper.

Fetches the weekly Legacy Futures-Only COT report for the CME Group from
the CFTC website and parses the fixed-width pre-formatted HTML page into
structured CotRecord messages.

The CFTC page embeds the COT data in a <pre> block.  Each commodity occupies
two lines: a header line carrying the commodity name and a six-digit contract
market code, followed by an "ALL" data line with nine whitespace-separated
numbers (non-comm long/short/spreads, comm long/short, total long/short,
non-rep long/short).  Spreads are at index 2 and are intentionally skipped.

Historical backfill is supported via scrape_year() and scrape_range().
"""

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.cftc_cot_pb2 import CotRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.cftc.gov/dea/futures/deacmesf.htm"

HISTORICAL_URL_TEMPLATE = "https://www.cftc.gov/dea/futures/deacmesf{year}.htm"

# Matches a commodity header line: starts with an uppercase letter, ends with
# a 6-digit CFTC contract market code, padded by 3+ spaces before the code.
_COMMODITY_RE = re.compile(r"^([A-Z][A-Z0-9 ,.()/&'-]+?)\s{3,}(\d{6})\s*$")

# Matches the "ALL" data line that follows each commodity header.
_ALL_LINE_RE = re.compile(r"^\s+ALL\s")

# Matches "As of <Weekday>, <Month> <Day>, <Year>" in page headings.
_DATE_RE = re.compile(r"As of \w+,\s+(\w+ \d+,\s*\d{4})", re.IGNORECASE)


def _extract_report_date(soup: BeautifulSoup) -> str:
    """Extract the report date from the page <h2> tags or <title>.

    Searches <h2> tags first (the CFTC page typically places the date in an
    <h2>), then falls back to the <title> element.  Expects a phrase like
    "As of Tuesday, January 14, 2025" and returns the date in YYYY-MM-DD
    format.

    Args:
        soup: Parsed BeautifulSoup tree of the CFTC COT page.

    Returns:
        ISO date string YYYY-MM-DD, or empty string if the pattern is absent.
    """
    candidates = [tag.get_text(" ", strip=True) for tag in soup.find_all("h2")]
    title_tag = soup.find("title")
    if title_tag:
        candidates.append(title_tag.get_text(" ", strip=True))

    for text in candidates:
        m = _DATE_RE.search(text)
        if not m:
            continue
        date_str = re.sub(r"\s+", " ", m.group(1)).strip()
        try:
            return datetime.strptime(date_str, "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _find_pre_with_data(soup: BeautifulSoup) -> Tag | None:
    """Return the first <pre> block containing a commodity header line.

    Some CFTC report pages include a separate introductory <pre> block (e.g.
    a column legend) before the actual data block.  Scanning all blocks and
    selecting the first one that matches _COMMODITY_RE avoids silently
    returning an empty parse for pages with such a preamble.

    Args:
        soup: Parsed BeautifulSoup tree of the CFTC COT page.

    Returns:
        The first Tag whose text matches _COMMODITY_RE, or None if no such
        block exists.
    """
    for pre in soup.find_all("pre"):
        if any(_COMMODITY_RE.match(line) for line in pre.get_text().splitlines()):
            return pre
    return None


def parse_html(html: str, source_url: str = SOURCE_URL) -> list[CotRecord]:
    """Parse the CFTC COT HTML page into a list of CotRecord messages.

    Finds the <pre> block in the page, scans line by line, and pairs each
    commodity header line (NAME padded to a 6-digit code) with the subsequent
    "ALL" data line that holds 9 comma-formatted integers.

    Column mapping within the "ALL" line (0-indexed, commas stripped):
      0  -> noncommercial_long
      1  -> noncommercial_short
      2  -> spreads (intentionally skipped)
      3  -> commercial_long
      4  -> commercial_short
      5  -> total_reportable_long
      6  -> total_reportable_short
      7  -> nonreportable_long
      8  -> nonreportable_short

    Lines with fewer than 9 numbers are silently skipped.  A pending commodity
    header is cleared when a new commodity header is found, ensuring mismatched
    pairs do not produce corrupt records.

    Args:
        html: Raw HTML string from the CFTC COT page.
        source_url: URL to embed in each CotRecord's source_url field.
            Defaults to SOURCE_URL; pass a year-specific URL for backfill.

    Returns:
        List of CotRecord instances, one per successfully parsed commodity row.
        Returns an empty list when no <pre> block with commodity data is present.
    """
    soup = BeautifulSoup(html, "lxml")
    report_date = _extract_report_date(soup)
    fetch_time = datetime.now(timezone.utc).isoformat()

    pre = _find_pre_with_data(soup)
    if not pre:
        return []

    lines = pre.get_text().splitlines()
    records: list[CotRecord] = []
    pending_name: str | None = None
    pending_code: str | None = None

    for line in lines:
        commodity_match = _COMMODITY_RE.match(line)
        if commodity_match:
            pending_name = commodity_match.group(1).strip()
            pending_code = commodity_match.group(2).strip()
            continue

        if _ALL_LINE_RE.match(line) and pending_name is not None:
            nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", line)]
            if len(nums) >= 9:
                records.append(
                    CotRecord(
                        report_date=report_date,
                        commodity_name=pending_name,
                        cftc_contract_market_code=pending_code or "",
                        noncommercial_long=nums[0],
                        noncommercial_short=nums[1],
                        # nums[2] is non-commercial spreads; excluded from the schema
                        commercial_long=nums[3],
                        commercial_short=nums[4],
                        total_reportable_long=nums[5],
                        total_reportable_short=nums[6],
                        nonreportable_long=nums[7],
                        nonreportable_short=nums[8],
                        source_url=source_url,
                        fetch_time=fetch_time,
                    )
                )
            pending_name = None
            pending_code = None

    return records


def scrape() -> list[CotRecord]:
    """Fetch the current CFTC COT page and return parsed records.

    Delegates to src.http_client.fetch, which checks robots.txt, sleeps 2–5 s
    before the first request (satisfying the ≥3 s average polite delay), and
    retries with exponential backoff on 429/5xx responses.

    Returns:
        List of CotRecord instances for the current reporting week.
    """
    resp = fetch(SOURCE_URL)
    return parse_html(resp.text)


def scrape_year(year: int) -> list[CotRecord]:
    """Fetch the CFTC COT page for a specific historical reporting year.

    Constructs the year-specific URL from HISTORICAL_URL_TEMPLATE, fetches the
    page via http_client.fetch (which enforces robots.txt, polite delay, and
    exponential backoff), then parses the response.  The year-specific URL is
    embedded in each returned CotRecord's source_url field.

    Args:
        year: Four-digit calendar year, e.g. 2020.  The CFTC typically
            publishes historical pages from 1986 onward.

    Returns:
        List of CotRecord instances for all commodities in that year's report.
    """
    url = HISTORICAL_URL_TEMPLATE.format(year=year)
    resp = fetch(url)
    return parse_html(resp.text, source_url=url)


def scrape_range(start_year: int, end_year: int) -> list[CotRecord]:
    """Fetch CFTC COT data for a contiguous range of historical years.

    Calls scrape_year for each year in [start_year, end_year] inclusive.
    Years whose fetch or parse raises any exception are skipped so that a
    single unavailable year does not abort a multi-year backfill.

    Args:
        start_year: First year to fetch, inclusive.
        end_year: Last year to fetch, inclusive.

    Returns:
        Concatenated list of CotRecord instances from all successful years.
    """
    all_records: list[CotRecord] = []
    for year in range(start_year, end_year + 1):
        try:
            all_records.extend(scrape_year(year))
        except Exception:
            pass
    return all_records


def main() -> int:
    """Scrape CFTC COT data and upload records to BigQuery.

    Returns:
        Count of rows successfully inserted.
    """
    records = scrape()
    return upload_rows("cftc_cot", records, date_column="report_date")


if __name__ == "__main__":
    main()
