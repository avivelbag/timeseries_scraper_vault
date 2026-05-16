"""Federal Reserve Z.1 Financial Accounts of the United States scraper.

Fetches the Z.1 statistical release HTML page, which publishes quarterly
household net worth, total nonfinancial debt, and related sector balance-
sheet totals. Targets tables whose caption or nearby heading contains
"household", "net worth", or "nonfinancial debt" (case-insensitive).

One FedZ1Record is emitted per (series_name, period_date) pair. Period
dates are normalized from Fed column-header format ("2024:Q3") to
"2024-Q3". Values marked "r" (revised) or "p" (preliminary) have the
trailing letter stripped before parsing. Parenthetical negatives such as
"(1,234.5)" are converted to -1234.5.
"""

import logging
import re
import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from protos.fed_z1_financial_accounts_pb2 import FedZ1Record  # type: ignore[attr-defined]
from src.scrapers.http_client import fetch

SOURCE_URL = "https://www.federalreserve.gov/releases/z1/"

_log = logging.getLogger(__name__)

_PERIOD_RE = re.compile(r"^(\d{4})[:\-]Q([1-4])$", re.IGNORECASE)
_REVISED_PRELIM_RE = re.compile(r"[rp]$", re.IGNORECASE)
_PARENS_RE = re.compile(r"^\(([0-9,.]+)\)$")

_TARGET_KEYWORDS = ("household", "net worth", "nonfinancial debt")


def _clean(text: str) -> str:
    return text.replace("\xa0", " ").strip()


def _normalize_period(raw: str) -> str | None:
    m = _PERIOD_RE.match(_clean(raw))
    if m:
        return f"{m.group(1)}-Q{m.group(2)}"
    return None


def _parse_value(raw: str) -> float | None:
    text = _clean(raw)
    text = _REVISED_PRELIM_RE.sub("", text)
    text = text.replace("$", "").replace(",", "").strip()

    if text in ("", "n.a.", "na", "NA", "--", "-", "N.A.", "..."):
        return None

    m = _PARENS_RE.match(text)
    if m:
        try:
            return -float(m.group(1).replace(",", ""))
        except ValueError:
            return None

    try:
        return float(text)
    except ValueError:
        _log.warning("Cannot parse Z.1 cell value %r as float", raw)
        return None


def _heading_matches(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in _TARGET_KEYWORDS)


def _find_target_tables(soup: BeautifulSoup) -> list[Tag]:
    result: list[Tag] = []
    for table in soup.find_all("table"):
        caption = table.find("caption")
        if caption and _heading_matches(caption.get_text()):
            result.append(table)
            continue

        prev = table.find_previous_sibling(["h2", "h3", "h4"])
        if prev and _heading_matches(prev.get_text()):
            result.append(table)
    return result


def _parse_table(table: Tag, source_url: str, fetch_ts: str) -> list[FedZ1Record]:
    rows = table.find_all("tr")
    if not rows:
        return []

    header_cells = rows[0].find_all(["th", "td"])
    period_cols: list[tuple[int, str]] = []
    for i, cell in enumerate(header_cells):
        if i == 0:
            continue
        period = _normalize_period(cell.get_text())
        if period:
            period_cols.append((i, period))

    if not period_cols:
        return []

    records: list[FedZ1Record] = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        series_name = _clean(cells[0].get_text())
        if not series_name:
            continue

        for col_idx, period_date in period_cols:
            if col_idx >= len(cells):
                continue
            value = _parse_value(_clean(cells[col_idx].get_text()))
            if value is None:
                continue

            rec = FedZ1Record()
            rec.series_name = series_name
            rec.period_date = period_date
            rec.value_billions_usd = value
            rec.source_url = source_url
            rec.fetch_time = fetch_ts
            records.append(rec)

    return records


def run(html: str, source_url: str = SOURCE_URL) -> list[FedZ1Record]:
    if not html or not html.strip():
        raise ValueError("Empty HTML provided to Z.1 parser")

    soup = BeautifulSoup(html, "lxml")
    tables = _find_target_tables(soup)

    if not tables:
        tables = list(soup.find_all("table"))

    if not tables:
        raise ValueError("No tables found in Z.1 page")

    fetch_ts = datetime.now(timezone.utc).isoformat()
    records: list[FedZ1Record] = []
    for table in tables:
        records.extend(_parse_table(table, source_url, fetch_ts))

    if not records:
        raise ValueError("No records extracted from Z.1 page")

    return records


def scrape() -> list[FedZ1Record]:
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)
