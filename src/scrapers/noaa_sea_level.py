"""NOAA Tide Gauge Monthly Mean Sea Level scraper.

Fetches per-station monthly mean sea level (MSL) data from the NOAA Tides
and Currents website.  Each station's HTML trend page at:
  https://tidesandcurrents.noaa.gov/sltrends/sltrends_station.shtml?id=<ID>
publishes a wide-format HTML table: one row per year, one column per
calendar month (abbreviated to three letters).  BeautifulSoup locates the
table by finding the first row whose initial header cell is "Year", then
unpivots each row into one SeaLevelRecord per valid (year, month) cell.

Missing values encoded as -99999 are silently skipped.  MSL values are
stored in millimetres (metres × 1000).

HTTP fetching (robots.txt compliance, polite delay, exponential backoff)
is handled by src.scrapers.http_client.fetch.  An additional minimum sleep
of 3 seconds is enforced between consecutive station fetches.
"""

import logging
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.noaa_sea_level_pb2 import SeaLevelRecord  # type: ignore[attr-defined]

log = logging.getLogger(__name__)

_STATION_URL = (
    "https://tidesandcurrents.noaa.gov/sltrends/sltrends_station.shtml?id={station_id}"
)

_MISSING_SENTINEL = -99999.0
_MIN_STATION_SLEEP = 3.0

_MONTH_ABBREVS: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_DEFAULT_STATIONS: dict[str, str] = {
    "8518750": "The Battery, New York",
    "9414290": "San Francisco, CA",
    "9447130": "Seattle, WA",
    "8443970": "Boston, MA",
    "8658120": "Wilmington, NC",
}


def _station_url(station_id: str) -> str:
    return _STATION_URL.format(station_id=station_id)


def _extract_station_name(
    soup: BeautifulSoup,
    station_id: str,
    fallback_names: dict[str, str],
) -> str:
    """Return the station's display name extracted from the page or a fallback.

    Searches h1/h2/h3 heading tags for the station_id; when found, strips
    the ID prefix (e.g. "8518750 - ") and returns the remainder as the name.
    Falls back to the provided dict if the station_id appears in none of the
    heading tags.  Title tags are intentionally excluded: NOAA page titles
    carry the ID on the right side of the dash ("Page - ID"), which causes
    split("-", 1)[1] to resolve to the bare ID rather than the name.

    Args:
        soup: Parsed BeautifulSoup of the station HTML page.
        station_id: NOAA station identifier used to locate the heading.
        fallback_names: Dict mapping station_id -> human-readable name.

    Returns:
        Station name string.
    """
    for tag in soup.find_all(["h1", "h2", "h3"]):
        text = tag.get_text(" ", strip=True)
        if station_id in text:
            parts = text.split("-", 1)
            if len(parts) == 2:
                return parts[1].strip()
            return text
    return fallback_names.get(station_id, station_id)


def parse_html(
    html: str,
    station_id: str,
    station_name: str,
    source_url: str,
) -> list[SeaLevelRecord]:
    """Parse an HTML page from the NOAA Sea Level Trends station page.

    Locates the first HTML table whose initial header cell contains "year"
    (case-insensitive).  Treats subsequent header cells as abbreviated month
    names (Jan, Feb, …, Dec) and maps them to integers 1-12.

    For each body row:
    - Reads the first cell as the year (integer).
    - Reads each month column; skips blank cells, non-numeric cells, and
      cells whose float value equals -99999 (the NOAA missing-data sentinel).
    - Converts valid metre values to millimetres (× 1000) before storing.

    All records produced in one call share the same fetch_time timestamp.

    Args:
        html: Raw HTML string of the NOAA station trend page.
        station_id: NOAA station identifier (stored verbatim in each record).
        station_name: Human-readable station name (stored in each record).
        source_url: URL the HTML was fetched from (stored in each record).

    Returns:
        List of SeaLevelRecord instances, one per valid (year, month) cell.
    """
    soup = BeautifulSoup(html, "lxml")
    fetch_time = datetime.now(timezone.utc).isoformat()
    records: list[SeaLevelRecord] = []

    data_table = None
    for table in soup.find_all("table"):
        first_row = table.find("tr")
        if first_row is None:
            continue
        first_cells = first_row.find_all(["th", "td"])
        if first_cells and first_cells[0].get_text(strip=True).lower() == "year":
            data_table = table
            break

    if data_table is None:
        log.warning("No MSL data table found for station %s", station_id)
        return records

    rows = data_table.find_all("tr")
    if len(rows) < 2:
        return records

    header_cells = rows[0].find_all(["th", "td"])
    col_to_month: dict[int, int] = {}
    for col_idx, cell in enumerate(header_cells[1:], start=1):
        abbrev = cell.get_text(strip=True).lower()[:3]
        month_num = _MONTH_ABBREVS.get(abbrev)
        if month_num is not None:
            col_to_month[col_idx] = month_num

    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        try:
            year = int(cells[0].get_text(strip=True))
        except ValueError:
            continue

        for col_idx, month in col_to_month.items():
            if col_idx >= len(cells):
                continue

            raw = cells[col_idx].get_text(strip=True)
            if not raw:
                continue

            try:
                msl_m = float(raw)
            except ValueError:
                continue

            if abs(msl_m - _MISSING_SENTINEL) < 0.5:
                continue

            records.append(
                SeaLevelRecord(
                    station_id=station_id,
                    station_name=station_name,
                    year=year,
                    month=month,
                    mean_sea_level_mm=msl_m * 1000.0,
                    source_url=source_url,
                    fetch_time=fetch_time,
                )
            )

    return records


def scrape_station(
    station_id: str,
    station_names: dict[str, str] | None = None,
) -> list[SeaLevelRecord]:
    """Fetch and parse sea level data for one station.

    Constructs the station HTML URL, fetches via http_client.fetch (which
    enforces robots.txt, polite delay, and exponential backoff), then
    delegates parsing to parse_html.  HTTP errors cause a warning log and
    an empty list return rather than a raised exception.

    Args:
        station_id: NOAA station identifier (e.g. "8518750").
        station_names: Optional dict mapping station_id -> display name.
            Falls back to _DEFAULT_STATIONS when None.

    Returns:
        List of SeaLevelRecord instances for this station; empty on error.
    """
    names = station_names if station_names is not None else _DEFAULT_STATIONS
    url = _station_url(station_id)

    try:
        resp = fetch(url)
    except Exception as exc:
        log.warning("Skipping station %s: %s", station_id, exc)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    station_name = _extract_station_name(soup, station_id, names)
    return parse_html(resp.text, station_id, station_name, url)


def scrape(
    station_ids: list[str] | None = None,
    station_names: dict[str, str] | None = None,
) -> list[SeaLevelRecord]:
    """Scrape MSL data for a configurable list of stations.

    Fetches each station in sequence.  Sleeps at least _MIN_STATION_SLEEP
    seconds between consecutive station fetches (in addition to the polite
    delay already applied by http_client.fetch).

    Stations that return HTTP errors are warned and skipped rather than
    causing the entire run to abort.

    Args:
        station_ids: List of NOAA station IDs to scrape.  Defaults to the
            five-station set defined in _DEFAULT_STATIONS.
        station_names: Optional override for station display name strings.

    Returns:
        Combined list of SeaLevelRecord instances from all stations.
    """
    ids = station_ids if station_ids is not None else list(_DEFAULT_STATIONS.keys())
    all_records: list[SeaLevelRecord] = []

    for i, station_id in enumerate(ids):
        if i > 0:
            time.sleep(_MIN_STATION_SLEEP)
        records = scrape_station(station_id, station_names)
        all_records.extend(records)
        log.info("Station %s: %d records", station_id, len(records))

    return all_records


def main() -> int:
    """Scrape NOAA tide gauge monthly MSL data and upload to BigQuery.

    Returns:
        Count of rows successfully inserted.
    """
    records = scrape()
    return upload_rows("noaa_sea_level", records, date_column="year")


if __name__ == "__main__":
    main()
