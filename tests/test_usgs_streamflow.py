"""Tests for src/scrapers/usgs_streamflow.py.

All tests use a static HTML fixture — no live network calls are made.
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.usgs_streamflow import run, scrape, SOURCE_URL, REQUIRED_FIELDS

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "usgs_streamflow_sample.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


class TestRunHappyPath:
    def test_returns_at_least_five_records(self, sample_html):
        assert len(run(sample_html)) >= 5

    def test_all_dates_valid_iso_format(self, sample_html):
        for record in run(sample_html):
            date.fromisoformat(record["date"])

    def test_all_dates_yyyy_mm_dd(self, sample_html):
        import re

        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for record in run(sample_html):
            assert pattern.match(record["date"]), f"Bad date format: {record['date']}"

    def test_all_discharge_positive(self, sample_html):
        for record in run(sample_html):
            assert record["discharge_cfs"] > 0

    def test_approval_status_a_or_p(self, sample_html):
        for record in run(sample_html):
            assert record["approval_status"] in ("A", "P"), (
                f"Unexpected approval_status: {record['approval_status']}"
            )

    def test_site_no_correct(self, sample_html):
        for record in run(sample_html):
            assert record["site_no"] == "01646500"

    def test_site_name_non_empty(self, sample_html):
        for record in run(sample_html):
            assert record["site_name"] and isinstance(record["site_name"], str)

    def test_source_url_points_to_usgs(self, sample_html):
        for record in run(sample_html):
            assert "waterdata.usgs.gov" in record["source_url"]


class TestRunEdgeCases:
    def test_empty_string_returns_empty_list(self):
        assert run("") == []

    def test_html_without_discharge_table_returns_empty_list(self):
        html = "<html><body><table><tr><th>col1</th><th>col2</th></tr></table></body></html>"
        assert run(html) == []

    def test_rows_with_skip_values_are_dropped(self):
        html = """
        <html><body>
        <h2>USGS 01646500 Test Station</h2>
        <table>
          <tr><th>agency_cd</th><th>site_no</th><th>datetime</th>
              <th>01646500_00060_00003</th><th>01646500_00060_00003_cd</th></tr>
          <tr><td>USGS</td><td>01646500</td><td>2025-01-01</td><td>--</td><td>A</td></tr>
          <tr><td>USGS</td><td>01646500</td><td>2025-01-02</td><td>Eqp</td><td>P</td></tr>
          <tr><td>USGS</td><td>01646500</td><td>2025-01-03</td><td>1500</td><td>A</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["discharge_cfs"] == 1500.0

    def test_rows_with_non_numeric_discharge_are_skipped(self):
        html = """
        <html><body>
        <table>
          <tr><th>agency_cd</th><th>site_no</th><th>datetime</th>
              <th>01646500_00060_00003</th><th>01646500_00060_00003_cd</th></tr>
          <tr><td>USGS</td><td>01646500</td><td>2025-01-01</td><td>notanumber</td><td>A</td></tr>
          <tr><td>USGS</td><td>01646500</td><td>2025-01-02</td><td>900</td><td>A</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["date"] == "2025-01-02"

    def test_rows_with_invalid_date_are_skipped(self):
        html = """
        <html><body>
        <table>
          <tr><th>agency_cd</th><th>site_no</th><th>datetime</th>
              <th>01646500_00060_00003</th><th>01646500_00060_00003_cd</th></tr>
          <tr><td>USGS</td><td>01646500</td><td>not-a-date</td><td>1000</td><td>A</td></tr>
          <tr><td>USGS</td><td>01646500</td><td>2025-01-03</td><td>1200</td><td>A</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["date"] == "2025-01-03"

    def test_site_name_falls_back_to_empty_string_when_no_heading(self):
        html = """
        <html><body>
        <table>
          <tr><th>agency_cd</th><th>site_no</th><th>datetime</th>
              <th>01646500_00060_00003</th><th>01646500_00060_00003_cd</th></tr>
          <tr><td>USGS</td><td>01646500</td><td>2025-01-01</td><td>1000</td><td>A</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["site_name"] == ""

    def test_table_without_required_columns_is_skipped(self):
        html = """
        <html><body>
        <table>
          <tr><th>col_a</th><th>col_b</th></tr>
          <tr><td>foo</td><td>bar</td></tr>
        </table>
        <table>
          <tr><th>agency_cd</th><th>site_no</th><th>datetime</th>
              <th>01646500_00060_00003</th><th>01646500_00060_00003_cd</th></tr>
          <tr><td>USGS</td><td>01646500</td><td>2025-01-01</td><td>1100</td><td>A</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1


class TestRunRecordStructure:
    def test_required_fields_all_present(self, sample_html):
        required = set(REQUIRED_FIELDS)
        for record in run(sample_html):
            assert required.issubset(record.keys()), f"Missing keys in: {record}"

    def test_discharge_cfs_is_float(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["discharge_cfs"], float)

    def test_source_url_matches_module_constant(self, sample_html):
        for record in run(sample_html):
            assert record["source_url"] == SOURCE_URL

    def test_required_fields_constant_contains_site_no_date_discharge(self):
        for field in ("site_no", "date", "discharge_cfs"):
            assert field in REQUIRED_FIELDS


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body>
        <h2>USGS 01646500 Potomac River at Little Falls</h2>
        <table>
          <tr><th>agency_cd</th><th>site_no</th><th>datetime</th>
              <th>01646500_00060_00003</th><th>01646500_00060_00003_cd</th></tr>
          <tr><td>USGS</td><td>01646500</td><td>2025-02-01</td><td>2100</td><td>A</td></tr>
        </table>
        </body></html>
        """
        with patch("src.scrapers.usgs_streamflow.fetch", return_value=fake_resp) as mock_fetch:
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) == 1
        assert records[0]["discharge_cfs"] == 2100.0

    def test_scrape_returns_same_as_run(self):
        test_html = """
        <html><body>
        <h2>USGS 01646500 Potomac River at Little Falls</h2>
        <table>
          <tr><th>agency_cd</th><th>site_no</th><th>datetime</th>
              <th>01646500_00060_00003</th><th>01646500_00060_00003_cd</th></tr>
          <tr><td>USGS</td><td>01646500</td><td>2025-02-01</td><td>1800</td><td>P</td></tr>
          <tr><td>USGS</td><td>01646500</td><td>2025-02-02</td><td>1950</td><td>P</td></tr>
        </table>
        </body></html>
        """
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = test_html

        with patch("src.scrapers.usgs_streamflow.fetch", return_value=fake_resp):
            scraped = scrape()

        direct = run(test_html)
        assert scraped == direct
