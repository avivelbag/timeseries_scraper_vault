"""CDC FluView Weekly Influenza Surveillance scraper.

Fetches the FluView weekly HTML report and parses the ILI (influenza-like
illness) surveillance table into per-region records for all 10 HHS regions
plus the national aggregate.

Source: https://www.cdc.gov/flu/weekly/flureport.htm
Table columns expected: HHS Region, Year, MMWR Week, % Weighted ILI,
    % Unweighted ILI, Total Patients, ILI Patients.
"""

import datetime
from datetime import timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.http_client import fetch
from protos.cdc_fluview_pb2 import CdcFluviewRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.cdc.gov/flu/weekly/flureport.htm"

REQUIRED_FIELDS: list[str] = ["week_ending_date", "year", "week", "region", "ili_percent"]

_VALID_REGIONS = frozenset(
    {"National"} | {f"Region {i}" for i in range(1, 11)}
)


def _mmwr_week_to_saturday(year: int, week: int) -> str:
    """Return the Saturday ending MMWR week ``week`` of ``year`` as YYYY-MM-DD.

    MMWR (Morbidity and Mortality Weekly Report) weeks run Sunday–Saturday.
    Week 1 is defined as the week containing January 4.

    Args:
        year: Four-digit calendar year.
        week: MMWR week number (1–53).

    Returns:
        ISO-format date string for the Saturday that ends the given MMWR week.
    """
    jan4 = datetime.date(year, 1, 4)
    # weekday(): Mon=0 … Sun=6.  Days elapsed since the most recent Sunday:
    days_since_sunday = (jan4.weekday() + 1) % 7
    week1_sunday = jan4 - datetime.timedelta(days=days_since_sunday)
    target_sunday = week1_sunday + datetime.timedelta(weeks=week - 1)
    saturday = target_sunday + datetime.timedelta(days=6)
    return saturday.strftime("%Y-%m-%d")


def _find_col(headers: list[str], *keywords: str) -> int | None:
    """Return the index of the first header that contains all ``keywords`` (case-insensitive).

    Args:
        headers: List of column header text values.
        *keywords: Substrings that must all appear (case-insensitive) in the header.

    Returns:
        Column index, or None if no header matches.
    """
    for i, h in enumerate(headers):
        hl = h.lower()
        if all(k.lower() in hl for k in keywords):
            return i
    return None


def run(html: str) -> list[dict]:
    """Parse FluView HTML into a list of ILI surveillance records.

    Searches all ``<table>`` elements for one whose headers contain columns
    for region, year, MMWR week, weighted ILI percent, total patients, and
    ILI patients.  Returns one dict per valid data row.

    Rows are rejected when:
    - The region cell is not in the known set (National + Region 1-10).
    - The year or week cannot be parsed as integers.
    - The ILI percent cell cannot be parsed as a float after stripping ``%``.
    - The total or ILI patient counts cannot be parsed as integers.

    Args:
        html: Raw HTML string of the FluView weekly report page.

    Returns:
        List of dicts with keys: week_ending_date, year, week, region,
        ili_percent, total_patients, ili_patients, source_url.
        ``fetch_time`` is omitted — callers add it after calling this function.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        headers = [cell.get_text(strip=True) for cell in header_row.find_all(["th", "td"])]

        region_col = _find_col(headers, "region")
        year_col = _find_col(headers, "year")
        week_col = _find_col(headers, "week")
        # "% Weighted ILI" but not "% Unweighted ILI"
        ili_pct_col = next(
            (
                i for i, h in enumerate(headers)
                if "ili" in h.lower() and "weighted" in h.lower() and "un" not in h.lower()
            ),
            None,
        )
        total_col = _find_col(headers, "total patients")
        ili_pat_col = _find_col(headers, "ili patients")

        if any(c is None for c in (region_col, year_col, week_col, ili_pct_col, total_col, ili_pat_col)):
            continue

        min_cols = max(region_col, year_col, week_col, ili_pct_col, total_col, ili_pat_col) + 1  # type: ignore[arg-type]

        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < min_cols:
                continue

            region = cells[region_col]  # type: ignore[index]
            if region not in _VALID_REGIONS:
                continue

            try:
                year = int(cells[year_col])  # type: ignore[index]
                week = int(cells[week_col])  # type: ignore[index]
            except ValueError:
                continue

            raw_pct = cells[ili_pct_col].rstrip("%")  # type: ignore[index]
            try:
                ili_percent = float(raw_pct)
            except ValueError:
                continue

            try:
                total_patients = int(cells[total_col])  # type: ignore[index]
                ili_patients = int(cells[ili_pat_col])  # type: ignore[index]
            except ValueError:
                continue

            records.append(
                {
                    "week_ending_date": _mmwr_week_to_saturday(year, week),
                    "year": year,
                    "week": week,
                    "region": region,
                    "ili_percent": ili_percent,
                    "total_patients": total_patients,
                    "ili_patients": ili_patients,
                    "source_url": SOURCE_URL,
                }
            )

        if records:
            break

    return records


def scrape() -> list[dict]:
    """Fetch the live FluView page and return parsed ILI records.

    Delegates HTTP to ``src.http_client.fetch``, which enforces a 2–5 s
    polite delay, robots.txt compliance, and exponential backoff on 5xx errors.

    Returns:
        Same structure as ``run()``.
    """
    resp = fetch(SOURCE_URL)
    return run(resp.text)


def _record_to_proto(record: dict) -> CdcFluviewRecord:
    """Convert a parsed record dict to a CdcFluviewRecord proto message.

    Args:
        record: Dict as returned by ``run()``.

    Returns:
        Populated CdcFluviewRecord instance.
    """
    msg = CdcFluviewRecord()
    msg.week_ending_date = record["week_ending_date"]
    msg.year = record["year"]
    msg.week = record["week"]
    msg.region = record["region"]
    msg.ili_percent = record["ili_percent"]
    msg.total_patients = record["total_patients"]
    msg.ili_patients = record["ili_patients"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape FluView ILI data and upload records to BigQuery.

    Returns:
        Count of rows successfully inserted.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("cdc_fluview", messages, date_column="week_ending_date")


if __name__ == "__main__":
    main()
