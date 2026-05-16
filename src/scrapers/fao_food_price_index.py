"""FAO Food Price Index monthly scraper.

Parses the HTML table published at SOURCE_URL, which lists monthly price
indices broken down by commodity group (Food Price Index, Cereals, Vegetable
Oil, Dairy, Meat, Sugar).  No API key required.
"""

from datetime import datetime, timezone

from bs4 import BeautifulSoup

from src.bq_uploader import upload_rows
from src.http_client import fetch
from protos.fao_food_price_index_pb2 import FaoFoodPriceRecord  # type: ignore[attr-defined]

SOURCE_URL = "https://www.fao.org/worldfoodsituation/foodpricesindex/en/"

REQUIRED_FIELDS: list[str] = [
    "date",
    "commodity_group",
    "index_value",
    "source_url",
]


def _find_price_table(soup: BeautifulSoup):
    """Return the first table whose headers include both Cereal and Dairy columns.

    The FAO Food Price Index page contains several elements; the price index
    table is identified by the presence of cereal and dairy column headers.
    Returns None if no matching table is found.
    """
    for table in soup.find_all("table"):
        first_row = table.find("tr")
        if first_row is None:
            continue
        cells = first_row.find_all(["th", "td"])
        header_text = " ".join(c.get_text(strip=True).lower() for c in cells)
        if "cereal" in header_text and "dairy" in header_text:
            return table
    return None


def run(html: str) -> list[dict]:
    """Parse FAO Food Price Index HTML into a list of commodity price records.

    Finds the price index table (identified by Cereal and Dairy column headers),
    extracts commodity group labels from the header row starting at column index 2,
    then iterates data rows.  For each data row:
      - The first cell is parsed as an integer year.
      - The second cell is parsed as an abbreviated month name (Jan–Dec).
      - Rows with an unparseable year or month are skipped.
      - Cells that cannot be parsed as float are skipped silently.
      - One record is emitted per commodity column per month (fanout).

    Args:
        html: Raw HTML string of the FAO Food Price Index page.

    Returns:
        List of dicts with keys: date (YYYY-MM string), commodity_group (str),
        index_value (float), source_url (str).  fetch_time is omitted —
        callers that need it should add it after calling this function.
    """
    soup = BeautifulSoup(html, "lxml")
    table = _find_price_table(soup)
    if table is None:
        return []

    rows = table.find_all("tr")
    if len(rows) < 2:
        return []

    header_cells = rows[0].find_all(["th", "td"])
    commodity_groups = [c.get_text(strip=True) for c in header_cells[2:]]
    if not commodity_groups:
        return []

    records: list[dict] = []
    for tr in rows[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 3:
            continue

        try:
            year = int(cells[0])
        except ValueError:
            continue

        month_abbr = cells[1].strip()[:3].capitalize()
        try:
            dt = datetime.strptime(f"{year} {month_abbr}", "%Y %b")
        except ValueError:
            continue
        date_str = dt.strftime("%Y-%m")

        for i, commodity_group in enumerate(commodity_groups):
            col_idx = i + 2
            if col_idx >= len(cells):
                break
            raw = cells[col_idx].replace(",", "").strip()
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue
            records.append(
                {
                    "date": date_str,
                    "commodity_group": commodity_group,
                    "index_value": value,
                    "source_url": SOURCE_URL,
                }
            )

    return records


def scrape() -> list[dict]:
    """Fetch the live FAO Food Price Index page and return parsed records.

    Delegates HTTP fetching to ``src.http_client.fetch``, which enforces
    robots.txt compliance, a polite 2–5 s delay, and exponential backoff on
    transient errors.

    Returns:
        Same structure as ``run()``.
    """
    resp = fetch(SOURCE_URL)
    return run(resp.text)


def _record_to_proto(record: dict) -> FaoFoodPriceRecord:
    msg = FaoFoodPriceRecord()
    msg.date = record["date"]
    msg.commodity_group = record["commodity_group"]
    msg.index_value = record["index_value"]
    msg.source_url = record["source_url"]
    msg.fetch_time = datetime.now(timezone.utc).isoformat()
    return msg


def main() -> int:
    """Scrape FAO Food Price Index data and upload records to BigQuery.

    Calls scrape(), converts each record to a FaoFoodPriceRecord proto, and
    uploads via upload_rows. Returns the count of successfully inserted rows.
    """
    records = scrape()
    messages = [_record_to_proto(r) for r in records]
    return upload_rows("fao_food_price_index", messages, date_column="date")


if __name__ == "__main__":
    main()
