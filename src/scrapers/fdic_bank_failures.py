"""FDIC Failed Bank List scraper.

Fetches the FDIC historical bank failures table and parses each row into a
structured record. The source page contains one row per failed bank since 2000,
with columns: Bank Name, City, State, Cert #, Acquiring Institution, Closing
Date, Fund.

The page has no assets/deposits/loss figures in its HTML table; those proto
fields are left unset. The canonical dedup key is (cert, failure_date).
"""

import time
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.fdic_bank_failures_pb2 import FdicBankFailureRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.fdic.gov/bank/individual/failed/banklist.html"

_COL_BANK_NAME = "bank name"
_COL_CITY = "city"
_COL_STATE = "state"
_COL_CERT = "cert"
_COL_CLOSING_DATE = "closing date"


def _parse_date(raw: str) -> str:
    """Convert MM/DD/YYYY closing date to ISO-8601 (YYYY-MM-DD).

    Args:
        raw: Date string from the FDIC table, e.g. '03/10/2023'.

    Returns:
        ISO-8601 date string, e.g. '2023-03-10'.

    Raises:
        ValueError: When raw cannot be parsed as MM/DD/YYYY.
    """
    return datetime.strptime(raw.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")


def _parse_cert(raw: str) -> int:
    """Parse the FDIC certificate number to an integer.

    Args:
        raw: Certificate number cell text, e.g. '24735'.

    Returns:
        Integer certificate number.

    Raises:
        ValueError: When raw cannot be converted to an integer.
    """
    return int(raw.strip().replace(",", ""))


def run(html: str, source_url: str = SOURCE_URL) -> list[dict]:
    """Parse the FDIC failed bank list HTML into per-bank records.

    Locates the first <table> element (expected to be id='table') and maps
    column positions from the header row. Rows with unparseable cert numbers
    or closing dates are silently skipped. The monetary proto fields
    (approx_assets_usd_millions, approx_deposits_usd_millions,
    estimated_loss_usd_millions) are absent from the source HTML table and are
    not populated here.

    Args:
        html: Raw HTML string of the FDIC failed bank list page.
        source_url: URL the page was fetched from, stored in each record.

    Returns:
        List of dicts with keys: cert, institution_name, city, state,
        failure_date, source_url.

    Raises:
        ValueError: When no records could be extracted from the page.
    """
    soup = BeautifulSoup(html, "lxml")

    table = soup.find("table", id="table") or soup.find("table")
    if table is None:
        raise ValueError("No table found in FDIC bank failures page")

    header_row = table.find("tr")
    if header_row is None:
        raise ValueError("No header row found in FDIC bank failures table")

    headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]

    col_map: dict[str, int] = {}
    for i, h in enumerate(headers):
        if _COL_BANK_NAME in h:
            col_map["institution_name"] = i
        elif _COL_CITY in h:
            col_map["city"] = i
        elif _COL_STATE in h:
            col_map["state"] = i
        elif _COL_CERT in h:
            col_map["cert"] = i
        elif _COL_CLOSING_DATE in h:
            col_map["failure_date"] = i

    required = {"institution_name", "city", "state", "cert", "failure_date"}
    missing = required - col_map.keys()
    if missing:
        raise ValueError(f"Missing expected columns in FDIC table: {missing}")

    records: list[dict] = []
    tbody = table.find("tbody")
    row_source = tbody if tbody is not None else table

    for tr in row_source.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue

        if len(cells) <= max(col_map.values()):
            continue

        raw_cert = cells[col_map["cert"]].get_text(strip=True)
        raw_date = cells[col_map["failure_date"]].get_text(strip=True)

        try:
            cert = _parse_cert(raw_cert)
        except (ValueError, IndexError):
            continue

        try:
            failure_date = _parse_date(raw_date)
        except (ValueError, IndexError):
            continue

        institution_name = cells[col_map["institution_name"]].get_text(strip=True)
        city = cells[col_map["city"]].get_text(strip=True)
        state = cells[col_map["state"]].get_text(strip=True)

        if not institution_name or not city or not state:
            continue

        records.append(
            {
                "cert": cert,
                "institution_name": institution_name,
                "city": city,
                "state": state,
                "failure_date": failure_date,
                "source_url": source_url,
            }
        )

    if not records:
        raise ValueError("No bank failure records extracted from FDIC page")

    return records


def scrape() -> list[dict]:
    """Fetch the live FDIC failed bank list page and return parsed records.

    Delegates HTTP fetching to src.scrapers.http_client.fetch, which enforces
    a User-Agent header, a 2–5 s polite delay, robots.txt compliance, and
    exponential backoff on 429/5xx responses. An additional 3-second courtesy
    sleep is applied after the response is received.

    Returns:
        Same structure as run().

    Raises:
        ValueError: Propagated from run() when no records are extracted.
        RuntimeError: When robots.txt disallows the target URL.
    """
    resp = fetch(SOURCE_URL)
    time.sleep(3)
    return run(resp.text, source_url=SOURCE_URL)


def _record_to_proto(record: dict) -> FdicBankFailureRecord:
    """Convert a parsed record dict to a FdicBankFailureRecord dataclass.

    Monetary fields (approx_assets_usd_millions, approx_deposits_usd_millions,
    estimated_loss_usd_millions) are not present in the FDIC HTML table and are
    left as None (optional fields).

    Args:
        record: Dict as returned by run().

    Returns:
        Populated FdicBankFailureRecord instance.
    """
    msg = FdicBankFailureRecord()
    msg.cert = record["cert"]
    msg.institution_name = record["institution_name"]
    msg.city = record["city"]
    msg.state = record["state"]
    msg.failure_date = record["failure_date"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape FDIC failed bank data and upload records to BigQuery.

    Calls scrape(), converts each record to a FdicBankFailureRecord proto stub,
    and uploads via upload_rows with failure_date as the dedup column.

    Returns:
        Count of successfully inserted rows.

    Raises:
        ValueError: When scrape() extracts zero records.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("fdic_bank_failures", messages, date_column="failure_date")


if __name__ == "__main__":
    main()
