"""EIA weekly net electricity generation by fuel-type scraper.

Parses the HTML table published at SOURCE_URL, which lists weekly net
generation in thousand MWh broken down by fuel type (Coal, Natural Gas,
Nuclear, Wind, Solar, Hydro, and others).  No API key required.
"""

from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.eia_electricity_pb2 import EiaElectricityRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.eia.gov/electricity/weekly/"

REQUIRED_FIELDS: list[str] = [
    "source_url",
    "week_ending_date",
    "fuel_type",
    "generation_thousand_mwh",
]

_SKIP_VALUES = {"W", "--", ""}


def _find_generation_table(soup: BeautifulSoup):
    """Return the first table whose header row includes both Coal and Natural Gas columns.

    The EIA Electric Power Weekly page contains several tables; the Net
    Generation table is identified by the presence of fuel-type column headers.
    Returns None if no matching table is found.
    """
    for table in soup.find_all("table"):
        first_row = table.select_one("tr")
        if first_row is None:
            continue
        cells = first_row.find_all(["th", "td"])
        texts = {c.get_text(strip=True).lower() for c in cells}
        if "coal" in texts and "natural gas" in texts:
            return table
    return None


def run(html: str) -> list[dict]:
    """Parse EIA Electric Power Weekly HTML into a list of generation records.

    Finds the Net Generation by Energy Source table (identified by Coal and
    Natural Gas column headers), extracts fuel-type labels from the header row,
    then iterates data rows.  For each data row:
      - Rows whose date cell contains "Total" are skipped (aggregate rows).
      - The date cell is parsed from MM/DD/YYYY format; non-date rows are skipped.
      - For each fuel-type cell, values of "W" (withheld), "--", or empty are skipped.
      - Commas are stripped from numeric strings before conversion to float.

    Args:
        html: Raw HTML string of the EIA Electric Power Weekly page.

    Returns:
        List of dicts with keys: source_url, week_ending_date (YYYY-MM-DD string),
        fuel_type (str), generation_thousand_mwh (float).  fetch_time is omitted —
        callers that need it should add it after calling this function.
    """
    soup = BeautifulSoup(html, "lxml")
    table = _find_generation_table(soup)
    if table is None:
        return []

    rows = table.find_all("tr")
    if not rows:
        return []

    header_cells = rows[0].find_all(["th", "td"])
    fuel_types = [c.get_text(strip=True) for c in header_cells[1:]]

    records: list[dict] = []
    for tr in rows[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if not cells:
            continue

        date_cell = cells[0]
        if "Total" in date_cell or not date_cell:
            continue

        try:
            dt = datetime.strptime(date_cell, "%m/%d/%Y")
            week_ending_date = dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

        for i, fuel_type in enumerate(fuel_types):
            if i + 1 >= len(cells):
                break
            raw_value = cells[i + 1].replace(",", "").strip()
            if raw_value in _SKIP_VALUES:
                continue
            try:
                value = float(raw_value)
            except ValueError:
                continue
            records.append(
                {
                    "source_url": SOURCE_URL,
                    "week_ending_date": week_ending_date,
                    "fuel_type": fuel_type,
                    "generation_thousand_mwh": value,
                }
            )

    return records


def scrape() -> list[dict]:
    """Fetch the live EIA Electric Power Weekly page and return parsed records.

    Delegates HTTP fetching to ``src.http_client.fetch``, which enforces
    robots.txt compliance, a polite 2–5 s delay, and exponential backoff on
    transient errors.

    Returns:
        Same structure as ``run()``.
    """
    resp = fetch(SOURCE_URL)
    return run(resp.text)


def _record_to_proto(record: dict) -> EiaElectricityRecord:
    msg = EiaElectricityRecord()
    msg.source_url = record["source_url"]
    msg.week_ending_date = record["week_ending_date"]
    msg.fuel_type = record["fuel_type"]
    msg.generation_thousand_mwh = record["generation_thousand_mwh"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape EIA electricity generation data and upload records to BigQuery.

    Calls scrape(), converts each record to an EiaElectricityRecord proto, and
    uploads via upload_rows. Returns the count of successfully inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("eia_electricity_generation", messages, date_column="week_ending_date")


if __name__ == "__main__":
    main()
