"""USDA NASS Weekly Crop Progress and Condition scraper.

Parses the HTML tables published at SOURCE_URL, which lists weekly crop
progress (phenological stages) and condition (5-point scale) for major U.S.
field crops.  No API key required.

Each HTML table is expected to carry a <caption> that names the crop
(e.g. "CORN Progress" or "SOYBEANS Condition").  Column headers identify
the table type: Condition tables contain headers from the 5-point scale
("Very Poor" … "Excellent"); all other data-column tables are Progress
tables with phenological stage names as headers.

One record is emitted per (state, stage/condition-category) cell that
holds a numeric value.  Blank or "--" cells are silently skipped.
"""

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.usda_crop_progress_pb2 import UsdaCropProgressRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.nass.usda.gov/Charts_and_Maps/Crop_Progress_&_Condition/"

TARGET_CROPS: list[str] = sorted(
    ["CORN", "SOYBEANS", "WINTER WHEAT", "COTTON"],
    key=len,
    reverse=True,
)

CONDITION_CATEGORIES: set[str] = {"Very Poor", "Poor", "Fair", "Good", "Excellent"}

REQUIRED_FIELDS: list[str] = [
    "report_week",
    "state",
    "crop",
    "stage",
    "pct_complete",
    "condition_category",
    "pct_condition",
    "source_url",
]


def _parse_report_week(soup: BeautifulSoup) -> str | None:
    """Extract the report-week end date from a page-level heading.

    Searches all heading and paragraph tags for the pattern
    "Week Ending <Month> <DD>,? <YYYY>" and returns the date as YYYY-MM-DD.
    Returns None if no matching heading is found.
    """
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "p"]):
        text = tag.get_text(" ", strip=True)
        m = re.search(
            r"Week\s+Ending\s+(\w+\s+\d+,?\s*\d{4})", text, re.IGNORECASE
        )
        if not m:
            continue
        date_str = m.group(1).replace(",", "").strip()
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def _get_crop_name(table) -> str | None:
    """Return the recognised crop name for a table, or None if unrecognised.

    Checks the table's <caption> first, then the first header row.  The
    crop names in TARGET_CROPS are tried in descending length order so that
    "WINTER WHEAT" is matched before a hypothetical "WHEAT" substring.
    """
    candidates: list[str] = []

    caption = table.find("caption")
    if caption:
        candidates.append(caption.get_text(" ", strip=True).upper())

    first_row = table.find("tr")
    if first_row:
        for th in first_row.find_all("th"):
            candidates.append(th.get_text(" ", strip=True).upper())

    for text in candidates:
        for crop in TARGET_CROPS:
            if crop in text:
                return crop
    return None


def _is_condition_table(column_names: list[str]) -> bool:
    """Return True when the column names include any USDA condition-scale label."""
    return bool(set(column_names) & CONDITION_CATEGORIES)


def _parse_table(table, crop: str, report_week: str) -> list[dict]:
    """Parse one crop HTML table into a flat list of record dicts.

    Locates the header row (first row whose first cell is "State" or
    "States"), reads column names, then fans out each data row into one
    record per non-blank numeric cell.

    Progress tables: each column name becomes `stage`; value → `pct_complete`.
    Condition tables: each column name becomes `condition_category`;
    value → `pct_condition`.

    Rows where the state cell is empty, and cells whose text is "" or "--",
    are skipped.

    Args:
        table: BeautifulSoup Tag for the <table> element.
        crop: Normalised crop name (e.g. "CORN").
        report_week: Report week end date as YYYY-MM-DD.

    Returns:
        List of record dicts with all REQUIRED_FIELDS keys.
    """
    rows = table.find_all("tr")

    header_idx: int | None = None
    for i, row in enumerate(rows):
        cells = row.find_all(["th", "td"])
        if cells and cells[0].get_text(strip=True).lower() in ("state", "states"):
            header_idx = i
            break

    if header_idx is None:
        return []

    header_cells = rows[header_idx].find_all(["th", "td"])
    headers = [c.get_text(strip=True) for c in header_cells]
    if len(headers) < 2:
        return []

    col_names = headers[1:]
    is_condition = _is_condition_table(col_names)

    records: list[dict] = []
    for row in rows[header_idx + 1 :]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        state = cells[0].get_text(strip=True)
        if not state:
            continue

        for i, col_name in enumerate(col_names):
            col_idx = i + 1
            if col_idx >= len(cells):
                break

            raw = cells[col_idx].get_text(strip=True)
            if not raw or raw == "--":
                continue

            try:
                pct = float(raw)
            except ValueError:
                continue

            if is_condition:
                records.append(
                    {
                        "report_week": report_week,
                        "state": state,
                        "crop": crop,
                        "stage": "",
                        "pct_complete": 0.0,
                        "condition_category": col_name,
                        "pct_condition": pct,
                        "source_url": SOURCE_URL,
                    }
                )
            else:
                records.append(
                    {
                        "report_week": report_week,
                        "state": state,
                        "crop": crop,
                        "stage": col_name,
                        "pct_complete": pct,
                        "condition_category": "",
                        "pct_condition": 0.0,
                        "source_url": SOURCE_URL,
                    }
                )

    return records


def run(html: str) -> list[dict]:
    """Parse USDA NASS Crop Progress HTML into a list of per-cell records.

    Returns an empty list when the report-week heading cannot be found or
    when no recognised crop tables are present.

    Args:
        html: Raw HTML string of the USDA NASS Crop Progress & Condition page.

    Returns:
        List of dicts; each dict has keys matching REQUIRED_FIELDS.
        fetch_time is intentionally absent — callers that need it add it
        after parsing (see _record_to_proto).
    """
    soup = BeautifulSoup(html, "lxml")

    report_week = _parse_report_week(soup)
    if not report_week:
        return []

    records: list[dict] = []
    for table in soup.find_all("table"):
        crop = _get_crop_name(table)
        if crop is None:
            continue
        records.extend(_parse_table(table, crop, report_week))

    return records


def scrape() -> list[dict]:
    """Fetch the live USDA NASS Crop Progress page and return parsed records.

    Delegates HTTP fetching to ``src.http_client.fetch``, which enforces
    robots.txt compliance, a polite 2–5 s delay, and exponential backoff on
    transient errors.

    Returns:
        Same structure as ``run()``.
    """
    resp = fetch(SOURCE_URL)
    return run(resp.text)


def _record_to_proto(record: dict) -> UsdaCropProgressRecord:
    msg = UsdaCropProgressRecord()
    msg.report_week = record["report_week"]
    msg.state = record["state"]
    msg.crop = record["crop"]
    msg.stage = record["stage"]
    msg.pct_complete = record["pct_complete"]
    msg.condition_category = record["condition_category"]
    msg.pct_condition = record["pct_condition"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape USDA NASS Crop Progress data and upload records to BigQuery.

    Calls scrape(), converts each record to a UsdaCropProgressRecord proto,
    and uploads via upload_rows.  Returns the count of inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("usda_crop_progress", messages, date_column="report_week")


if __name__ == "__main__":
    main()
