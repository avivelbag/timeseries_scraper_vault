import logging
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup, Tag

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.cftc_cot_pb2 import CotRecord  # type: ignore[attr-defined]
import requests

SOURCE_URL = "https://www.cftc.gov/dea/futures/deacmesf.htm"

HISTORICAL_URL_TEMPLATE = "https://www.cftc.gov/dea/futures/deacmesf{year}.htm"

_COMMODITY_RE = re.compile(r"^([A-Z][A-Z0-9 ,.()/&'-]+?)\s{3,}(\d{6})\s*$")

_ALL_LINE_RE = re.compile(r"^\s+ALL\s")

_DATE_RE = re.compile(r"As of \w+,\s+(\w+ \d+,\s*\d{4})", re.IGNORECASE)

_log = logging.getLogger(__name__)


def _extract_report_date(soup: BeautifulSoup) -> str:
    candidates = [tag.get_text(" ", strip=True) for tag in soup.find_all("h2")]
    title_tag = soup.find("title")
    if title_tag:
        candidates.append(title_tag.get_text(" ", strip=True))

    for text in candidates:
        m = _DATE_RE.search(text)
        if not m:
            continue
        date_str = re.sub(r"\s+", " ", m.group(1)).strip()
        try:
            return datetime.strptime(date_str, "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _find_pre_with_data(soup: BeautifulSoup) -> Tag | None:
    for pre in soup.find_all("pre"):
        if any(_COMMODITY_RE.match(line) for line in pre.get_text().splitlines()):
            return pre
    return None


def parse_html(html: str, source_url: str = SOURCE_URL) -> list[CotRecord]:
    soup = BeautifulSoup(html, "lxml")
    report_date = _extract_report_date(soup)
    fetch_time = datetime.now(timezone.utc).isoformat()

    pre = _find_pre_with_data(soup)
    if not pre:
        return []

    lines = pre.get_text().splitlines()
    records: list[CotRecord] = []
    pending_name: str | None = None
    pending_code: str | None = None

    for line in lines:
        commodity_match = _COMMODITY_RE.match(line)
        if commodity_match:
            pending_name = commodity_match.group(1).strip()
            pending_code = commodity_match.group(2).strip()
            continue

        if _ALL_LINE_RE.match(line) and pending_name is not None:
            nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", line)]
            if len(nums) >= 9:
                records.append(
                    CotRecord(
                        report_date=report_date,
                        commodity_name=pending_name,
                        cftc_contract_market_code=pending_code or "",
                        noncommercial_long=nums[0],
                        noncommercial_short=nums[1],
                        # nums[2] is non-commercial spreads; excluded from the schema
                        commercial_long=nums[3],
                        commercial_short=nums[4],
                        total_reportable_long=nums[5],
                        total_reportable_short=nums[6],
                        nonreportable_long=nums[7],
                        nonreportable_short=nums[8],
                        source_url=source_url,
                        fetch_time=fetch_time,
                    )
                )
            pending_name = None
            pending_code = None

    return records


def scrape() -> list[CotRecord]:
    resp = fetch(SOURCE_URL)
    return parse_html(resp.text)


def scrape_year(year: int) -> list[CotRecord]:
    url = HISTORICAL_URL_TEMPLATE.format(year=year)
    resp = fetch(url)
    return parse_html(resp.text, source_url=url)


def scrape_range(start_year: int, end_year: int) -> list[CotRecord]:
    all_records: list[CotRecord] = []
    for year in range(start_year, end_year + 1):
        try:
            all_records.extend(scrape_year(year))
        except requests.RequestException as exc:
            _log.warning("skipping year %d: %s", year, exc)
    return all_records


def main() -> int:
    records = scrape()
    return upload_rows("cftc_cot", records, date_column="report_date")


if __name__ == "__main__":
    main()
