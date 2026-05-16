"""Tests for src/scrapers/eia_petroleum.py.

All tests use a static HTML fixture — no live network calls are made.
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.eia_petroleum import run, scrape, SOURCE_URL

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "eia_petroleum_sample.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


class TestRunHappyPath:
    def test_returns_at_least_five_records(self, sample_html):
        assert len(run(sample_html)) >= 5

    def test_all_period_dates_valid_iso(self, sample_html):
        for record in run(sample_html):
            date.fromisoformat(record["period_date"])

    def test_all_prices_positive(self, sample_html):
        for record in run(sample_html):
            assert record["price_usd_per_gallon"] > 0

    def test_product_and_region_non_empty_strings(self, sample_html):
        for record in run(sample_html):
            assert record["product"] and isinstance(record["product"], str)
            assert record["region"] and isinstance(record["region"], str)

    def test_source_url_points_to_eia(self, sample_html):
        for record in run(sample_html):
            assert "eia.gov" in record["source_url"]

    def test_grade_field_non_empty(self, sample_html):
        for record in run(sample_html):
            assert record["grade"] and isinstance(record["grade"], str)

    def test_units_field_present_and_non_empty(self, sample_html):
        for record in run(sample_html):
            assert record.get("units") and isinstance(record["units"], str)


class TestRunEdgeCases:
    def test_empty_string_returns_empty_list(self):
        assert run("") == []

    def test_html_with_no_data_tables_returns_empty_list(self):
        html = "<html><body><table><tr><td>random</td></tr></table></body></html>"
        assert run(html) == []

    def test_missing_price_cells_are_skipped(self):
        html = """
        <html><body>
        <h2>Regular Gasoline</h2>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>U.S.</th><th>East Coast</th></tr></thead>
          <tbody>
            <tr><td>01/06/2025</td><td>--</td><td>--</td></tr>
            <tr><td>01/13/2025</td><td>3.200</td><td>3.150</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 2
        assert all(r["price_usd_per_gallon"] > 0 for r in records)

    def test_rows_with_invalid_date_format_are_skipped(self):
        html = """
        <html><body>
        <h2>Regular Gasoline</h2>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>U.S.</th></tr></thead>
          <tbody>
            <tr><td>not-a-date</td><td>3.200</td></tr>
            <tr><td>2025-01-13</td><td>3.100</td></tr>
            <tr><td>01/13/2025</td><td>3.150</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["period_date"] == "2025-01-13"

    def test_empty_cell_value_is_skipped(self):
        html = """
        <html><body>
        <h2>Diesel</h2>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>U.S.</th></tr></thead>
          <tbody>
            <tr><td>01/06/2025</td><td></td></tr>
            <tr><td>01/13/2025</td><td>3.654</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["price_usd_per_gallon"] == 3.654

    def test_table_without_preceding_heading_uses_unknown_grade(self):
        html = """
        <html><body>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>U.S.</th></tr></thead>
          <tbody>
            <tr><td>01/06/2025</td><td>3.100</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["grade"] == "unknown"


class TestRunRecordStructure:
    def test_required_proto_fields_all_present(self, sample_html):
        required = {"source_url", "period_date", "product", "region", "price_usd_per_gallon", "grade", "units"}
        for record in run(sample_html):
            assert required.issubset(record.keys()), f"Missing keys in record: {record}"

    def test_price_is_float(self, sample_html):
        for record in run(sample_html):
            assert isinstance(record["price_usd_per_gallon"], float)

    def test_source_url_constant_matches_module_value(self, sample_html):
        for record in run(sample_html):
            assert record["source_url"] == SOURCE_URL


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body>
        <h2>Regular Gasoline</h2>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>U.S.</th></tr></thead>
          <tbody>
            <tr><td>02/10/2025</td><td>3.012</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with patch("src.scrapers.eia_petroleum.fetch", return_value=fake_resp) as mock_fetch:
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) == 1
        assert records[0]["price_usd_per_gallon"] == 3.012

    def test_scrape_returns_run_output(self):
        """scrape() must return the same records as run() would on the same HTML."""
        test_html = """
        <html><body>
        <h2>Premium Gasoline</h2>
        <table class="DataTable">
          <thead><tr><th>Date</th><th>U.S.</th><th>East Coast</th></tr></thead>
          <tbody>
            <tr><td>02/10/2025</td><td>3.912</td><td>3.876</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = test_html

        with patch("src.scrapers.eia_petroleum.fetch", return_value=fake_resp):
            scraped = scrape()

        direct = run(test_html)
        assert scraped == direct
