"""NSIDC Arctic Sea Ice Extent monthly scraper.

Fetches the NSIDC Sea Ice Index page to discover the link to the Northern
Hemisphere monthly extent CSV file, downloads it, parses CSV rows, and
uploads one record per valid observation to BigQuery.

The data file uses CSV format with columns:
  Year, Mo, Data type, Region, Extent, Area
Rows beginning with '#' are comment lines to skip.  Rows where Extent equals
the missing-data sentinel (-9999.00) are also skipped.  If Area is the
sentinel, it is stored as 0.0 (area data has historical gaps whereas extent
is the primary measurement).

HTTP fetching (robots.txt, polite delay, backoff) is handled by
src.scrapers.http_client.fetch.
"""

from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.nsidc_sea_ice_pb2 import NsidcSeaIceRecord  # type: ignore[attr-defined]

INDEX_URL = "https://nsidc.org/data/seaice_index/"
_BASE_URL = "https://nsidc.org"
_MISSING_SENTINEL = -9999.0


def _discover_data_url(html: str) -> str:
    """Parse the NSIDC sea ice index page and return the Northern Hemisphere data file URL.

    Searches all anchor tags for an href containing 'N_seaice_extent_monthly',
    which identifies the Northern Hemisphere monthly extent CSV file.
    Relative hrefs are made absolute by prepending _BASE_URL.

    Args:
        html: Raw HTML content of the NSIDC sea ice index page.

    Returns:
        Absolute URL of the monthly extent data file.

    Raises:
        ValueError: If no matching href is found in the page.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if "N_seaice_extent_monthly" in href:
            if href.startswith("http"):
                return href
            return _BASE_URL + href
    raise ValueError(
        f"Could not find monthly extent data URL in {INDEX_URL}; "
        "expected an anchor with 'N_seaice_extent_monthly' in its href."
    )


def parse_lines(lines: list[str]) -> list[NsidcSeaIceRecord]:
    """Parse CSV lines from the NSIDC Northern Hemisphere monthly sea ice extent file.

    Skips lines that start with '#' (comments) or are blank.  The header row
    ('Year, Mo, ...') is skipped because 'Year' cannot be parsed as an integer.
    Each data line is comma-separated with columns:
      Year, Mo, Data type, Region, Extent, Area
    Rows where Extent equals the missing-data sentinel (-9999.00) are skipped.
    Rows where Area is the sentinel have area_million_sq_km set to 0.0 instead
    of being dropped (area records have historical gaps that do not invalidate
    the extent measurement).  Rows with fewer than 6 columns or unparseable
    numeric fields are silently skipped.

    Args:
        lines: Lines of the CSV file including comment and header lines.

    Returns:
        List of NsidcSeaIceRecord instances, one per valid observation row.
        All records in a single call share the same fetch_time timestamp.
    """
    records: list[NsidcSeaIceRecord] = []
    fetch_time = datetime.now(timezone.utc)

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        parts = [p.strip() for p in stripped.split(",")]
        if len(parts) < 6:
            continue

        try:
            year = int(parts[0])
            month = int(parts[1])
            extent = float(parts[4])
            area = float(parts[5])
        except (ValueError, IndexError):
            continue

        if extent <= _MISSING_SENTINEL + 1:
            continue

        area_val = 0.0 if area <= _MISSING_SENTINEL + 1 else area

        rec = NsidcSeaIceRecord(
            year=year,
            month=month,
            extent_million_sq_km=extent,
            area_million_sq_km=area_val,
            source_url=INDEX_URL,
        )
        rec.fetch_time.FromDatetime(fetch_time)
        records.append(rec)

    return records


def scrape() -> list[NsidcSeaIceRecord]:
    """Fetch NSIDC sea ice data and return parsed records.

    Fetches the NSIDC sea ice index page, discovers the Northern Hemisphere
    monthly extent data URL via BeautifulSoup href parsing, then downloads and
    parses the CSV file.  Uses src.scrapers.http_client.fetch for robots.txt
    compliance, polite delay, and exponential backoff on 429/5xx responses.

    Returns:
        List of NsidcSeaIceRecord instances, one per valid observation.
    """
    index_resp = fetch(INDEX_URL)
    data_url = _discover_data_url(index_resp.text)
    data_resp = fetch(data_url)
    return parse_lines(data_resp.text.splitlines())


def main() -> int:
    """Scrape NSIDC Arctic sea ice data and upload records to BigQuery.

    Returns:
        Count of rows successfully inserted.
    """
    records = scrape()
    return upload_rows("nsidc_sea_ice", records, date_column="")


if __name__ == "__main__":
    main()
