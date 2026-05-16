"""Tests for src/scrapers/eia_natural_gas.py.

All tests use a static HTML fixture — no live network calls are made.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.eia_natural_gas import run, scrape, SOURCE_URL, REQUIRED_FIELDS

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "eia_natural_gas.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


class TestRunHappyPath:
    def test_returns_at_least_twelve_records(self, sample_html):
        assert len(run(sample_html)) >= 12

    def test_all_period_dates_are_yyyy_mm(self, sample_html):
        pattern = re.compile(r"^\d{4}-\d{2}$")
        for record in run(sample_html):
            assert pattern.match(record["period_date"]), (
                f"period_date {record['period_date']!r} is not YYYY-MM"
            )

    def test_all_prices_positive_floats(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["price_usd_per_mmbtu"], float)
            assert record["price_usd_per_mmbtu"] > 0

    def test_all_locations_are_henry_hub(self, sample_html):
        for record in run(sample_html):
            assert record["location"] == "Henry Hub"

    def test_source_url_points_to_eia(self, sample_html):
        for record in run(sample_html):
            assert "eia.gov" in record["source_url"]

    def test_source_url_constant_matches_module_value(self, sample_html):
        for record in run(sample_html):
            assert record["source_url"] == SOURCE_URL

    def test_period_dates_are_valid_year_month(self, sample_html):
        from datetime import datetime
        for record in run(sample_html):
            datetime.strptime(record["period_date"], "%Y-%m")


class TestRunEdgeCases:
    def test_empty_string_returns_empty_list(self):
        assert run("") == []

    def test_html_with_no_data_tables_returns_empty_list(self):
        html = "<html><body><table><tr><td>random</td></tr></table></body></html>"
        assert run(html) == []

    def test_missing_price_cells_are_skipped(self):
        html = """
        <html><body>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>Price</th></tr></thead>
          <tbody>
            <tr><td>Jan-2025</td><td>--</td></tr>
            <tr><td>Feb-2025</td><td>3.512</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["period_date"] == "2025-02"
        assert records[0]["price_usd_per_mmbtu"] == pytest.approx(3.512)

    def test_rows_with_invalid_date_format_are_skipped(self):
        html = """
        <html><body>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>Price</th></tr></thead>
          <tbody>
            <tr><td>not-a-date</td><td>3.200</td></tr>
            <tr><td>2025-01</td><td>3.100</td></tr>
            <tr><td>Mar-2025</td><td>3.150</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["period_date"] == "2025-03"

    def test_empty_price_cell_is_skipped(self):
        html = """
        <html><body>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>Price</th></tr></thead>
          <tbody>
            <tr><td>Jan-2025</td><td></td></tr>
            <tr><td>Feb-2025</td><td>2.750</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["price_usd_per_mmbtu"] == pytest.approx(2.750)

    def test_price_with_comma_separator_is_parsed(self):
        html = """
        <html><body>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>Price</th></tr></thead>
          <tbody>
            <tr><td>Jan-2000</td><td>1,234.56</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["price_usd_per_mmbtu"] == pytest.approx(1234.56)

    def test_row_with_fewer_than_two_cells_is_skipped(self):
        html = """
        <html><body>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>Price</th></tr></thead>
          <tbody>
            <tr><td>Jan-2025</td></tr>
            <tr><td>Feb-2025</td><td>2.750</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["period_date"] == "2025-02"


class TestRunRecordStructure:
    def test_required_fields_all_present(self, sample_html):
        required = set(REQUIRED_FIELDS)
        for record in run(sample_html):
            assert required.issubset(record.keys()), f"Missing keys in record: {record}"

    def test_price_is_float_type(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["price_usd_per_mmbtu"], float)

    def test_period_date_is_string_type(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["period_date"], str)

    def test_location_is_string_type(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["location"], str)


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>Price</th></tr></thead>
          <tbody>
            <tr><td>Mar-2025</td><td>3.200</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with patch("src.scrapers.eia_natural_gas.fetch", return_value=fake_resp) as mock_fetch:
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) == 1
        assert records[0]["price_usd_per_mmbtu"] == pytest.approx(3.200)

    def test_scrape_returns_run_output(self):
        """scrape() must return the same records as run() would on the same HTML."""
        test_html = """
        <html><body>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>Price</th></tr></thead>
          <tbody>
            <tr><td>Apr-2025</td><td>2.875</td></tr>
            <tr><td>May-2025</td><td>3.012</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = test_html

        with patch("src.scrapers.eia_natural_gas.fetch", return_value=fake_resp):
            scraped = scrape()

        direct = run(test_html)
        assert scraped == direct

    def test_scrape_record_location_is_henry_hub(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>Price</th></tr></thead>
          <tbody>
            <tr><td>Jun-2025</td><td>2.650</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with patch("src.scrapers.eia_natural_gas.fetch", return_value=fake_resp):
            records = scrape()

        assert records[0]["location"] == "Henry Hub"
