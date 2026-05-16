import io
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

import openpyxl
from bs4 import BeautifulSoup

from protos.baker_hughes_rig_count_pb2 import RigCountRecord  # type: ignore[attr-defined]
from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch

SOURCE_URL = "https://rigcount.bakerhughes.com/na-rig-count"

_REGION_MAP: dict[str, str] = {
    "us": "us",
    "u.s.": "us",
    "united states": "us",
    "can": "canada",
    "canada": "canada",
}

_DRILL_TYPE_MAP: dict[str, str] = {
    "oil": "oil",
    "gas": "gas",
    "misc": "misc",
    "miscellaneous": "misc",
}

REQUIRED_FIELDS: list[str] = [
    "report_date",
    "region",
    "drill_type",
    "rig_count",
    "week_over_week_change",
    "year_ago_count",
    "source_url",
    "fetch_time",
]

_EXPECTED_HEADERS: frozenset[str] = frozenset({"publishdate", "location", "drillfor", "count"})


def _normalise_region(raw: str) -> Optional[str]:
    return _REGION_MAP.get(raw.strip().lower())


def _normalise_drill_type(raw: str) -> Optional[str]:
    return _DRILL_TYPE_MAP.get(raw.strip().lower())


def _parse_date(raw: object) -> Optional[str]:
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, str):
        raw = raw.strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return None


def _safe_int(val: object) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(str(val)))
    except (TypeError, ValueError):
        return None


def _find_excel_url(html: str, page_url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("a", href=True):
        href = str(tag["href"])
        lower = href.lower()
        if lower.endswith(".xlsx") or lower.endswith(".xls"):
            if href.startswith("http://") or href.startswith("https://"):
                return href
            return urljoin(page_url, href)
    raise ValueError(f"No Excel download link found on {page_url}")


def parse_workbook(wb: openpyxl.Workbook, source_url: str) -> list[RigCountRecord]:
    sheet = None
    for ws in wb.worksheets:
        if "north america" in ws.title.lower():
            sheet = ws
            break
    if sheet is None:
        titles = [ws.title for ws in wb.worksheets]
        raise ValueError(f"No 'North America' sheet found; available: {titles}")

    fetch_time = datetime.now(timezone.utc).isoformat()
    all_rows = list(sheet.iter_rows(values_only=True))
    if not all_rows:
        return []

    header_idx: Optional[int] = None
    col_map: dict[str, int] = {}
    for i, row in enumerate(all_rows):
        lower_cells = {
            str(c).strip().lower() if c is not None else ""
            for c in row
        }
        if _EXPECTED_HEADERS.issubset(lower_cells):
            header_idx = i
            for j, cell in enumerate(row):
                if cell is not None:
                    col_map[str(cell).strip().lower()] = j
            break

    if header_idx is None:
        raise ValueError(
            f"Required columns not found in sheet '{sheet.title}'; "
            f"expected {sorted(_EXPECTED_HEADERS)}"
        )

    date_col = col_map["publishdate"]
    location_col = col_map["location"]
    drillfor_col = col_map["drillfor"]
    count_col = col_map["count"]
    priorweek_col = col_map.get("priorweek")
    yearago_col = col_map.get("yearago")

    records: list[RigCountRecord] = []
    for row in all_rows[header_idx + 1:]:
        if not row or row[date_col] is None:
            continue

        report_date = _parse_date(row[date_col])
        if report_date is None:
            continue

        if row[location_col] is None:
            continue
        region = _normalise_region(str(row[location_col]))
        if region is None:
            continue

        if row[drillfor_col] is None:
            continue
        drill_type = _normalise_drill_type(str(row[drillfor_col]))
        if drill_type is None:
            continue

        count = _safe_int(row[count_col])
        if count is None:
            continue

        prior = _safe_int(row[priorweek_col]) if priorweek_col is not None else 0
        if prior is None:
            prior = 0

        year_ago = _safe_int(row[yearago_col]) if yearago_col is not None else 0
        if year_ago is None:
            year_ago = 0

        records.append(
            RigCountRecord(
                report_date=report_date,
                region=region,
                drill_type=drill_type,
                rig_count=count,
                week_over_week_change=count - prior,
                year_ago_count=year_ago,
                source_url=source_url,
                fetch_time=fetch_time,
            )
        )

    return records


def scrape() -> list[RigCountRecord]:
    page_resp = fetch(SOURCE_URL, min_delay=3.0, max_delay=6.0)
    excel_url = _find_excel_url(page_resp.text, SOURCE_URL)

    excel_resp = fetch(excel_url, min_delay=3.0, max_delay=6.0, stream=True)
    buf = io.BytesIO()
    for chunk in excel_resp.iter_content(chunk_size=65536):
        if chunk:
            buf.write(chunk)
    buf.seek(0)

    wb = openpyxl.load_workbook(buf, read_only=True, data_only=True)
    return parse_workbook(wb, excel_url)


def main() -> int:
    records = scrape()
    return upload_rows("baker_hughes_rig_count", records, date_column="report_date")


if __name__ == "__main__":
    main()
