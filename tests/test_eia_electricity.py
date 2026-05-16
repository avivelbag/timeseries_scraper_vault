"""Tests for src/scrapers/eia_electricity.py.

All tests use a static HTML fixture or inline HTML — no live network calls are made.
"""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.eia_electricity import run, scrape, SOURCE_URL, REQUIRED_FIELDS
from protos.eia_electricity_pb2 import EiaElectricityRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "eia_electricity.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


_INLINE_HTML = """
<html><body>
<table>
  <thead>
    <tr>
      <th>Week Ending</th>
      <th>Coal</th>
      <th>Natural Gas</th>
      <th>Nuclear</th>
      <th>Wind</th>
      <th>Solar</th>
      <th>Hydro</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Total United States</td>
      <td>100,000</td><td>200,000</td><td>50,000</td>
      <td>30,000</td><td>20,000</td><td>15,000</td>
    </tr>
    <tr>
      <td>05/03/2025</td>
      <td>56,123</td>
      <td>97,450</td>
      <td>48,321</td>
      <td>W</td>
      <td>32,100</td>
      <td>28,750</td>
    </tr>
    <tr>
      <td>04/26/2025</td>
      <td>54,890</td>
      <td>93,200</td>
      <td>47,980</td>
      <td>31,450</td>
      <td>--</td>
      <td>29,100</td>
    </tr>
    <tr>
      <td>Total (year-to-date)</td>
      <td>999,999</td><td>888,888</td><td>777,777</td>
      <td>666,666</td><td>555,555</td><td>444,444</td>
    </tr>
  </tbody>
</table>
</body></html>
"""


class TestRunHappyPath:
    def test_fixture_returns_records(self, sample_html):
        records = run(sample_html)
        assert len(records) >= 1

    def test_fixture_yields_correct_date_fuel_value_triples(self, sample_html):
        records = run(sample_html)
        coal_records = [r for r in records if r["fuel_type"] == "Coal"]
        assert any(r["week_ending_date"] == "2025-05-03" for r in coal_records)

    def test_inline_correct_date_fuel_value_triples(self):
        records = run(_INLINE_HTML)
        coal_may3 = next(
            (r for r in records if r["fuel_type"] == "Coal" and r["week_ending_date"] == "2025-05-03"),
            None,
        )
        assert coal_may3 is not None
        assert coal_may3["generation_thousand_mwh"] == pytest.approx(56123.0)

        gas_apr26 = next(
            (r for r in records if r["fuel_type"] == "Natural Gas" and r["week_ending_date"] == "2025-04-26"),
            None,
        )
        assert gas_apr26 is not None
        assert gas_apr26["generation_thousand_mwh"] == pytest.approx(93200.0)

    def test_all_dates_are_yyyy_mm_dd(self, sample_html):
        for record in run(sample_html):
            datetime.strptime(record["week_ending_date"], "%Y-%m-%d")

    def test_all_generation_values_positive_floats(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["generation_thousand_mwh"], float)
            assert record["generation_thousand_mwh"] > 0

    def test_source_url_points_to_eia(self, sample_html):
        for record in run(sample_html):
            assert "eia.gov" in record["source_url"]

    def test_source_url_constant_matches_module_value(self, sample_html):
        for record in run(sample_html):
            assert record["source_url"] == SOURCE_URL


class TestTotalRowsSkipped:
    def test_rows_containing_total_are_skipped(self):
        records = run(_INLINE_HTML)
        for record in records:
            assert "Total" not in record["week_ending_date"]

    def test_total_row_dates_not_in_results(self):
        """No record should have a week_ending_date derived from a Total row."""
        records = run(_INLINE_HTML)
        dates = {r["week_ending_date"] for r in records}
        assert "2025-05-03" in dates or "2025-04-26" in dates
        assert len(dates) <= 2

    def test_inline_total_rows_excluded_count(self):
        """Only data rows (not Total rows) produce records."""
        records = run(_INLINE_HTML)
        dates = {r["week_ending_date"] for r in records}
        assert dates == {"2025-05-03", "2025-04-26"}


class TestSkippedCells:
    def test_w_withheld_cells_are_skipped(self):
        """Wind on 05/03/2025 is 'W'; that fuel-date pair must not appear."""
        records = run(_INLINE_HTML)
        wind_may3 = [
            r for r in records
            if r["fuel_type"] == "Wind" and r["week_ending_date"] == "2025-05-03"
        ]
        assert wind_may3 == []

    def test_dash_dash_cells_are_skipped(self):
        """Solar on 04/26/2025 is '--'; that fuel-date pair must not appear."""
        records = run(_INLINE_HTML)
        solar_apr26 = [
            r for r in records
            if r["fuel_type"] == "Solar" and r["week_ending_date"] == "2025-04-26"
        ]
        assert solar_apr26 == []

    def test_skipping_w_does_not_raise(self):
        html = """
        <html><body><table>
          <tr><th>Week</th><th>Coal</th><th>Natural Gas</th></tr>
          <tr><td>01/10/2025</td><td>W</td><td>50,000</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["fuel_type"] == "Natural Gas"

    def test_skipping_dash_does_not_raise(self):
        html = """
        <html><body><table>
          <tr><th>Week</th><th>Coal</th><th>Natural Gas</th></tr>
          <tr><td>01/10/2025</td><td>--</td><td>50,000</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["fuel_type"] == "Natural Gas"

    def test_comma_separated_numbers_parsed_correctly(self):
        html = """
        <html><body><table>
          <tr><th>Week</th><th>Coal</th><th>Natural Gas</th></tr>
          <tr><td>03/15/2025</td><td>1,234,567</td><td>9,876,543</td></tr>
        </table></body></html>
        """
        records = run(html)
        coal = next(r for r in records if r["fuel_type"] == "Coal")
        assert coal["generation_thousand_mwh"] == pytest.approx(1234567.0)


class TestDateParsing:
    def test_mm_dd_yyyy_parsed_to_yyyy_mm_dd(self):
        html = """
        <html><body><table>
          <tr><th>Week</th><th>Coal</th><th>Natural Gas</th></tr>
          <tr><td>12/31/2024</td><td>40,000</td><td>80,000</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert records[0]["week_ending_date"] == "2024-12-31"

    def test_invalid_date_format_row_is_skipped(self):
        html = """
        <html><body><table>
          <tr><th>Week</th><th>Coal</th><th>Natural Gas</th></tr>
          <tr><td>2025-01-05</td><td>40,000</td><td>80,000</td></tr>
          <tr><td>01/05/2025</td><td>41,000</td><td>81,000</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 2
        assert records[0]["week_ending_date"] == "2025-01-05"

    def test_non_date_row_is_skipped(self):
        html = """
        <html><body><table>
          <tr><th>Week</th><th>Coal</th><th>Natural Gas</th></tr>
          <tr><td>Notes</td><td>40,000</td><td>80,000</td></tr>
          <tr><td>01/05/2025</td><td>41,000</td><td>81,000</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 2


class TestProtoFields:
    def test_record_to_proto_field_correctness(self):
        """_record_to_proto sets all fields correctly on the proto dataclass."""
        from src.scrapers.eia_electricity import _record_to_proto

        record = {
            "source_url": SOURCE_URL,
            "week_ending_date": "2025-05-03",
            "fuel_type": "Coal",
            "generation_thousand_mwh": 56123.0,
        }
        msg = _record_to_proto(record)
        assert isinstance(msg, EiaElectricityRecord)
        assert msg.source_url == SOURCE_URL
        assert msg.week_ending_date == "2025-05-03"
        assert msg.fuel_type == "Coal"
        assert msg.generation_thousand_mwh == pytest.approx(56123.0)
        assert msg.fetch_time != ""
        datetime.fromisoformat(msg.fetch_time)

    def test_proto_dataclass_defaults(self):
        msg = EiaElectricityRecord()
        assert msg.week_ending_date == ""
        assert msg.fuel_type == ""
        assert msg.generation_thousand_mwh == 0.0
        assert msg.source_url == ""
        assert msg.fetch_time == ""


class TestRunEdgeCases:
    def test_empty_string_returns_empty_list(self):
        assert run("") == []

    def test_no_matching_table_returns_empty_list(self):
        html = "<html><body><table><tr><td>Foo</td><td>Bar</td></tr></table></body></html>"
        assert run(html) == []

    def test_table_without_coal_and_gas_headers_is_ignored(self):
        html = """
        <html><body><table>
          <tr><th>Week</th><th>Oil</th><th>Propane</th></tr>
          <tr><td>01/05/2025</td><td>10,000</td><td>5,000</td></tr>
        </table></body></html>
        """
        assert run(html) == []

    def test_required_fields_all_present(self, sample_html):
        required = set(REQUIRED_FIELDS)
        for record in run(sample_html):
            assert required.issubset(record.keys()), f"Missing keys: {record}"

    def test_large_table_all_records_valid(self):
        """Synthetic table with 50 weeks and 6 fuel types produces 300 records."""
        fuel_headers = "<th>Coal</th><th>Natural Gas</th><th>Nuclear</th><th>Wind</th><th>Solar</th><th>Hydro</th>"
        rows = ""
        base_month = 1
        base_day = 5
        for week in range(50):
            month = (base_month + (week // 4)) % 12 or 12
            day = (base_day + (week * 7)) % 28 or 1
            rows += f"<tr><td>{month:02d}/{day:02d}/2024</td><td>50,000</td><td>90,000</td><td>45,000</td><td>25,000</td><td>20,000</td><td>15,000</td></tr>"
        html = f"<html><body><table><tr><th>Week</th>{fuel_headers}</tr>{rows}</table></body></html>"
        records = run(html)
        assert len(records) == 300

    def test_row_with_no_valid_cells_produces_no_records(self):
        html = """
        <html><body><table>
          <tr><th>Week</th><th>Coal</th><th>Natural Gas</th></tr>
          <tr><td>01/05/2025</td><td>W</td><td>--</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert records == []


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.eia_electricity.fetch", return_value=fake_resp) as mock_fetch:
            records = scrape()
        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_returns_same_as_run(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.eia_electricity.fetch", return_value=fake_resp):
            scraped = scrape()
        direct = run(_INLINE_HTML)
        assert scraped == direct

    def test_scrape_no_live_network(self):
        """Confirm scrape() is mock-patched and never touches the network."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.eia_electricity.fetch", return_value=fake_resp) as mock_fetch:
            scrape()
        assert mock_fetch.call_count == 1

    def test_scrape_records_have_correct_fuel_types(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = _INLINE_HTML
        with patch("src.scrapers.eia_electricity.fetch", return_value=fake_resp):
            records = scrape()
        fuel_types = {r["fuel_type"] for r in records}
        assert "Coal" in fuel_types
        assert "Natural Gas" in fuel_types
