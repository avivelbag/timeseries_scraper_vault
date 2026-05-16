"""NASA GISS Global Surface Temperature Anomaly scraper.

Fetches the fixed-width plain-text table of monthly global mean surface
temperature anomalies (relative to the 1951-1980 baseline) published by NASA
GISS and uploads one record per valid (year, month) cell to BigQuery.

The source file encodes anomaly values in hundredths of a degree Celsius.
A single HTTP GET suffices; there is no pagination.  HTTP retries with
exponential backoff on 429/5xx and polite rate-limiting are delegated to
src.http_client.fetch.
"""

from datetime import datetime, timezone

from src.bq_uploader import upload_rows
from src.http_client import fetch
from protos.nasa_giss_temp_pb2 import NasaGissTempRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://data.giss.nasa.gov/gistemp/tabledata_v4/GLB.Ts+dSST.txt"

_SENTINEL = "****"


def parse_lines(lines: list[str]) -> list[NasaGissTempRecord]:
    """Parse text lines from the NASA GISS GLB.Ts+dSST.txt file.

    Skips header lines whose first whitespace-separated token is not a
    four-digit decimal year string.  For each data line, reads columns 1–12
    (Jan–Dec) and converts from hundredths of a degree Celsius to degrees
    Celsius by dividing by 100.  Cells whose value equals the sentinel "****"
    are silently skipped.  Columns beyond index 12 (seasonal averages) are
    ignored.

    Args:
        lines: Lines of the text file, including header and blank lines.

    Returns:
        List of NasaGissTempRecord instances, one per valid (year, month) cell.
        All records in a single call share the same fetch_time timestamp.
    """
    records: list[NasaGissTempRecord] = []
    fetch_time = datetime.now(timezone.utc).isoformat()
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        year_str = parts[0]
        if not (year_str.isdigit() and len(year_str) == 4):
            continue
        year = int(year_str)
        for col in range(1, 13):
            if col >= len(parts):
                break
            value_str = parts[col]
            if value_str == _SENTINEL:
                continue
            try:
                anomaly_c = int(value_str) / 100.0
            except ValueError:
                continue
            month = col
            records.append(
                NasaGissTempRecord(
                    year=year,
                    month=month,
                    year_month=f"{year}-{month:02d}",
                    anomaly_c=anomaly_c,
                    source_url=SOURCE_URL,
                    fetch_time=fetch_time,
                )
            )
    return records


def scrape() -> list[NasaGissTempRecord]:
    """Fetch the NASA GISS temperature anomaly file and return parsed records.

    Delegates to src.http_client.fetch, which sets the project User-Agent,
    sleeps 2–5 s before the first request, and retries on 429/5xx with
    exponential backoff (minimum 2 s between retries).

    Returns:
        List of NasaGissTempRecord instances, one per valid (year, month) cell.
    """
    resp = fetch(SOURCE_URL)
    return parse_lines(resp.text.splitlines())


def main() -> int:
    """Scrape NASA GISS temperature anomaly data and upload records to BigQuery.

    Returns:
        Count of rows successfully inserted.
    """
    records = scrape()
    return upload_rows("nasa_giss_temp", records, date_column="year_month")


if __name__ == "__main__":
    main()
