"""AAR Weekly Rail Traffic scraper.

Fetches the Association of American Railroads weekly press release, which
publishes U.S. rail carloads and intermodal units by commodity group. The
press release is an HTML page containing a freeform article with embedded
tables and percentage-change text.

One AarWeeklyRailTrafficRecord is emitted per commodity group row in the
table. The week_ending_date is extracted from a paragraph near the top of
the article body. Percentage changes appear in the form "(+5.2%)" or
"(-3.1%)" and are parsed by stripping parentheses and the trailing "%".
"""

import logging
import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from protos.aar_weekly_rail_traffic_pb2 import AarWeeklyRailTrafficRecord  # type: ignore[attr-defined]
from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch

SOURCE_URL = "https://www.aar.org/news/weekly-railroad-traffic/"

_log = logging.getLogger(__name__)

_DATE_PATTERNS = [
    re.compile(
        r"week\s+ending\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"week\s+of\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        re.IGNORECASE,
    ),
]

_DATE_FORMATS = ["%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"]

_PCT_RE = re.compile(r"([+-]?\d+\.?\d*)\s*%")


def _clean(text: str) -> str:
    return text.replace("\xa0", " ").replace("–", "-").strip()


def _parse_week_ending_date(soup: BeautifulSoup) -> str:
    """Extract the week-ending date from article paragraph text.

    Scans all <p> tags for the pattern "week ending <Month> <Day>, <Year>"
    and returns an ISO-8601 date string (YYYY-MM-DD).

    Raises:
        ValueError: When no recognizable date phrase is found.
    """
    for tag in soup.find_all(["p", "h1", "h2", "h3", "div", "span"]):
        text = _clean(tag.get_text())
        for pattern in _DATE_PATTERNS:
            m = pattern.search(text)
            if m:
                raw_date = m.group(1).strip().rstrip(",")
                for fmt in _DATE_FORMATS:
                    try:
                        return datetime.strptime(raw_date, fmt).strftime("%Y-%m-%d")
                    except ValueError:
                        continue
    raise ValueError("Could not find week-ending date in HTML")


def _parse_pct(cell_text: str) -> float:
    """Parse a percentage value from text like '(+5.2%)' or '(-3.1%)'.

    Strips parentheses and extracts the numeric value including its sign.
    Returns 0.0 when no numeric pattern is found.
    """
    text = _clean(cell_text)
    m = _PCT_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return 0.0
    return 0.0


def _parse_int(cell_text: str) -> int:
    """Parse a comma-formatted integer from a table cell."""
    text = _clean(cell_text).replace(",", "").replace(" ", "")
    if not text or text in ("-", "--", "n.a.", "N.A."):
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _is_data_row(first_cell_text: str) -> bool:
    """Return False for footnote rows (first cell starts with '*' or is blank)."""
    text = first_cell_text.strip()
    return bool(text) and not text.startswith("*")


def _find_column_indices(header_rows: list[Tag]) -> dict[str, int]:
    """Map logical column names to their zero-based index from the last header row.

    Inspects the final <tr> in the thead to find columns by keyword matching.
    Returns a dict with keys: 'commodity', 'carloads', 'intermodal',
    'carloads_yoy', 'intermodal_yoy'. Missing columns default to -1.
    """
    if not header_rows:
        return {}

    last_row = header_rows[-1]
    cells = last_row.find_all(["th", "td"])

    expanded: list[str] = []
    for cell in cells:
        colspan = int(str(cell.get("colspan") or 1))
        label = _clean(cell.get_text()).lower()
        for _ in range(colspan):
            expanded.append(label)

    col_map: dict[str, int] = {
        "commodity": -1,
        "carloads": -1,
        "intermodal": -1,
        "carloads_yoy": -1,
        "intermodal_yoy": -1,
    }

    for i, label in enumerate(expanded):
        if col_map["commodity"] == -1 and ("commodity" in label or i == 0):
            col_map["commodity"] = i
        elif col_map["carloads"] == -1 and "carload" in label and "yoy" not in label and "%" not in label and "change" not in label and "year" not in label:
            col_map["carloads"] = i
        elif col_map["intermodal"] == -1 and "intermodal" in label and "yoy" not in label and "%" not in label and "change" not in label and "year" not in label:
            col_map["intermodal"] = i
        elif col_map["carloads_yoy"] == -1 and "carload" in label and ("yoy" in label or "%" in label or "change" in label or "year" in label):
            col_map["carloads_yoy"] = i
        elif col_map["intermodal_yoy"] == -1 and "intermodal" in label and ("yoy" in label or "%" in label or "change" in label or "year" in label):
            col_map["intermodal_yoy"] = i

    return col_map


def _parse_table(
    table: Tag,
    week_ending_date: str,
    source_url: str,
    fetch_ts: str,
) -> list[AarWeeklyRailTrafficRecord]:
    """Parse a single commodity table and return one record per data row.

    Handles colspan header rows by flattening them to determine logical column
    positions. Strips footnote rows (first cell blank or starts with '*').
    Parses percentage-change columns that contain values like '(+5.2%)'.
    """
    thead = table.find("thead")
    tbody = table.find("tbody")

    if not thead or not tbody:
        all_rows = table.find_all("tr")
        if len(all_rows) < 2:
            return []
        header_rows = [all_rows[0]]
        data_rows = all_rows[1:]
    else:
        header_rows = thead.find_all("tr")
        data_rows = tbody.find_all("tr")

    col_map = _find_column_indices(header_rows)

    if col_map.get("commodity", -1) == -1 or col_map.get("carloads", -1) == -1:
        col_map = {
            "commodity": 0,
            "carloads": 1,
            "intermodal": 2,
            "carloads_yoy": 4,
            "intermodal_yoy": 5,
        }

    records: list[AarWeeklyRailTrafficRecord] = []
    for row in data_rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        commodity = _clean(cells[0].get_text())
        if not _is_data_row(commodity):
            continue

        def _cell(idx: int) -> str:
            if idx < 0 or idx >= len(cells):
                return ""
            return _clean(cells[idx].get_text())

        carloads = _parse_int(_cell(col_map.get("carloads", 1)))
        intermodal = _parse_int(_cell(col_map.get("intermodal", 2)))
        carloads_yoy = _parse_pct(_cell(col_map.get("carloads_yoy", 4)))
        intermodal_yoy = _parse_pct(_cell(col_map.get("intermodal_yoy", 5)))

        rec = AarWeeklyRailTrafficRecord()
        rec.week_ending_date = week_ending_date
        rec.commodity_group = commodity
        rec.carloads = carloads
        rec.carloads_yoy_pct = carloads_yoy
        rec.intermodal_units = intermodal
        rec.intermodal_yoy_pct = intermodal_yoy
        rec.source_url = source_url
        rec.fetch_time = fetch_ts
        records.append(rec)

    return records


def run(html: str, source_url: str = SOURCE_URL) -> list[AarWeeklyRailTrafficRecord]:
    """Parse AAR weekly rail traffic HTML and return structured records.

    Locates the commodity breakdown table via BeautifulSoup, extracts the
    week-ending date from surrounding prose, and parses each data row into
    an AarWeeklyRailTrafficRecord.

    Args:
        html: Raw HTML of the AAR weekly press release page.
        source_url: URL used to populate the source_url field on each record.

    Returns:
        List of AarWeeklyRailTrafficRecord instances, one per commodity group.

    Raises:
        ValueError: When html is empty, no table is found, or no records can
            be extracted.
    """
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to AAR rail traffic parser")

    soup = BeautifulSoup(html, "lxml")

    week_ending_date = _parse_week_ending_date(soup)

    tables = soup.find_all("table")
    if not tables:
        raise ValueError("No tables found in AAR rail traffic page")

    fetch_ts = datetime.now(timezone.utc).isoformat()
    records: list[AarWeeklyRailTrafficRecord] = []
    for table in tables:
        records.extend(_parse_table(table, week_ending_date, source_url, fetch_ts))

    if not records:
        raise ValueError("No records extracted from AAR rail traffic page")

    return records


def scrape() -> list[AarWeeklyRailTrafficRecord]:
    """Fetch the AAR weekly rail traffic press release and parse it.

    Calls http_client.fetch on SOURCE_URL, sleeps 3 seconds to respect
    polite-crawl conventions, then delegates to run().

    Returns:
        List of AarWeeklyRailTrafficRecord instances.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def main() -> int:
    """Scrape AAR weekly rail traffic data and upload records to BigQuery.

    Returns:
        Count of successfully inserted rows.
    """
    records = scrape()
    return upload_rows("aar_weekly_rail_traffic", records, date_column="week_ending_date")
