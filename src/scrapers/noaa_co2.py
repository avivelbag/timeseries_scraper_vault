"""NOAA Mauna Loa Monthly CO2 Concentration scraper.

Fetches the plain-text monthly average CO2 file published by NOAA GML and
uploads one record per valid observation row to BigQuery.

The source file is whitespace-separated with seven columns:
  year  month  decimal_year  monthly_avg  interpolated  trend  ndays

Rows where monthly_avg equals the sentinel -99.99 indicate missing data and
are silently skipped.  Comment lines begin with '#' and are also skipped.

HTTP fetching (robots.txt, polite delay, backoff) is handled by
src.scrapers.http_client.fetch.
"""

from datetime import datetime, timezone

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.noaa_co2_pb2 import NoaaCo2Record  # type: ignore[attr-defined]

SOURCE_URL = "https://gml.noaa.gov/webdata/ccgg/trends/co2/co2_mm_mlo.txt"

_MISSING_SENTINEL = -99.99
_MIN_RECORDS = 700


def parse_lines(lines: list[str]) -> list[NoaaCo2Record]:
    """Parse text lines from the NOAA co2_mm_mlo.txt file into records.

    Skips lines that start with '#' (comments) or are blank.  Each data line
    has seven whitespace-separated columns: year, month, decimal_year,
    monthly_avg, interpolated, trend, ndays.  Rows where monthly_avg equals
    -99.99 (the NOAA missing-data sentinel) are skipped.  Column index 5
    (trend) is stored as deseasonalized_ppm.

    Args:
        lines: Lines of the text file including header comments.

    Returns:
        List of NoaaCo2Record instances, one per valid observation row.
        All records in a single call share the same fetch_time timestamp.
    """
    records: list[NoaaCo2Record] = []
    fetch_time = datetime.now(timezone.utc).isoformat()

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        parts = stripped.split()
        if len(parts) < 7:
            continue

        try:
            year = int(parts[0])
            month = int(parts[1])
            decimal_year = float(parts[2])
            monthly_avg = float(parts[3])
            trend = float(parts[5])
        except ValueError:
            continue

        if abs(monthly_avg - _MISSING_SENTINEL) < 1e-6:
            continue

        records.append(
            NoaaCo2Record(
                year=year,
                month=month,
                decimal_year=decimal_year,
                monthly_avg_ppm=monthly_avg,
                deseasonalized_ppm=trend,
                source_url=SOURCE_URL,
                fetch_time=fetch_time,
            )
        )

    return records


def scrape() -> list[NoaaCo2Record]:
    """Fetch the NOAA Mauna Loa CO2 file and return parsed records.

    Delegates to src.scrapers.http_client.fetch for robots.txt compliance,
    polite delay, and exponential backoff on 429/5xx responses.

    Returns:
        List of NoaaCo2Record instances, one per valid observation.

    Raises:
        ValueError: When fewer than 700 records are parsed, indicating a
            likely truncated or malformed response.
    """
    resp = fetch(SOURCE_URL)
    records = parse_lines(resp.text.splitlines())
    if len(records) < _MIN_RECORDS:
        raise ValueError(
            f"Only {len(records)} records parsed from {SOURCE_URL}; "
            f"expected at least {_MIN_RECORDS}. Response may be truncated."
        )
    return records


def main() -> int:
    """Scrape NOAA Mauna Loa CO2 data and upload records to BigQuery.

    Returns:
        Count of rows successfully inserted.
    """
    records = scrape()
    return upload_rows("noaa_co2", records, date_column="decimal_year")


if __name__ == "__main__":
    main()
