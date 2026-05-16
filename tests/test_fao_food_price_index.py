"""Tests for src/scrapers/fao_food_price_index.py.

All tests use a static HTML fixture or inline HTML — no live network calls are made.
"""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.fao_food_price_index import run, scrape, SOURCE_URL, REQUIRED_FIELDS
from protos.fao_food_price_index_pb2 import FaoFoodPriceRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "fao_food_price_index.html")

FIVE_COMMODITY_GROUPS = {
    "Cereals Price Index",
    "Vegetable Oil Price Index",
    "Dairy Price Index",
    "Meat Price Index",
    "Sugar Price Index",
}


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


_INLINE_HTML = """
<html><body>
<table>
  <thead>
    <tr>
      <th>Year</th>
      <th>Month</th>
      <th>Food Price Index</th>
      <th>Cereals Price Index</th>
      <th>Vegetable Oil Price Index</th>
      <th>Dairy Price Index</th>
      <th>Meat Price Index</th>
      <th>Sugar Price Index</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>2025</td><td>Mar</td><td>127.8</td><td>110.4</td><td>163.2</td><td>136.1</td><td>123.4</td><td>118.5</td>
    </tr>
    <tr>
      <td>2025</td><td>Feb</td><td>126.4</td><td>109.8</td><td>161.5</td><td>134.7</td><td>122.9</td><td>116.2</td>
    </tr>
    <tr>
      <td>2025</td><td>Jan</td><td>124.9</td><td>111.2</td><td>158.3</td><td>133.8</td><td>121.5</td><td>112.4</td>
    </tr>
  </tbody>
</table>
</body></html>
"""


class TestRunHappyPath:
    def test_fixture_returns_at_least_three_records(self, sample_html):
        records = run(sample_html)
        assert len(records) >= 3

    def test_inline_returns_at_least_three_records(self):
        records = run(_INLINE_HTML)
        assert len(records) >= 3

    def test_all_five_commodity_groups_present(self, sample_html):
        records = run(sample_html)
        found = {r["commodity_group"] for r in records}
        assert FIVE_COMMODITY_GROUPS.issubset(found)

    def test_all_five_commodity_groups_present_inline(self):
        records = run(_INLINE_HTML)
        found = {r["commodity_group"] for r in records}
        assert FIVE_COMMODITY_GROUPS.issubset(found)

    def test_all_index_values_positive(self, sample_html):
        records = run(sample_html)
        for record in records:
            assert record["index_value"] > 0.0

    def test_date_format_is_yyyy_mm(self, sample_html):
        for record in run(sample_html):
            datetime.strptime(record["date"], "%Y-%m")

    def test_source_url_in_every_record(self, sample_html):
        for record in run(sample_html):
            assert record["source_url"] == SOURCE_URL

    def test_specific_cereal_value_march_2025(self):
        records = run(_INLINE_HTML)
        cereal_mar = next(
            (r for r in records if r["commodity_group"] == "Cereals Price Index" and r["date"] == "2025-03"),
            None,
        )
        assert cereal_mar is not None
        assert cereal_mar["index_value"] == pytest.approx(110.4)

    def test_specific_dairy_value_january_2025(self):
        records = run(_INLINE_HTML)
        dairy_jan = next(
            (r for r in records if r["commodity_group"] == "Dairy Price Index" and r["date"] == "2025-01"),
            None,
        )
        assert dairy_jan is not None
        assert dairy_jan["index_value"] == pytest.approx(133.8)

    def test_fanout_produces_multiple_records_per_month(self):
        records = run(_INLINE_HTML)
        mar_records = [r for r in records if r["date"] == "2025-03"]
        assert len(mar_records) >= 5


class TestRequiredFields:
    def test_all_required_fields_present_in_fixture(self, sample_html):
        required = set(REQUIRED_FIELDS)
        for record in run(sample_html):
            assert required.issubset(record.keys()), f"Missing keys: {record}"

    def test_all_required_fields_correct_types(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["date"], str)
            assert isinstance(record["commodity_group"], str)
            assert isinstance(record["index_value"], float)
            assert isinstance(record["source_url"], str)


class TestEdgeCases:
    def test_empty_html_returns_empty_list(self):
        assert run("") == []

    def test_no_matching_table_returns_empty_list(self):
        html = "<html><body><table><tr><th>Year</th><th>Month</th><th>Oil</th></tr></table></body></html>"
        assert run(html) == []

    def test_table_without_cereal_and_dairy_is_ignored(self):
        html = """
        <html><body><table>
          <tr><th>Year</th><th>Month</th><th>Wheat</th><th>Rice</th></tr>
          <tr><td>2025</td><td>Jan</td><td>100.0</td><td>95.0</td></tr>
        </table></body></html>
        """
        assert run(html) == []

    def test_invalid_year_row_is_skipped(self):
        html = """
        <html><body><table>
          <tr><th>Year</th><th>Month</th><th>Cereals Price Index</th><th>Dairy Price Index</th></tr>
          <tr><td>N/A</td><td>Jan</td><td>100.0</td><td>95.0</td></tr>
          <tr><td>2025</td><td>Feb</td><td>110.0</td><td>105.0</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 2
        assert all(r["date"] == "2025-02" for r in records)

    def test_invalid_month_row_is_skipped(self):
        html = """
        <html><body><table>
          <tr><th>Year</th><th>Month</th><th>Cereals Price Index</th><th>Dairy Price Index</th></tr>
          <tr><td>2025</td><td>Q1</td><td>100.0</td><td>95.0</td></tr>
          <tr><td>2025</td><td>Mar</td><td>110.0</td><td>105.0</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 2
        assert all(r["date"] == "2025-03" for r in records)

    def test_non_numeric_cell_is_skipped(self):
        html = """
        <html><body><table>
          <tr><th>Year</th><th>Month</th><th>Cereals Price Index</th><th>Dairy Price Index</th></tr>
          <tr><td>2025</td><td>Jan</td><td>N/A</td><td>95.0</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["commodity_group"] == "Dairy Price Index"

    def test_large_table_all_records_valid(self):
        """12 months × 5 commodity columns produces 60 records."""
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        headers = (
            "<th>Year</th><th>Month</th>"
            "<th>Cereals Price Index</th><th>Vegetable Oil Price Index</th>"
            "<th>Dairy Price Index</th><th>Meat Price Index</th><th>Sugar Price Index</th>"
        )
        rows = "".join(
            f"<tr><td>2024</td><td>{m}</td>"
            "<td>100.0</td><td>150.0</td><td>120.0</td><td>110.0</td><td>130.0</td></tr>"
            for m in months
        )
        html = f"<html><body><table><tr>{headers}</tr>{rows}</table></body></html>"
        records = run(html)
        assert len(records) == 60
        assert all(r["index_value"] > 0 for r in records)

    def test_row_with_all_non_numeric_cells_produces_no_records(self):
        html = """
        <html><body><table>
          <tr><th>Year</th><th>Month</th><th>Cereals Price Index</th><th>Dairy Price Index</th></tr>
          <tr><td>2025</td><td>Jan</td><td>--</td><td>n/a</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert records == []


class TestProtoFields:
    def test_record_to_proto_field_correctness(self):
        """_record_to_proto sets all fields correctly on the proto dataclass."""
        from src.scrapers.fao_food_price_index import _record_to_proto

        record = {
            "date": "2025-03",
            "commodity_group": "Cereals Price Index",
            "index_value": 110.4,
            "source_url": SOURCE_URL,
        }
        msg = _record_to_proto(record)
        assert isinstance(msg, FaoFoodPriceRecord)
        assert msg.date == "2025-03"
        assert msg.commodity_group == "Cereals Price Index"
        assert msg.index_value == pytest.approx(110.4)
        assert msg.source_url == SOURCE_URL
        assert msg.fetch_time != ""
        datetime.fromisoformat(msg.fetch_time)

    def test_proto_dataclass_defaults(self):
        msg = FaoFoodPriceRecord()
        assert msg.date == ""
        assert msg.commodity_group == ""
        assert msg.index_value == 0.0
        assert msg.source_url == ""
        assert msg.fetch_time == ""


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.fao_food_price_index.fetch", return_value=fake_resp) as mock_fetch:
            records = scrape()
        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_returns_same_as_run(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.fao_food_price_index.fetch", return_value=fake_resp):
            scraped = scrape()
        direct = run(_INLINE_HTML)
        assert scraped == direct

    def test_scrape_no_live_network(self):
        """Confirm scrape() is mock-patched and never touches the network."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.fao_food_price_index.fetch", return_value=fake_resp) as mock_fetch:
            scrape()
        assert mock_fetch.call_count == 1

    def test_scrape_records_have_expected_commodity_groups(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.fao_food_price_index.fetch", return_value=fake_resp):
            records = scrape()
        found = {r["commodity_group"] for r in records}
        assert FIVE_COMMODITY_GROUPS.issubset(found)
