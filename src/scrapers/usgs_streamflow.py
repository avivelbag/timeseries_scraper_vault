"""USGS NWIS daily streamflow HTML-table scraper.

Parses the daily-values page for site 01646500 (Potomac River at Little Falls)
published at SOURCE_URL.  The page requires no API key and returns a plain HTML
table with columns agency_cd, site_no, datetime, <site>_00060_00003 (discharge
in cfs), and <site>_00060_00003_cd (approval status).
"""

from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.scrapers.http_client import fetch
from protos.usgs_streamflow_pb2 import UsgsStreamflowRecord  # type: ignore[attr-defined]

SOURCE_URL = (
    "https://waterdata.usgs.gov/nwis/dv"
    "?cb_00060=on&format=html_table&site_no=01646500&period=P30D"
)
_SITE_NO = "01646500"

REQUIRED_FIELDS: list[str] = ["site_no", "date", "discharge_cfs"]

# Values that NWIS inserts when a reading is unavailable or qualified out.
_SKIP_VALUES = {"", "--", "Eqp", "Ice", "Bkw", "Mnt", "Rat", "ZFl"}


def run(html: str) -> list[dict]:
    """Parse NWIS daily-values HTML into a list of streamflow records.

    Locates the first ``<table>`` whose header row contains a column matching
    ``_00060_00003`` (the NWIS parameter code for daily mean discharge), then
    yields one dict per data row that has a parseable date and positive numeric
    discharge value.

    Rows whose discharge cell is in ``_SKIP_VALUES`` or cannot be cast to float
    are silently dropped so callers receive only valid observations.

    The site name is extracted from the nearest ``<h2>`` or ``<h3>`` heading
    that contains the site number; falls back to an empty string when absent.

    Args:
        html: Raw HTML string of the NWIS daily-values page.

    Returns:
        List of dicts with keys: site_no, site_name, date, discharge_cfs,
        approval_status, source_url.  fetch_time is omitted — callers that need
        it should add it after calling this function.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    site_name = ""
    for heading in soup.find_all(["h1", "h2", "h3"]):
        text = heading.get_text(strip=True)
        if _SITE_NO in text:
            site_name = text
            break

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if header_row is None:
            continue
        headers = [cell.get_text(strip=True) for cell in header_row.find_all(["th", "td"])]

        val_col = next(
            (i for i, h in enumerate(headers) if "_00060_00003" in h and not h.endswith("_cd")),
            None,
        )
        cd_col = next(
            (i for i, h in enumerate(headers) if h.endswith("_00060_00003_cd")),
            None,
        )
        if val_col is None or cd_col is None:
            continue

        try:
            date_col = headers.index("datetime")
            site_no_col = headers.index("site_no")
        except ValueError:
            continue

        min_cols = max(date_col, val_col, cd_col, site_no_col) + 1
        for tr in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < min_cols:
                continue

            date_str = cells[date_col]
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue

            raw_val = cells[val_col]
            if raw_val in _SKIP_VALUES:
                continue
            try:
                discharge = float(raw_val)
            except ValueError:
                continue

            records.append(
                {
                    "site_no": cells[site_no_col],
                    "site_name": site_name,
                    "date": date_str,
                    "discharge_cfs": discharge,
                    "approval_status": cells[cd_col],
                    "source_url": SOURCE_URL,
                }
            )

        if records:
            break

    return records


def scrape() -> list[dict]:
    """Fetch the live NWIS daily-values page and return parsed streamflow records.

    Delegates HTTP fetching to ``src.http_client.fetch``, which enforces
    robots.txt compliance, a polite 2–5 s delay, and exponential backoff on
    transient errors.

    Returns:
        Same structure as ``run()``.
    """
    resp = fetch(SOURCE_URL)
    return run(resp.text)


def _record_to_proto(record: dict) -> UsgsStreamflowRecord:
    msg = UsgsStreamflowRecord()
    msg.site_no = record["site_no"]
    msg.site_name = record["site_name"]
    msg.date = record["date"]
    msg.discharge_cfs = record["discharge_cfs"]
    msg.approval_status = record["approval_status"]
    msg.source_url = record["source_url"]
    msg.fetch_time.FromDatetime(datetime.now(timezone.utc))
    return msg


def main() -> int:
    """Scrape USGS streamflow data and upload records to BigQuery.

    Calls scrape(), converts each record to a UsgsStreamflowRecord proto, and
    uploads via upload_rows. Returns the count of successfully inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("usgs_streamflow", messages, date_column="date")


if __name__ == "__main__":
    main()
