"""Bureau of Economic Analysis GDP by Expenditure Component scraper.

Fetches the BEA GDP summary page and parses two embedded HTML tables:
one with quarterly percent changes (annualised) and one with quarterly
level values in billions of chained 2017 dollars.  Records are emitted
as GdpRecord proto messages covering major expenditure components:
personal consumption, gross private domestic investment, net exports,
and government consumption.

The page uses multi-row headers (year row + Roman-numeral quarter row),
parentheses to denote negative values, commas as thousands separators,
and non-breaking spaces (\\xa0) to encode component hierarchy depth.
"""

import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.bea_gdp_pb2 import GdpRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.bea.gov/data/gdp/gross-domestic-product"

_QUARTER_MAP = {"I": "Q1", "II": "Q2", "III": "Q3", "IV": "Q4"}
_QUARTER_START_MONTH = {"Q1": "01", "Q2": "04", "Q3": "07", "Q4": "10"}


def _build_col_info(thead: Tag | None) -> tuple[int, list[str | None]]:
    """Parse a two-row BEA table header into column metadata.

    The first header row contains year values (e.g. '2023', '2024') with
    colspan attributes spanning the quarters they cover; cells with rowspan>1
    are non-data columns (e.g. 'Line', 'Component').  The last header row
    contains Roman-numeral quarter labels (I–IV) for each data column.

    Args:
        thead: BeautifulSoup Tag for the <thead> element.

    Returns:
        A tuple (n_skip, labels) where n_skip is the number of prefix
        columns in each data row that do not contain numeric values
        (i.e. line number + component name), and labels is a list whose
        length equals the number of data columns, with entries like
        '2024Q1' for valid quarter columns and None for unrecognised ones.
        Returns (0, []) when thead is None or has fewer than two rows.
    """
    if thead is None:
        return 0, []
    rows = thead.find_all("tr")
    if len(rows) < 2:
        return 0, []

    year_row = rows[0]
    quarter_row = rows[-1]

    n_skip = 0
    year_seq: list[str | None] = []

    for cell in year_row.find_all(["th", "td"]):
        text = cell.get_text(strip=True)
        colspan = int(cell.get("colspan", 1))
        rowspan = int(cell.get("rowspan", 1))
        if rowspan > 1:
            n_skip += colspan
        elif re.match(r"^\d{4}$", text):
            year_seq.extend([text] * colspan)
        else:
            year_seq.extend([None] * colspan)

    quarter_seq: list[str | None] = []
    for cell in quarter_row.find_all(["th", "td"]):
        text = cell.get_text(strip=True)
        quarter_seq.append(_QUARTER_MAP.get(text))

    if not year_seq or len(year_seq) != len(quarter_seq):
        return n_skip, []

    labels: list[str | None] = [
        f"{y}{q}" if y and q else None
        for y, q in zip(year_seq, quarter_seq)
    ]
    return n_skip, labels


def _classify_table(table: Tag) -> str | None:
    """Return 'pct', 'level', or None based on the table caption or heading.

    Inspects the table's <caption> element and, if needed, the nearest
    preceding sibling element with text.  'percent change' in the text
    indicates a percent-change table; 'billion' or 'level' indicates a
    levels table.  Classification is case-insensitive.

    Args:
        table: BeautifulSoup Tag for the <table> element.

    Returns:
        'pct' for percent-change tables, 'level' for levels tables,
        or None when the table type cannot be determined.
    """
    candidates: list[str] = []
    caption = table.find("caption")
    if caption:
        candidates.append(caption.get_text().lower())

    for sibling in table.previous_siblings:
        if not hasattr(sibling, "get_text"):
            continue
        text = sibling.get_text().lower().strip()
        if text:
            candidates.append(text)
            break

    combined = " ".join(candidates)
    if "percent change" in combined or "percent" in combined:
        return "pct"
    if "billion" in combined or "level" in combined:
        return "level"
    return None


def _clean_component(text: str) -> str:
    """Strip non-breaking spaces and surrounding whitespace from a component name.

    BEA tables use \\xa0 characters to encode hierarchy depth (indentation).
    This function normalises the name to a canonical flat string.

    Args:
        text: Raw text from the component name cell.

    Returns:
        Component name with all leading/trailing whitespace and \\xa0 removed.
    """
    return text.replace("\xa0", " ").replace(" ", " ").strip()


def _clean_value(text: str) -> float | None:
    """Parse a BEA table cell value into a float.

    Handles commas used as thousands separators and parentheses used to
    represent negative values (e.g. '(1,234.5)' → -1234.5).  Returns None
    for empty cells, ellipses, dashes, and cells that cannot be parsed as
    a number.

    Args:
        text: Raw text from a data cell.

    Returns:
        Parsed float, or None when the cell is non-numeric or empty.
    """
    text = text.strip()
    if not text or text in ("...", "N/A", "—", "–", "n/a"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace(",", "").strip()
    try:
        val = float(cleaned)
        return -val if negative else val
    except ValueError:
        return None


def _period_to_date(period: str) -> str | None:
    """Convert a period string like '2024Q1' into an ISO-8601 date string.

    Maps each quarter to the first calendar day of that quarter:
    Q1 → January 1, Q2 → April 1, Q3 → July 1, Q4 → October 1.

    Args:
        period: String of the form 'YYYYQ#' (e.g. '2024Q1').

    Returns:
        ISO-8601 date string 'YYYY-MM-DD', or None for unrecognised input.
    """
    m = re.match(r"^(\d{4})(Q[1-4])$", period)
    if not m:
        return None
    year, q = m.group(1), m.group(2)
    month = _QUARTER_START_MONTH.get(q)
    if not month:
        return None
    return f"{year}-{month}-01"


def _parse_table(
    table: Tag,
) -> tuple[str | None, dict[tuple[str, str], float]]:
    """Parse a single BEA GDP table into a keyed value mapping.

    Args:
        table: BeautifulSoup Tag for the <table> element.

    Returns:
        A tuple (table_type, data) where table_type is 'pct', 'level', or
        None, and data maps (component, period) tuples to float values.
        Returns (None, {}) when the table lacks a valid two-row header or
        cannot be classified.
    """
    thead = table.find("thead")
    if thead is None:
        return None, {}

    n_skip, labels = _build_col_info(thead)
    if not labels:
        return None, {}

    table_type = _classify_table(table)
    if table_type is None:
        return None, {}

    tbody = table.find("tbody")
    if tbody is None:
        return table_type, {}

    data: dict[tuple[str, str], float] = {}
    for tr in tbody.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) < n_skip:
            continue

        component = _clean_component(cells[n_skip - 1].get_text())
        if not component:
            continue

        data_cells = cells[n_skip:]
        for i, label in enumerate(labels):
            if label is None or i >= len(data_cells):
                continue
            val = _clean_value(data_cells[i].get_text())
            if val is None:
                continue
            data[(component, label)] = val

    return table_type, data


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse a BEA GDP HTML page into GdpRecord-shaped dicts.

    Searches all tables in the page for BEA-style two-row headers.
    Tables classified as 'pct' populate pct_change_annualized; tables
    classified as 'level' populate value_billions_usd.  Results are merged
    on (component, period) key so each output record carries both fields
    where available.

    Only (component, period) pairs that appear in at least one table with
    a numeric value are emitted — pairs where all cells were empty or
    non-parseable are silently dropped.

    Args:
        html: Raw HTML string of the BEA GDP page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: period_date, component, value_billions_usd,
        pct_change_annualized, source_url.

    Raises:
        ValueError: When no records could be extracted from the page.
    """
    soup = BeautifulSoup(html, "lxml")

    pct_data: dict[tuple[str, str], float] = {}
    level_data: dict[tuple[str, str], float] = {}

    for table in soup.find_all("table"):
        table_type, data = _parse_table(table)
        if table_type == "pct":
            pct_data.update(data)
        elif table_type == "level":
            level_data.update(data)

    all_keys = set(pct_data) | set(level_data)
    records: list[dict] = []
    for component, period in sorted(all_keys):
        period_date = _period_to_date(period)
        if period_date is None:
            continue
        records.append(
            {
                "period_date": period_date,
                "component": component,
                "value_billions_usd": level_data.get((component, period), 0.0),
                "pct_change_annualized": pct_data.get((component, period), 0.0),
                "source_url": source_url,
            }
        )

    if not records:
        raise ValueError("No GDP records extracted from BEA page")

    return records


def scrape() -> list[dict]:
    """Fetch the live BEA GDP page and return parsed expenditure records.

    Delegates HTTP fetching to src.scrapers.http_client.fetch, which enforces
    the User-Agent header, a 2–5 s polite delay, and exponential backoff on
    429/5xx responses.  An additional 3-second courtesy sleep is applied
    after the fetch before parsing.

    Returns:
        Same structure as run().

    Raises:
        RuntimeError: If robots.txt disallows the URL.
        ValueError: Propagated from run() when no records are extracted.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def _record_to_proto(record: dict) -> GdpRecord:
    """Convert a record dict into a GdpRecord proto message.

    Sets fetch_time to the current UTC time via FromDatetime and pins
    schema_version to 1 so the drift detector can flag structural changes.

    Args:
        record: Dict as returned by run(), with keys period_date, component,
            value_billions_usd, pct_change_annualized, source_url.

    Returns:
        Populated GdpRecord proto instance.
    """
    msg = GdpRecord()
    msg.period_date = record["period_date"]
    msg.component = record["component"]
    msg.value_billions_usd = record["value_billions_usd"]
    msg.pct_change_annualized = record["pct_change_annualized"]
    msg.source_url = record["source_url"]
    msg.fetch_time.FromDatetime(datetime.now(timezone.utc))
    msg.schema_version = 1
    return msg


def main() -> int:
    """Scrape BEA GDP expenditure data and upload records to BigQuery.

    Calls scrape(), converts each record to a GdpRecord proto, and uploads
    via upload_rows to the 'bea_gdp' table.

    Returns:
        Count of successfully inserted rows.

    Raises:
        ValueError: When scrape() extracts zero records.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("bea_gdp", messages)


if __name__ == "__main__":
    main()
