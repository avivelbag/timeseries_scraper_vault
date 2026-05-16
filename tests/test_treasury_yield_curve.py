"""Tests for src/scrapers/treasury_yield_curve.py.

All tests use a static HTML fixture or inline HTML — no live network calls.
"""

import os
import re
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.treasury_yield_curve import (
    LABEL_TO_FIELD,
    REQUIRED_FIELDS,
    run,
    scrape,
)

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "treasury_yield_curve_sample.html"
)


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


class TestRunHappyPath:
    def test_returns_at_least_five_records(self, sample_html):
        assert len(run(sample_html)) >= 5

    def test_all_dates_yyyy_mm_dd_format(self, sample_html):
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for record in run(sample_html):
            assert pattern.match(record["date"]), f"Bad date format: {record['date']}"

    def test_all_dates_parseable_as_iso(self, sample_html):
        for record in run(sample_html):
            date.fromisoformat(record["date"])

    def test_maturity_10y_positive_for_all_records(self, sample_html):
        for record in run(sample_html):
            assert record["maturity_10y"] > 0, f"Expected positive maturity_10y, got {record['maturity_10y']}"

    def test_na_cell_produces_minus_one_sentinel(self, sample_html):
        records = run(sample_html)
        # Fixture row 05/01/2025 has N/A for 1 Mo
        na_records = [r for r in records if r["maturity_1m"] == -1.0]
        assert len(na_records) >= 1, "Expected at least one record with maturity_1m == -1.0"

    def test_non_na_rows_have_positive_maturity_1m(self, sample_html):
        records = run(sample_html)
        positive = [r for r in records if r["maturity_1m"] > 0]
        assert len(positive) >= 4

    def test_required_fields_present_in_every_record(self, sample_html):
        required = set(REQUIRED_FIELDS)
        for record in run(sample_html):
            assert required.issubset(record.keys()), f"Missing keys in: {record}"

    def test_source_url_propagated_when_passed(self, sample_html):
        url = "https://home.treasury.gov/test"
        for record in run(sample_html, source_url=url):
            assert record["source_url"] == url

    def test_source_url_empty_string_by_default(self, sample_html):
        for record in run(sample_html):
            assert record["source_url"] == ""


class TestRunEdgeCases:
    def test_empty_string_returns_empty_list(self):
        assert run("") == []

    def test_html_with_no_table_returns_empty_list(self):
        assert run("<html><body><p>No table here.</p></body></html>") == []

    def test_table_without_maturity_headers_returns_empty_list(self):
        html = "<html><body><table><tr><th>Foo</th><th>Bar</th></tr><tr><td>x</td><td>y</td></tr></table></body></html>"
        assert run(html) == []

    def test_rows_with_invalid_date_are_skipped(self):
        html = """
        <html><body><table>
          <tr><th>Date</th><th>1 Mo</th><th>10 Yr</th></tr>
          <tr><td>not-a-date</td><td>5.31</td><td>4.40</td></tr>
          <tr><td>05/01/2025</td><td>5.31</td><td>4.40</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["date"] == "2025-05-01"

    def test_header_only_table_returns_empty_list(self):
        html = """
        <html><body><table>
          <tr><th>Date</th><th>1 Mo</th><th>10 Yr</th></tr>
        </table></body></html>
        """
        assert run(html) == []

    def test_first_matching_table_is_used_second_ignored(self):
        html = """
        <html><body>
          <table>
            <tr><th>Date</th><th>10 Yr</th></tr>
            <tr><td>05/01/2025</td><td>4.35</td></tr>
          </table>
          <table>
            <tr><th>Date</th><th>10 Yr</th></tr>
            <tr><td>06/01/2025</td><td>4.50</td></tr>
          </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["date"] == "2025-05-01"

    def test_non_maturity_table_before_maturity_table_is_skipped(self):
        html = """
        <html><body>
          <table>
            <tr><th>col_a</th><th>col_b</th></tr>
            <tr><td>foo</td><td>bar</td></tr>
          </table>
          <table>
            <tr><th>Date</th><th>10 Yr</th></tr>
            <tr><td>05/01/2025</td><td>4.35</td></tr>
          </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["date"] == "2025-05-01"


class TestRunRecordStructure:
    def test_required_fields_constant_contains_date_and_maturity_10y(self):
        assert "date" in REQUIRED_FIELDS
        assert "maturity_10y" in REQUIRED_FIELDS

    def test_all_eight_maturity_fields_present_in_every_record(self, sample_html):
        for record in run(sample_html):
            for field_name in LABEL_TO_FIELD.values():
                assert field_name in record, f"Missing field {field_name}"

    def test_maturity_values_are_floats(self, sample_html):
        for record in run(sample_html):
            for field_name in LABEL_TO_FIELD.values():
                assert isinstance(record[field_name], float), (
                    f"Field {field_name} is {type(record[field_name])}, expected float"
                )

    def test_empty_cell_produces_minus_one(self):
        html = """
        <html><body><table>
          <tr><th>Date</th><th>1 Mo</th><th>10 Yr</th></tr>
          <tr><td>05/01/2025</td><td></td><td>4.40</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["maturity_1m"] == -1.0

    def test_non_numeric_rate_cell_produces_minus_one(self):
        html = """
        <html><body><table>
          <tr><th>Date</th><th>1 Mo</th><th>10 Yr</th></tr>
          <tr><td>05/01/2025</td><td>badval</td><td>4.40</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["maturity_1m"] == -1.0

    def test_missing_column_in_table_produces_minus_one(self):
        html = """
        <html><body><table>
          <tr><th>Date</th><th>10 Yr</th></tr>
          <tr><td>05/01/2025</td><td>4.40</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["maturity_1m"] == -1.0
        assert records[0]["maturity_10y"] == 4.40

    def test_date_converted_from_mm_dd_yyyy_to_iso(self):
        html = """
        <html><body><table>
          <tr><th>Date</th><th>10 Yr</th></tr>
          <tr><td>12/31/2024</td><td>4.20</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["date"] == "2024-12-31"


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_yield_curve_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body><table>
          <tr><th>Date</th><th>10 Yr</th></tr>
          <tr><td>05/01/2025</td><td>4.40</td></tr>
        </table></body></html>
        """
        with patch("src.scrapers.treasury_yield_curve.fetch", return_value=fake_resp) as mock_fetch:
            scrape()
        mock_fetch.assert_called_once()
        call_url = mock_fetch.call_args[0][0]
        assert "daily_treasury_yield_curve" in call_url
        assert "home.treasury.gov" in call_url

    def test_scrape_uses_provided_year_month_in_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body><table>
          <tr><th>Date</th><th>10 Yr</th></tr>
          <tr><td>01/15/2024</td><td>4.20</td></tr>
        </table></body></html>
        """
        with patch("src.scrapers.treasury_yield_curve.fetch", return_value=fake_resp) as mock_fetch:
            scrape(year_month="202401")
        call_url = mock_fetch.call_args[0][0]
        assert "202401" in call_url

    def test_scrape_returns_same_as_run_on_same_html(self):
        test_html = """
        <html><body><table>
          <tr><th>Date</th><th>1 Mo</th><th>3 Mo</th><th>10 Yr</th></tr>
          <tr><td>05/01/2025</td><td>5.30</td><td>5.28</td><td>4.40</td></tr>
          <tr><td>05/02/2025</td><td>5.31</td><td>5.29</td><td>4.41</td></tr>
        </table></body></html>
        """
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = test_html

        with patch("src.scrapers.treasury_yield_curve.fetch", return_value=fake_resp) as mock_fetch:
            scraped = scrape(year_month="202505")

        url = mock_fetch.call_args[0][0]
        direct = run(test_html, source_url=url)
        assert scraped == direct

    def test_scrape_source_url_matches_fetch_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><body><table>
          <tr><th>Date</th><th>10 Yr</th></tr>
          <tr><td>05/01/2025</td><td>4.40</td></tr>
        </table></body></html>
        """
        with patch("src.scrapers.treasury_yield_curve.fetch", return_value=fake_resp) as mock_fetch:
            records = scrape(year_month="202505")

        expected_url = mock_fetch.call_args[0][0]
        for record in records:
            assert record["source_url"] == expected_url
