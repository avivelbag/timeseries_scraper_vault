"""EIA weekly underground natural gas storage scraper.

Parses the multi-level HTML table at SOURCE_URL, which publishes weekly
natural gas storage levels in billion cubic feet (Bcf) for EIA reporting
regions. The table uses a two-row header: region names with colspan attributes
in the first row, and sub-column labels (Current, Year Ago, 5-Yr Avg) in the
second row. One record is emitted per region per report week.
"""

import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.eia_natural_gas_storage_pb2 import EiaNaturalGasStorageRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.eia.gov/dnav/ng/hist/nw2_epg0_swo_r48_bcfw.htm"

_SUB_COL_MAP: dict[str, str] = {
    "current": "storage_bcf",
    "year ago": "year_ago_bcf",
    "5-yr avg": "five_year_avg_bcf",
    "5-year avg": "five_year_avg_bcf",
    "5 yr avg": "five_year_avg_bcf",
    "5 year avg": "five_year_avg_bcf",
}


def _build_column_map(table) -> dict[int, tuple[str, str]]:
    """Build a column-index to (region, field_name) mapping from the two-row header.

    Reads the first header row for region names (cells with colspan > 1) and
    the second header row for sub-column labels. The date column (identified by
    rowspan > 1 in the first header row) is excluded from the mapping, so all
    returned indices are 1-based relative to the full row cell list.

    Args:
        table: BeautifulSoup Tag for the <table> element.

    Returns:
        Dict mapping integer column index to a (region_name, field_name) tuple.
        field_name is one of: storage_bcf, year_ago_bcf, five_year_avg_bcf.

    Raises:
        ValueError: When the table has fewer than 2 header rows.
    """
    thead = table.find("thead") or table
    rows = thead.find_all("tr")

    if len(rows) < 2:
        raise ValueError("Expected at least 2 header rows in EIA storage table")

    header_row1 = rows[0].find_all(["th", "td"])
    header_row2 = rows[1].find_all(["th", "td"])

    region_groups: list[tuple[str, int]] = []
    for cell in header_row1:
        rowspan = int(cell.get("rowspan", 1))
        if rowspan > 1:
            continue
        colspan = int(cell.get("colspan", 1))
        region_name = cell.get_text(strip=True)
        region_groups.append((region_name, colspan))

    sub_col_texts = [cell.get_text(strip=True).lower() for cell in header_row2]

    column_map: dict[int, tuple[str, str]] = {}
    col_idx = 1
    sub_col_pos = 0
    for region, n_subcols in region_groups:
        for _ in range(n_subcols):
            if sub_col_pos < len(sub_col_texts):
                sub_col_text = sub_col_texts[sub_col_pos]
                field_name = _SUB_COL_MAP.get(sub_col_text, sub_col_text)
                column_map[col_idx] = (region, field_name)
                sub_col_pos += 1
            col_idx += 1

    return column_map


def _parse_date(raw: str) -> str:
    """Convert a raw date string from the EIA table to ISO-8601 YYYY-MM-DD format.

    Tries multiple date formats that EIA pages are known to use. Whitespace is
    stripped before parsing.

    Args:
        raw: Raw date string from the table cell.

    Returns:
        ISO-8601 date string in YYYY-MM-DD format.

    Raises:
        ValueError: When raw cannot be parsed with any known format.
    """
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {raw!r}")


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse EIA weekly natural gas storage HTML into per-region-per-week records.

    Locates the first <table> element, builds a column map from the two-row
    header, then iterates over data rows. For each row, values are collected
    per region and one record is emitted per (region, week) pair. Cells with
    missing or non-numeric values ("--", "NA") are silently skipped; rows
    whose date cell cannot be parsed are also skipped.

    Args:
        html: Raw HTML string of the EIA weekly natural gas storage page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: region, storage_bcf, year_ago_bcf,
        five_year_avg_bcf, report_date, source_url.

    Raises:
        ValueError: When no table is found or no records could be extracted.
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        raise ValueError("No table found in EIA natural gas storage page")

    column_map = _build_column_map(table)
    if not column_map:
        raise ValueError("Could not build column map from EIA table headers")

    tbody = table.find("tbody")
    row_source = tbody if tbody is not None else table

    records: list[dict] = []
    for tr in row_source.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue

        raw_date = cells[0].get_text(strip=True)
        try:
            report_date = _parse_date(raw_date)
        except ValueError:
            continue

        region_data: dict[str, dict[str, float]] = {}
        for col_idx, (region, field_name) in column_map.items():
            if col_idx >= len(cells):
                continue
            raw_val = cells[col_idx].get_text(strip=True).replace(",", "")
            if raw_val in ("", "--", "NA", "N/A"):
                continue
            try:
                value = float(raw_val)
            except ValueError:
                continue
            if region not in region_data:
                region_data[region] = {}
            region_data[region][field_name] = value

        for region, fields in region_data.items():
            if not fields:
                continue
            records.append(
                {
                    "region": region,
                    "storage_bcf": fields.get("storage_bcf", 0.0),
                    "year_ago_bcf": fields.get("year_ago_bcf", 0.0),
                    "five_year_avg_bcf": fields.get("five_year_avg_bcf", 0.0),
                    "report_date": report_date,
                    "source_url": source_url,
                }
            )

    if not records:
        raise ValueError("No records extracted from EIA natural gas storage page")

    return records


def scrape() -> list[dict]:
    """Fetch the live EIA weekly natural gas storage page and return parsed records.

    Delegates HTTP fetching to src.scrapers.http_client.fetch, which enforces
    robots.txt compliance, a polite 2-5 s delay, and exponential backoff on
    429/5xx responses. An additional 3-second courtesy sleep is applied after
    the response is received.

    Returns:
        Same structure as run().

    Raises:
        ValueError: Propagated from run() when no records are extracted.
        RuntimeError: When robots.txt disallows the target URL.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def backfill(start_date: str, end_date: str) -> list[dict]:
    """Return historical EIA storage records within a closed date range.

    Fetches the full historical table via scrape() (a single HTTP request that
    returns all available weeks) and filters to records whose report_date falls
    within [start_date, end_date]. ISO-8601 lexicographic comparison is used,
    so YYYY-MM-DD string ordering matches chronological ordering.

    Args:
        start_date: ISO-8601 date string (YYYY-MM-DD), inclusive lower bound.
        end_date: ISO-8601 date string (YYYY-MM-DD), inclusive upper bound.

    Returns:
        Filtered list of record dicts in the same format as run().

    Raises:
        ValueError: When start_date > end_date or no records fall in the range.
        RuntimeError: Propagated from scrape() when robots.txt disallows SOURCE_URL.
    """
    if start_date > end_date:
        raise ValueError(
            f"start_date {start_date!r} must be <= end_date {end_date!r}"
        )
    records = scrape()
    filtered = [r for r in records if start_date <= r["report_date"] <= end_date]
    if not filtered:
        raise ValueError(
            f"No records found in date range [{start_date}, {end_date}]"
        )
    return filtered


def _record_to_proto(record: dict) -> EiaNaturalGasStorageRecord:
    """Convert a parsed record dict to an EiaNaturalGasStorageRecord stub.

    Args:
        record: Dict as returned by run().

    Returns:
        Populated EiaNaturalGasStorageRecord dataclass instance with fetch_time
        set to the current UTC time in ISO-8601 format.
    """
    msg = EiaNaturalGasStorageRecord()
    msg.region = record["region"]
    msg.storage_bcf = record["storage_bcf"]
    msg.year_ago_bcf = record["year_ago_bcf"]
    msg.five_year_avg_bcf = record["five_year_avg_bcf"]
    msg.report_date = record["report_date"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape EIA weekly natural gas storage data and upload records to BigQuery.

    Calls scrape(), converts each record to an EiaNaturalGasStorageRecord proto
    stub, and uploads via upload_rows with report_date as the dedup column.

    Returns:
        Count of successfully inserted rows.

    Raises:
        ValueError: When scrape() extracts zero records.
        KeyError: When BQ_PROJECT or BQ_DATASET environment variables are unset.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("eia_natural_gas_storage", messages, date_column="report_date")


if __name__ == "__main__":
    main()
