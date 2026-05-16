"""US Drought Monitor weekly statistics scraper.

Fetches the comprehensive statistics HTML table from droughtmonitor.unl.edu
for a bounded date range and emits one DroughtRecord per top-level region
(CONUS, Alaska, Hawaii, Puerto Rico) per week.

HTTP fetching is delegated to src.http_client.fetch, which enforces
robots.txt compliance, a polite 2–5 s sleep, and exponential backoff on
429/5xx responses.  An additional 3 s courtesy delay is applied after each
fetch to stay within the site's implicit rate limit.
"""

import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.http_client import fetch
from protos.us_drought_monitor_pb2 import DroughtRecord  # type: ignore[attr-defined]

SOURCE_URL = (
    "https://droughtmonitor.unl.edu/DmData/DataDownload/ComprehensiveStatistics.aspx"
)

TOP_LEVEL_REGIONS: frozenset[str] = frozenset(
    {"conus", "alaska", "hawaii", "puerto rico"}
)

REQUIRED_FIELDS: list[str] = [
    "release_date",
    "region",
    "d0_percent",
    "d1_percent",
    "d2_percent",
    "d3_percent",
    "d4_percent",
    "source_url",
]

_D_COLUMNS = ("D0", "D1", "D2", "D3", "D4")
_REGION_COL_HEADERS = ("Name", "StateName", "State", "Region")


def _normalize_col_header(raw: str) -> str:
    """Strip long-form suffixes from drought column labels.

    Converts 'D0 - Abnormally Dry' to 'D0', leaving plain headers like
    'MapDate' or 'Name' unchanged.

    Args:
        raw: Header cell text as returned by BeautifulSoup get_text().

    Returns:
        Normalised column label.
    """
    return raw.split(" - ")[0].strip()


def _build_col_map(header_cells: list) -> dict[str, int]:
    """Map normalised column names to their zero-based cell indices.

    Args:
        header_cells: List of BeautifulSoup Tag objects from the header row.

    Returns:
        Dict mapping normalised header text to column index.
    """
    return {
        _normalize_col_header(cell.get_text(strip=True)): idx
        for idx, cell in enumerate(header_cells)
    }


def _find_region_col(col_map: dict[str, int]) -> int | None:
    """Return the column index for the region/state label column, or None.

    Tries a list of known header variations in priority order.

    Args:
        col_map: Normalised column name → index mapping from _build_col_map().

    Returns:
        Column index of the region column, or None when not found.
    """
    for candidate in _REGION_COL_HEADERS:
        if candidate in col_map:
            return col_map[candidate]
    return None


def _parse_date(raw: str) -> str | None:
    """Parse a US-locale date string like '1/2/2024' into 'YYYY-MM-DD'.

    Args:
        raw: Date text from the MapDate table cell.

    Returns:
        ISO-8601 date string, or None when the value cannot be parsed.
    """
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_float(raw: str) -> float | None:
    """Convert a cell value to float, returning None for empty/N/A values.

    Args:
        raw: Cell text, possibly 'N/A', '', or a numeric string.

    Returns:
        Parsed float, or None when the cell is blank or not numeric.
    """
    stripped = raw.strip()
    if not stripped or stripped.upper() == "N/A":
        return None
    try:
        return float(stripped)
    except ValueError:
        return None


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse a Drought Monitor statistics HTML page into a list of records.

    Finds the first table with a 'MapDate' header and a recognisable region
    column.  Only rows whose region matches TOP_LEVEL_REGIONS (case-insensitive)
    are emitted; state-level breakdown rows are silently skipped.  Rows where
    any D0–D4 cell is empty or 'N/A' are also skipped without raising.

    Args:
        html: Raw HTML string of the ComprehensiveStatistics page.
        source_url: URL the page was fetched from; stored in each record.

    Returns:
        List of dicts with keys matching REQUIRED_FIELDS.  fetch_time is
        intentionally absent — callers add it during proto conversion.
    """
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict] = []

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue

        header_cells = header_row.find_all(["th", "td"])
        col_map = _build_col_map(header_cells)

        if "MapDate" not in col_map:
            continue
        if not all(d in col_map for d in _D_COLUMNS):
            continue

        region_col = _find_region_col(col_map)
        if region_col is None:
            continue

        date_col = col_map["MapDate"]

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue

            if region_col >= len(cells) or date_col >= len(cells):
                continue

            region_raw = cells[region_col].get_text(strip=True).lower()
            if region_raw not in TOP_LEVEL_REGIONS:
                continue

            release_date = _parse_date(cells[date_col].get_text(strip=True))
            if release_date is None:
                continue

            d_vals: dict[str, float] = {}
            skip = False
            for d_col in _D_COLUMNS:
                col_idx = col_map[d_col]
                if col_idx >= len(cells):
                    skip = True
                    break
                val = _parse_float(cells[col_idx].get_text(strip=True))
                if val is None:
                    skip = True
                    break
                d_vals[d_col] = val

            if skip:
                continue

            records.append(
                {
                    "release_date": release_date,
                    "region": region_raw,
                    "d0_percent": d_vals["D0"],
                    "d1_percent": d_vals["D1"],
                    "d2_percent": d_vals["D2"],
                    "d3_percent": d_vals["D3"],
                    "d4_percent": d_vals["D4"],
                    "source_url": source_url,
                }
            )

        if records:
            break

    return records


def scrape(
    startdate: str = "2024-01-01",
    enddate: str = "2024-03-31",
) -> list[dict]:
    """Fetch a bounded date range from the Drought Monitor and return records.

    Constructs the request URL with startdate/enddate GET parameters and
    statType=0 (percentage of area).  Delegates to src.http_client.fetch for
    robots.txt compliance, User-Agent enforcement, and retry logic.  Sleeps an
    additional 3 s after the fetch to stay within the site's rate limit.

    Args:
        startdate: Start of the date range, 'YYYY-MM-DD'.
        enddate:   End of the date range, 'YYYY-MM-DD'.

    Returns:
        Same structure as run().
    """
    url = f"{SOURCE_URL}?startdate={startdate}&enddate={enddate}&statType=0"
    resp = fetch(url)
    time.sleep(3)
    return run(resp.text, source_url=url)


def _record_to_proto(record: dict) -> DroughtRecord:
    """Convert a parsed record dict to a DroughtRecord proto message.

    Args:
        record: Dict with keys matching REQUIRED_FIELDS.

    Returns:
        Populated DroughtRecord with fetch_time set to the current UTC time.
    """
    msg = DroughtRecord()
    msg.release_date = record["release_date"]
    msg.region = record["region"]
    msg.d0_percent = record["d0_percent"]
    msg.d1_percent = record["d1_percent"]
    msg.d2_percent = record["d2_percent"]
    msg.d3_percent = record["d3_percent"]
    msg.d4_percent = record["d4_percent"]
    msg.source_url = record["source_url"]
    msg.fetch_time.FromDatetime(datetime.now(timezone.utc))
    return msg


def main() -> int:
    """Scrape Drought Monitor data and upload records to BigQuery.

    Calls scrape() with default date bounds, converts each record to a
    DroughtRecord proto, and uploads via upload_rows.  Returns the count of
    inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("us_drought_monitor", messages, date_column="release_date")


if __name__ == "__main__":
    main()
