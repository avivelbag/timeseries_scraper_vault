"""BLS JOLTS (Job Openings and Labor Turnover Survey) HTML-table scraper.

Fetches three JOLTS news-release HTML tables (Job Openings t01, Hires t04,
Total Separations t07) and parses the two-row thead structure into
BlsJoltsRecord dataclass stubs.  One record is emitted per (period, industry)
pair per table.  A 3-second courtesy delay is applied between each fetch per
BLS rate-limit guidance.
"""

import re
import time
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.bls_jolts_pb2 import BlsJoltsRecord  # type: ignore[attr-defined]

_T01_URL = "https://www.bls.gov/news.release/jolts.t01.htm"
_T04_URL = "https://www.bls.gov/news.release/jolts.t04.htm"
_T07_URL = "https://www.bls.gov/news.release/jolts.t07.htm"

JOLTS_TABLES: list[tuple[str, str]] = [
    (_T01_URL, "job_openings"),
    (_T04_URL, "hires"),
    (_T07_URL, "separations"),
]

_MONTH_ABBR: dict[str, str] = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

_PERIOD_RE = re.compile(r"([A-Za-z]+)\s+(\d{4})")
_NON_NUMERIC = re.compile(r"[^\d.]")


def _parse_period(text: str) -> str | None:
    """Convert a JOLTS period cell value to ISO year-month format.

    Strips preliminary markers like "(p)" before matching.  Accepts inputs
    such as "Nov 2024", "Jan 2025(p)", "Mar 2025 (p)".

    Args:
        text: Raw text from the period column cell.

    Returns:
        ISO year-month string "YYYY-MM", or None when the text does not
        contain a recognisable month-year pair.
    """
    m = _PERIOD_RE.search(text)
    if not m:
        return None
    month_num = _MONTH_ABBR.get(m.group(1).lower()[:3])
    if not month_num:
        return None
    return f"{m.group(2)}-{month_num}"


def _clean_header_text(text: str) -> str:
    """Strip parenthetical footnote markers and collapse whitespace from a header cell."""
    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", text)
    return " ".join(cleaned.split())


def _parse_numeric(text: str) -> float | None:
    """Parse a numeric table cell value, stripping commas and non-numeric characters.

    Args:
        text: Raw cell text such as "7,999", "4.7", or "8,355(p)".

    Returns:
        Float value, or None when the cell carries no parseable number.
    """
    cleaned = _NON_NUMERIC.sub("", text.replace(",", "")).strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _build_column_map(thead: Any) -> dict[int, tuple[str, str]]:
    """Build a column-index → (industry, col_type) map from a two-row JOLTS thead.

    Row 0 contains industry category headers with colspan attributes.  The
    "Period" cell uses rowspan=2 and is skipped during span accounting.
    Row 1 contains alternating "Level" and "Rate" labels for each industry.

    Args:
        thead: BeautifulSoup Tag for the <thead> element.

    Returns:
        Dict mapping absolute column index to a (industry_name, col_type) pair
        where col_type is "level" or "rate".  Returns an empty dict when the
        header does not match the expected two-row structure.
    """
    rows = thead.find_all("tr")
    if len(rows) < 2:
        return {}

    # Row 0: record (industry_name, abs_start_col, colspan) for non-Period cells.
    # The Period cell is identified by rowspan > 1.
    industry_spans: list[tuple[str, int, int]] = []
    abs_col = 0
    for th in rows[0].find_all(["th", "td"]):
        colspan = int(th.get("colspan", 1))
        rowspan = int(th.get("rowspan", 1))
        if rowspan > 1:
            abs_col += colspan
            continue
        name = _clean_header_text(th.get_text(strip=True))
        industry_spans.append((name, abs_col, colspan))
        abs_col += colspan

    industry_by_abs_col: dict[int, str] = {}
    for name, start, span in industry_spans:
        for i in range(span):
            industry_by_abs_col[start + i] = name

    # Row 1 cells start at absolute column 1 (column 0 is Period, spanned from row 0).
    col_map: dict[int, tuple[str, str]] = {}
    for row1_idx, th in enumerate(rows[1].find_all(["th", "td"])):
        abs_col_here = row1_idx + 1
        label = th.get_text(separator=" ", strip=True).lower()
        if "level" in label:
            col_type = "level"
        elif "rate" in label:
            col_type = "rate"
        else:
            continue
        industry = industry_by_abs_col.get(abs_col_here)
        if industry:
            col_map[abs_col_here] = (industry, col_type)

    return col_map


def parse_jolts_table(html: str, source_url: str, data_type: str) -> list[dict]:
    """Parse a BLS JOLTS news-release HTML table into a list of record dicts.

    Locates the first <table> whose <thead> yields a valid two-row column map,
    then iterates <tbody> rows.  One record is emitted per (period, industry)
    pair; rows with unparseable periods or missing level values are skipped.

    Args:
        html: Raw HTML string of a BLS JOLTS table page.
        source_url: URL the page was fetched from, stored in each record.
        data_type: Label for the table type; one of "job_openings", "hires",
            "separations".

    Returns:
        List of dicts with keys: series_id, period, data_type, industry,
        level_thousands, rate_pct, source_url.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    for table in soup.find_all("table"):
        thead = table.find("thead")
        if thead is None:
            continue
        col_map = _build_column_map(thead)
        if not col_map:
            continue

        # Group column map entries by industry → {col_type: abs_col_index}.
        industry_cols: dict[str, dict[str, int]] = {}
        for col_idx, (industry, col_type) in col_map.items():
            industry_cols.setdefault(industry, {})[col_type] = col_idx

        tbody = table.find("tbody")
        if tbody is None:
            continue

        for tr in tbody.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            for cell in cells:
                for sup in cell.find_all("sup"):
                    sup.decompose()
            cell_texts = [c.get_text(strip=True) for c in cells]
            if not cell_texts:
                continue

            period = _parse_period(cell_texts[0])
            if period is None:
                continue

            for industry, cols in industry_cols.items():
                level_col = cols.get("level")
                if level_col is None or level_col >= len(cell_texts):
                    continue
                level = _parse_numeric(cell_texts[level_col])
                if level is None:
                    continue

                rate_col = cols.get("rate")
                rate = 0.0
                if rate_col is not None and rate_col < len(cell_texts):
                    parsed_rate = _parse_numeric(cell_texts[rate_col])
                    if parsed_rate is not None:
                        rate = parsed_rate

                series_id = f"{data_type}_{industry.lower().replace(' ', '_')}"
                records.append(
                    {
                        "series_id": series_id,
                        "period": period,
                        "data_type": data_type,
                        "industry": industry,
                        "level_thousands": level,
                        "rate_pct": rate,
                        "source_url": source_url,
                    }
                )

        if records:
            break

    return records


def _record_to_proto(record: dict) -> BlsJoltsRecord:
    msg = BlsJoltsRecord()
    msg.series_id = record["series_id"]
    msg.period = record["period"]
    msg.data_type = record["data_type"]
    msg.industry = record["industry"]
    msg.level_thousands = record["level_thousands"]
    msg.rate_pct = record["rate_pct"]
    msg.source_url = record["source_url"]
    msg.fetch_time.FromDatetime(datetime.now(timezone.utc))
    return msg


def fetch_jolts() -> int:
    """Fetch all JOLTS tables, parse records, and upload to BigQuery.

    Fetches job openings (t01.htm), hires (t04.htm), and total separations
    (t07.htm) in sequence.  Applies a 3-second courtesy delay after each
    request per BLS rate-limit guidance.  All records from all three tables
    are merged and uploaded in a single call to upload_rows.

    Returns:
        Count of rows successfully inserted into BigQuery.
    """
    all_records: list[dict] = []
    for url, data_type in JOLTS_TABLES:
        resp = fetch(url)
        time.sleep(3)
        records = parse_jolts_table(resp.text, url, data_type)
        all_records.extend(records)

    messages = [_record_to_proto(r) for r in all_records]
    return upload_rows("bls_jolts", messages)


if __name__ == "__main__":
    fetch_jolts()
