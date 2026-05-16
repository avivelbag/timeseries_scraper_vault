"""MBA Weekly Mortgage Applications Survey scraper.

Fetches the most-recent press release from the MBA landing page at SOURCE_URL,
parses the embedded HTML table for mortgage application index values and
percentage changes, and maps each row to a MortgageApplicationsRecord proto.

All HTTP work is delegated to http_client.fetch, which enforces robots.txt
compliance, a polite 2–5 s delay, and exponential backoff on 429/5xx
responses. An explicit ≥3 s sleep is added after each page fetch.
"""

import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from protos.mba_mortgage_applications_pb2 import MortgageApplicationsRecord  # type: ignore[attr-defined]
from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch

SOURCE_URL = (
    "https://www.mba.org/news-research-and-resources/research-and-economics"
    "/single-family-research/weekly-applications-survey"
)
BASE_URL = "https://www.mba.org"

_WEEK_ENDING_PATTERNS = [
    re.compile(r"week ending\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", re.IGNORECASE),
    re.compile(r"for the week of\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", re.IGNORECASE),
]

_INDEX_NAME_MAP = {
    "market composite": "Market_Composite",
    "market composite index": "Market_Composite",
    "purchase": "Purchase",
    "purchase index": "Purchase",
    "refinance": "Refinance",
    "refinance index": "Refinance",
    "government": "Government",
    "conventional": "Conventional",
    "arm": "ARM",
    "adjustable rate mortgage": "ARM",
}

_NULL_SENTINELS = frozenset({"", "--", "n.a.", "na", "n/a"})

_SKIP_ROW_LABELS = frozenset({"survey component", "index", "component"})


def _parse_pct(text: str) -> float | None:
    """Parse a percentage cell, returning None for missing-data markers.

    Strips the '%' suffix and surrounding whitespace before float conversion.
    Treats empty strings, "--", "n.a.", "na", and "n/a" (case-insensitive) as
    absent data.

    Args:
        text: Raw cell text, e.g. "-2.3%", "5.1%", "n.a.", "--", "".

    Returns:
        Float percentage value (sign preserved, '%' stripped), or None when
        the cell contains a missing-data marker or cannot be parsed.
    """
    cleaned = text.strip().rstrip("%")
    if cleaned.lower() in _NULL_SENTINELS:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _normalize_index_name(raw: str) -> tuple[str, bool]:
    """Normalise an MBA index row label to a canonical name and SA/NSA flag.

    Strips trailing colons and whitespace, detects the "(SA)" or "(NSA)"
    adjuster marker, removes the marker and the trailing word "Index", then
    maps the result to a canonical underscore-separated identifier. Unknown
    names are returned with spaces replaced by underscores.

    Args:
        raw: Raw row label, e.g. "Market Composite Index (SA):".

    Returns:
        A (index_name, seasonally_adjusted) tuple. index_name is one of
        "Market_Composite", "Purchase", "Refinance", "Government",
        "Conventional", "ARM", or a best-effort normalisation of the raw text.
    """
    text = raw.strip().rstrip(":").strip()
    sa_match = re.search(r"\((SA|NSA)\)", text, re.IGNORECASE)
    is_sa = bool(sa_match and sa_match.group(1).upper() == "SA")

    clean = re.sub(r"\s*\((SA|NSA)\)\s*", "", text, flags=re.IGNORECASE)
    clean = re.sub(r"\s+Index\s*$", "", clean, flags=re.IGNORECASE).strip()

    return _INDEX_NAME_MAP.get(clean.lower(), clean.replace(" ", "_")), is_sa


def _parse_week_ending_date(html: str) -> str | None:
    """Extract the week-ending date from a press-release page.

    Scans the page <title> and all <h1>/<h2> elements for patterns like
    "Week Ending January 12, 2024". Returns the date as "YYYY-MM-DD", or
    None when no recognisable date string is found.

    Args:
        html: Raw HTML of the release page.

    Returns:
        ISO-8601 date string (YYYY-MM-DD), or None if not found.
    """
    soup = BeautifulSoup(html, "lxml")
    candidates: list[str] = []
    if soup.title:
        candidates.append(soup.title.get_text())
    for tag in soup.find_all(["h1", "h2"]):
        candidates.append(tag.get_text())

    for text in candidates:
        for pattern in _WEEK_ENDING_PATTERNS:
            m = pattern.search(text)
            if m:
                try:
                    dt = datetime.strptime(m.group(1).strip(), "%B %d, %Y")
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    continue
    return None


def _find_data_table(soup: BeautifulSoup):
    """Locate the mortgage-applications data table in a parsed release page.

    Scans all <table> elements for one whose full text contains both a known
    index-family keyword ("composite", "purchase", or "refinance") and the
    word "index", which uniquely identifies the WAS data table among any
    navigation or footnote tables.

    Args:
        soup: BeautifulSoup object for the full release page.

    Returns:
        The matching BeautifulSoup Tag, or None if not found.
    """
    for table in soup.find_all("table"):
        text = table.get_text().lower()
        has_index_family = "composite" in text or "purchase" in text or "refinance" in text
        if has_index_family and "index" in text:
            return table
    return None


def _find_column_indices(table) -> tuple[int, int, int]:
    """Determine the column positions of index value, pct-week, and pct-year.

    Iterates header rows looking for column labels that unambiguously match:
    - value column: header containing "index" (without "%" or "change") or
      "this week"
    - pct-week column: header containing "change", "previous", or "last week"
      but NOT "year"
    - pct-year column: header containing "year" together with "ago" or
      "change"

    Falls back to positional defaults (1, 2, 3) when the header cannot be
    matched.

    Args:
        table: BeautifulSoup Tag for the <table> element.

    Returns:
        A (value_col, pct_week_col, pct_year_col) tuple of 0-based column
        indices.
    """
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        texts = [c.get_text(" ", strip=True).lower() for c in cells]

        val_col = pct_week_col = pct_year_col = None
        for i, t in enumerate(texts):
            if i == 0:
                continue
            if val_col is None and (
                "this week" in t
                or ("index" in t and "%" not in t and "change" not in t)
            ):
                val_col = i
            elif pct_week_col is None and (
                "change" in t or "previous" in t or "last week" in t
            ) and "year" not in t:
                pct_week_col = i
            elif pct_year_col is None and "year" in t and (
                "ago" in t or "change" in t
            ):
                pct_year_col = i

        if val_col is not None or pct_week_col is not None:
            return (
                val_col if val_col is not None else 1,
                pct_week_col if pct_week_col is not None else 2,
                pct_year_col if pct_year_col is not None else 3,
            )

    return 1, 2, 3


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse a MBA WAS release page into per-index mortgage application records.

    Locates the data table with _find_data_table, identifies column positions
    from the header with _find_column_indices, extracts the week-ending date
    with _parse_week_ending_date, then iterates data rows. Header rows whose
    first cell matches a known skip label are skipped. Rows whose value cell
    cannot be parsed as a float are skipped. Missing percentage cells are
    stored as None.

    Args:
        html: Raw HTML of the release page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: week_ending_date, index_name, index_value,
        change_pct_week (None or float), change_pct_year (None or float),
        seasonally_adjusted (bool), source_url, fetch_time.

    Raises:
        ValueError: When html is empty or no mortgage-applications table is
            found in the page.
    """
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to MBA WAS parser")

    soup = BeautifulSoup(html, "lxml")
    table = _find_data_table(soup)
    if table is None:
        raise ValueError("No mortgage applications table found in release page")

    week_ending = _parse_week_ending_date(html) or ""
    val_col, pct_week_col, pct_year_col = _find_column_indices(table)

    fetch_ts = datetime.now(timezone.utc).isoformat()
    records: list[dict] = []

    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) <= 1:
            continue

        raw_name = cells[0].get_text(strip=True)
        if not raw_name or raw_name.lower() in _SKIP_ROW_LABELS:
            continue

        if len(cells) <= val_col:
            continue

        raw_val = cells[val_col].get_text(strip=True).replace(",", "")
        try:
            index_value = float(raw_val)
        except ValueError:
            continue

        index_name, is_sa = _normalize_index_name(raw_name)

        pct_week: float | None = None
        if len(cells) > pct_week_col:
            pct_week = _parse_pct(cells[pct_week_col].get_text(strip=True))

        pct_year: float | None = None
        if len(cells) > pct_year_col:
            pct_year = _parse_pct(cells[pct_year_col].get_text(strip=True))

        records.append(
            {
                "week_ending_date": week_ending,
                "index_name": index_name,
                "index_value": index_value,
                "change_pct_week": pct_week,
                "change_pct_year": pct_year,
                "seasonally_adjusted": is_sa,
                "source_url": source_url,
                "fetch_time": fetch_ts,
            }
        )

    return records


def _extract_release_links(html: str, base_url: str = BASE_URL) -> list[tuple[str, str]]:
    """Extract (week_ending_date, absolute_url) pairs from the WAS listing page.

    Scans all anchor elements whose href contains "weekly-applications-survey"
    or "mortgage-applications". For each candidate link, attempts to parse the
    week-ending date from the link text using _WEEK_ENDING_PATTERNS. Falls back
    to the date embedded in the URL path (/YYYY/MM/DD/) when the link text
    yields no date. Results are sorted newest-first by date.

    Args:
        html: Raw HTML of the WAS listing page.
        base_url: Base URL used to resolve relative hrefs.

    Returns:
        List of (week_ending_date, absolute_url) tuples sorted descending by
        date. week_ending_date is "YYYY-MM-DD". Empty list when no matching
        links are found.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href: str = a["href"]
        href_lower = href.lower()
        if "weekly-applications-survey" not in href_lower and "mortgage-applications" not in href_lower:
            continue

        abs_url = urljoin(base_url, href)
        if abs_url in seen_urls:
            continue

        week_date: str | None = None
        link_text = a.get_text()
        for pattern in _WEEK_ENDING_PATTERNS:
            m = pattern.search(link_text)
            if m:
                try:
                    dt = datetime.strptime(m.group(1).strip(), "%B %d, %Y")
                    week_date = dt.strftime("%Y-%m-%d")
                    break
                except ValueError:
                    pass

        if week_date is None:
            url_date_m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", href)
            if url_date_m:
                week_date = f"{url_date_m.group(1)}-{url_date_m.group(2)}-{url_date_m.group(3)}"

        if week_date is not None:
            results.append((week_date, abs_url))
            seen_urls.add(abs_url)

    results.sort(key=lambda x: x[0], reverse=True)
    return results


def scrape() -> list[dict]:
    """Fetch the most-recent MBA WAS release and return parsed records.

    Fetches the landing page, extracts the most-recent release link (by
    week-ending date), fetches that release page, and parses it. Sleeps ≥3 s
    after each fetch to satisfy the polite-crawl acceptance criterion.

    Returns:
        Same structure as run().

    Raises:
        ValueError: When no release links are found on the landing page or no
            records are parsed from the release.
        RuntimeError: When robots.txt disallows SOURCE_URL or the release URL.
    """
    listing_resp = fetch(SOURCE_URL)
    time.sleep(3)

    links = _extract_release_links(listing_resp.text, BASE_URL)
    if not links:
        raise ValueError("No release links found on MBA WAS landing page")

    _, release_url = links[0]
    release_resp = fetch(release_url)
    time.sleep(3)

    return run(release_resp.text, source_url=release_url)


def backfill(start_date: str, end_date: str) -> list[dict]:
    """Scrape all MBA WAS releases whose week-ending date falls in [start_date, end_date].

    Fetches the listing page, filters extracted links to those whose
    week-ending date is within the closed date range, then fetches and parses
    each matching release page with ≥3 s sleep between requests.

    Args:
        start_date: ISO-8601 date string (YYYY-MM-DD), inclusive lower bound
            for the week_ending_date.
        end_date: ISO-8601 date string (YYYY-MM-DD), inclusive upper bound.

    Returns:
        Flat list of all parsed records across matching releases, in the same
        format as run().

    Raises:
        ValueError: When start_date > end_date or no matching releases are
            found in the date range.
        RuntimeError: Propagated from fetch() when robots.txt disallows a URL.
    """
    if start_date > end_date:
        raise ValueError(
            f"start_date {start_date!r} must be <= end_date {end_date!r}"
        )

    listing_resp = fetch(SOURCE_URL)
    time.sleep(3)

    links = _extract_release_links(listing_resp.text, BASE_URL)
    matching = [(d, u) for d, u in links if start_date <= d <= end_date]

    if not matching:
        raise ValueError(
            f"No MBA WAS releases found in date range [{start_date}, {end_date}]"
        )

    all_records: list[dict] = []
    for _, url in matching:
        resp = fetch(url)
        time.sleep(3)
        all_records.extend(run(resp.text, source_url=url))

    return all_records


def _record_to_proto(record: dict) -> MortgageApplicationsRecord:
    """Convert a parsed record dict to a MortgageApplicationsRecord stub.

    Args:
        record: Dict as returned by run().

    Returns:
        Populated MortgageApplicationsRecord dataclass with fetch_time set
        to the current UTC time in ISO-8601 format.
    """
    msg = MortgageApplicationsRecord()
    msg.week_ending_date = record["week_ending_date"]
    msg.index_name = record["index_name"]
    msg.index_value = record["index_value"]
    msg.change_pct_week = record["change_pct_week"]
    msg.change_pct_year = record["change_pct_year"]
    msg.seasonally_adjusted = record["seasonally_adjusted"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape MBA WAS data and upload records to BigQuery.

    Calls scrape(), converts each record to a MortgageApplicationsRecord proto
    stub, and uploads via upload_rows with week_ending_date as the dedup column
    so previously-uploaded weeks are not re-inserted.

    Returns:
        Count of successfully inserted rows.

    Raises:
        ValueError: When scrape() finds no records.
        KeyError: When BQ_PROJECT or BQ_DATASET environment variables are unset.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("mba_mortgage_applications", messages, date_column="week_ending_date")


if __name__ == "__main__":
    main()
