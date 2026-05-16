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
    _header_to_field,
    _iter_months,
    backfill,
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


class TestHeaderDrift:
    """Header label variations the Treasury site might emit (layout drift)."""

    def test_canonical_labels_resolve(self):
        for label, field in LABEL_TO_FIELD.items():
            assert _header_to_field(label) == field

    def test_hyphenated_label_resolves(self):
        assert _header_to_field("10-Yr") == "maturity_10y"
        assert _header_to_field("1-Mo") == "maturity_1m"
        assert _header_to_field("30-Yr") == "maturity_30y"

    def test_no_space_label_resolves(self):
        assert _header_to_field("10Yr") == "maturity_10y"
        assert _header_to_field("1Mo") == "maturity_1m"
        assert _header_to_field("5Yr") == "maturity_5y"

    def test_uppercase_label_resolves(self):
        assert _header_to_field("10 YR") == "maturity_10y"
        assert _header_to_field("1 MO") == "maturity_1m"

    def test_unknown_label_returns_none(self):
        assert _header_to_field("Unknown") is None
        assert _header_to_field("") is None
        assert _header_to_field("4 Mo") is None

    def test_run_parses_hyphenated_headers(self):
        html = """
        <html><body><table>
          <tr><th>Date</th><th>1-Mo</th><th>10-Yr</th></tr>
          <tr><td>05/01/2025</td><td>5.31</td><td>4.40</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["maturity_1m"] == 5.31
        assert records[0]["maturity_10y"] == 4.40

    def test_run_parses_compact_headers(self):
        html = """
        <html><body><table>
          <tr><th>Date</th><th>1Mo</th><th>10Yr</th><th>30Yr</th></tr>
          <tr><td>05/02/2025</td><td>5.32</td><td>4.41</td><td>4.62</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["maturity_1m"] == 5.32
        assert records[0]["maturity_10y"] == 4.41
        assert records[0]["maturity_30y"] == 4.62

    def test_run_mixed_canonical_and_drifted_headers(self):
        html = """
        <html><body><table>
          <tr><th>Date</th><th>1 Mo</th><th>10-Yr</th></tr>
          <tr><td>05/03/2025</td><td>5.30</td><td>4.39</td></tr>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["maturity_1m"] == 5.30
        assert records[0]["maturity_10y"] == 4.39


class TestIterMonths:
    def test_single_month(self):
        assert list(_iter_months("2025-01", "2025-01")) == ["202501"]

    def test_two_months_same_year(self):
        assert list(_iter_months("2025-01", "2025-02")) == ["202501", "202502"]

    def test_year_boundary(self):
        result = list(_iter_months("2024-12", "2025-02"))
        assert result == ["202412", "202501", "202502"]

    def test_full_year(self):
        result = list(_iter_months("2024-01", "2024-12"))
        assert len(result) == 12
        assert result[0] == "202401"
        assert result[-1] == "202412"

    def test_end_before_start_yields_nothing(self):
        assert list(_iter_months("2025-03", "2025-01")) == []

    def test_multi_year_span(self):
        result = list(_iter_months("2023-11", "2024-01"))
        assert result == ["202311", "202312", "202401"]


class TestBackfill:
    def _make_fake_resp(self, date_str: str, rate: str = "4.40") -> MagicMock:
        fake = MagicMock(spec=requests.Response)
        fake.text = f"""
        <html><body><table>
          <tr><th>Date</th><th>10 Yr</th></tr>
          <tr><td>{date_str}</td><td>{rate}</td></tr>
        </table></body></html>
        """
        return fake

    def test_backfill_single_month_returns_records(self):
        fake_resp = self._make_fake_resp("01/15/2024")
        with patch("src.scrapers.treasury_yield_curve.fetch", return_value=fake_resp):
            records = backfill("2024-01", "2024-01")
        assert len(records) == 1
        assert records[0]["date"] == "2024-01-15"

    def test_backfill_two_months_calls_fetch_twice(self):
        fake_resp = self._make_fake_resp("01/15/2024")
        with patch("src.scrapers.treasury_yield_curve.fetch", return_value=fake_resp) as mock_fetch:
            backfill("2024-01", "2024-02")
        assert mock_fetch.call_count == 2

    def test_backfill_aggregates_records_from_all_months(self):
        jan_resp = self._make_fake_resp("01/15/2024", "4.20")
        feb_resp = self._make_fake_resp("02/15/2024", "4.25")
        with patch("src.scrapers.treasury_yield_curve.fetch", side_effect=[jan_resp, feb_resp]):
            records = backfill("2024-01", "2024-02")
        assert len(records) == 2
        dates = {r["date"] for r in records}
        assert "2024-01-15" in dates
        assert "2024-02-15" in dates

    def test_backfill_skips_month_on_fetch_error(self):
        good_resp = self._make_fake_resp("03/15/2024", "4.30")
        with patch(
            "src.scrapers.treasury_yield_curve.fetch",
            side_effect=[RuntimeError("robots.txt disallows"), good_resp],
        ):
            records = backfill("2024-02", "2024-03")
        assert len(records) == 1
        assert records[0]["date"] == "2024-03-15"

    def test_backfill_end_before_start_returns_empty(self):
        with patch("src.scrapers.treasury_yield_curve.fetch") as mock_fetch:
            records = backfill("2024-03", "2024-01")
        assert records == []
        mock_fetch.assert_not_called()

    def test_backfill_year_boundary(self):
        dec_resp = self._make_fake_resp("12/15/2023", "3.90")
        jan_resp = self._make_fake_resp("01/15/2024", "4.00")
        with patch("src.scrapers.treasury_yield_curve.fetch", side_effect=[dec_resp, jan_resp]):
            records = backfill("2023-12", "2024-01")
        assert len(records) == 2
