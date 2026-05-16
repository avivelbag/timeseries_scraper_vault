"""Federal Reserve H.15 selected interest rates HTML scraper.

Fetches the H.15 release page from federalreserve.gov and parses the
transposed HTML table (rates-as-rows, dates-as-columns) into per-(series,date)
records.  The pivot strategy reads date headers from the thead and iterates
tbody rows for series labels and cell values.
"""

import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.fed_h15_rates_pb2 import FedH15Record  # type: ignore[attr-defined]

SOURCE_URL = "https://www.federalreserve.gov/releases/h15/"

REQUIRED_FIELDS: list[str] = ["period_date", "series_name", "rate"]

_MATURITY_MONTH = re.compile(r"(\d+)\s*[-]?\s*(month|mo)\b", re.IGNORECASE)
_MATURITY_YEAR = re.compile(r"(\d+)\s*[-]?\s*(year|yr)\b", re.IGNORECASE)
_YEAR_PATTERN = re.compile(r"\b(20\d{2}|19\d{2})\b")


def _page_year(soup: BeautifulSoup) -> int:
    """Extract the most recent year mentioned in the page title or caption.

    Falls back to the current UTC year when no four-digit year is found.

    Args:
        soup: Parsed BeautifulSoup document.

    Returns:
        Four-digit integer year.
    """
    for tag in ("title", "caption"):
        el = soup.find(tag)
        if el:
            match = _YEAR_PATTERN.search(el.get_text())
            if match:
                return int(match.group(1))
    return datetime.now(timezone.utc).year


def _parse_date(date_str: str, fallback_year: int) -> str | None:
    """Normalise an H.15 column header date string to ISO-8601.

    Tries the full "Month DD, YYYY" format first, then falls back to
    "Month DD" using *fallback_year*.  Returns None if neither format matches.

    Args:
        date_str: Raw text from a thead <th> cell, e.g. "May 12, 2025" or "May 12".
        fallback_year: Year to apply when the string omits it.

    Returns:
        ISO-8601 date string (YYYY-MM-DD), or None on parse failure.
    """
    try:
        return datetime.strptime(date_str, "%B %d, %Y").date().isoformat()
    except ValueError:
        pass
    try:
        dt = datetime.strptime(date_str, "%B %d")
        return dt.replace(year=fallback_year).date().isoformat()
    except ValueError:
        return None


def _extract_maturity(series_name: str) -> str:
    """Return a short maturity code parsed from a series label.

    Recognises patterns like "3-month", "6 month", "1-year", "2 yr".
    Returns an empty string when no maturity hint is found.

    Args:
        series_name: Full series label from the H.15 row header.

    Returns:
        Short code such as "3m", "6m", "1y", or "" if not found.
    """
    m = _MATURITY_MONTH.search(series_name)
    if m:
        return f"{m.group(1)}m"
    m = _MATURITY_YEAR.search(series_name)
    if m:
        return f"{m.group(1)}y"
    return ""


def _extract_frequency(series_name: str) -> str:
    """Infer publication frequency from the series label text.

    Args:
        series_name: Full series label from the H.15 row header.

    Returns:
        "weekly", "monthly", or "daily".
    """
    lower = series_name.lower()
    if "weekly" in lower:
        return "weekly"
    if "monthly" in lower:
        return "monthly"
    return "daily"


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse a Federal Reserve H.15 HTML page into per-(series, date) records.

    Uses a pivot strategy: date strings are extracted from the thead column
    headers, then each tbody row contributes one record per date column.  Rows
    that have no <td> cells (section-divider headers) and rows with an empty
    series label are silently skipped.  "ND" cells map to rate = -1.0; all
    other cells are parsed as float and rows with unparseable values are
    skipped.

    Args:
        html: Raw HTML string of the H.15 release page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: period_date, series_name, maturity, frequency,
        rate, source_url.
    """
    soup = BeautifulSoup(html, "lxml")
    fallback_year = _page_year(soup)
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

        # Skip the first "Instruments" cell; remaining cells are date columns.
        date_strings = [c.get_text(strip=True) for c in header_cells[1:]]
        dates = [_parse_date(ds, fallback_year) for ds in date_strings]

        if not any(d is not None for d in dates):
            continue

        tbody = table.find("tbody")
        if tbody is None:
            continue

        for tr in tbody.find_all("tr"):
            th = tr.find("th")
            if th is None:
                continue
            series_name = th.get_text(strip=True)

            tds = tr.find_all("td")
            # Rows without <td> cells are section dividers; empty labels are noise.
            if not tds or not series_name:
                continue

            maturity = _extract_maturity(series_name)
            frequency = _extract_frequency(series_name)

            for i, td in enumerate(tds):
                if i >= len(dates) or dates[i] is None:
                    continue
                cell_text = td.get_text(strip=True)
                if cell_text == "ND":
                    rate = -1.0
                else:
                    try:
                        rate = float(cell_text)
                    except ValueError:
                        continue

                records.append(
                    {
                        "period_date": dates[i],
                        "series_name": series_name,
                        "maturity": maturity,
                        "frequency": frequency,
                        "rate": rate,
                        "source_url": source_url,
                    }
                )

        if records:
            break

    return records


def scrape() -> list[dict]:
    """Fetch the live H.15 page and return parsed records.

    Delegates HTTP fetching to ``src.http_client.fetch``, which enforces a
    User-Agent header, a 2–5 s polite delay, and exponential backoff on
    429/5xx responses.  An additional 3-second sleep is applied after the
    fetch as a courtesy to the Federal Reserve server.

    Returns:
        Same structure as ``run()``.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def _record_to_proto(record: dict) -> FedH15Record:
    msg = FedH15Record()
    msg.period_date = record["period_date"]
    msg.series_name = record["series_name"]
    msg.maturity = record["maturity"]
    msg.frequency = record["frequency"]
    msg.rate = record["rate"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape H.15 interest rate data and upload records to BigQuery.

    Calls scrape(), converts each record to a FedH15Record proto, and uploads
    via upload_rows.  Returns the count of successfully inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("fed_h15_rates", messages)


if __name__ == "__main__":
    main()
