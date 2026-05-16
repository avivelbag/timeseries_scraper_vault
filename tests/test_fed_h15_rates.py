"""Tests for src/scrapers/fed_h15_rates.py.

All tests use a static HTML fixture or inline HTML — no live network calls.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.fed_h15_rates import (
    REQUIRED_FIELDS,
    SOURCE_URL,
    _extract_frequency,
    _extract_maturity,
    _parse_date,
    _record_to_proto,
    run,
    scrape,
)
from protos.fed_h15_rates_pb2 import FedH15Record

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "fed_h15_sample.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------

class TestRunHappyPath:
    def test_returns_twelve_records(self, sample_html):
        """4 series × 3 date columns = 12 records; ND is included as -1.0."""
        assert len(run(sample_html)) == 12

    def test_period_date_is_iso8601(self, sample_html):
        """All period_date values must be YYYY-MM-DD strings."""
        import re
        iso_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for r in run(sample_html):
            assert iso_pattern.match(r["period_date"]), f"Bad date: {r['period_date']}"

    def test_non_nd_rates_positive(self, sample_html):
        for r in run(sample_html):
            if r["rate"] != -1.0:
                assert r["rate"] > 0

    def test_nd_cell_maps_to_minus_one(self, sample_html):
        """Federal funds (effective) has one ND cell in the fixture."""
        nd_records = [r for r in run(sample_html) if r["rate"] == -1.0]
        assert len(nd_records) == 1
        assert nd_records[0]["series_name"] == "Federal funds (effective)"

    def test_series_names_non_empty(self, sample_html):
        for r in run(sample_html):
            assert r["series_name"] != ""

    def test_required_fields_all_present(self, sample_html):
        required = set(REQUIRED_FIELDS)
        for r in run(sample_html):
            assert required.issubset(r.keys()), f"Missing fields in: {r}"

    def test_federal_funds_effective_present(self, sample_html):
        series_names = {r["series_name"] for r in run(sample_html)}
        assert "Federal funds (effective)" in series_names

    def test_prime_present(self, sample_html):
        series_names = {r["series_name"] for r in run(sample_html)}
        assert "Prime" in series_names

    def test_three_date_columns_used(self, sample_html):
        dates = {r["period_date"] for r in run(sample_html)}
        assert len(dates) == 3

    def test_section_dividers_skipped(self, sample_html):
        """'Federal funds' and 'Bank prime loan' colspan rows must not appear."""
        series_names = {r["series_name"] for r in run(sample_html)}
        assert "Federal funds" not in series_names
        assert "Bank prime loan" not in series_names

    def test_source_url_stored(self, sample_html):
        for r in run(sample_html, source_url="https://example.com"):
            assert r["source_url"] == "https://example.com"

    def test_maturity_extracted_for_3month(self, sample_html):
        records_3m = [
            r for r in run(sample_html)
            if "3-month" in r["series_name"] and r["maturity"] == "3m"
        ]
        assert len(records_3m) == 3

    def test_maturity_extracted_for_6month(self, sample_html):
        records_6m = [
            r for r in run(sample_html)
            if "6-month" in r["series_name"] and r["maturity"] == "6m"
        ]
        assert len(records_6m) == 3

    def test_frequency_defaults_to_daily(self, sample_html):
        for r in run(sample_html):
            assert r["frequency"] == "daily"


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------

class TestRunEdgeCases:
    def test_empty_string_returns_empty_list(self):
        assert run("") == []

    def test_no_table_returns_empty_list(self):
        html = "<html><body><p>No table here</p></body></html>"
        assert run(html) == []

    def test_table_without_thead_returns_empty_list(self):
        html = """
        <html><body>
        <table>
          <tbody>
            <tr><th>Federal funds (effective)</th><td>4.33</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        assert run(html) == []

    def test_all_nd_cells(self):
        html = """
        <html><head><title>H.15 May 14, 2025</title></head>
        <body><table>
          <thead><tr><th>Instruments</th><th>May 14, 2025</th></tr></thead>
          <tbody>
            <tr><th>Prime</th><td>ND</td></tr>
          </tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["rate"] == -1.0

    def test_large_table_all_records_valid(self):
        """Table with 10 series and 5 date columns = 50 records."""
        date_headers = "".join(
            f"<th>May {10 + i}, 2025</th>" for i in range(5)
        )
        data_rows = "".join(
            f"<tr><th>Series {j}</th>"
            + "".join(f"<td>{3.0 + j * 0.1 + i * 0.01:.2f}</td>" for i in range(5))
            + "</tr>"
            for j in range(10)
        )
        html = f"""
        <html><head><title>H.15 May 2025</title></head>
        <body><table>
          <thead><tr><th>Instruments</th>{date_headers}</tr></thead>
          <tbody>{data_rows}</tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 50
        for r in records:
            assert r["rate"] > 0
            assert r["series_name"] != ""

    def test_year_inferred_from_title(self):
        html = """
        <html><head><title>H.15 -- May 14, 2023</title></head>
        <body><table>
          <thead><tr><th>Instruments</th><th>May 14</th></tr></thead>
          <tbody>
            <tr><th>Prime</th><td>7.50</td></tr>
          </tbody>
        </table></body></html>
        """
        records = run(html)
        assert records[0]["period_date"] == "2023-05-14"

    def test_unparseable_rate_cell_skipped(self):
        """A cell containing a non-numeric, non-ND value is skipped entirely."""
        html = """
        <html><head><title>H.15 May 2025</title></head>
        <body><table>
          <thead><tr><th>Instruments</th><th>May 12, 2025</th><th>May 13, 2025</th></tr></thead>
          <tbody>
            <tr><th>Prime</th><td>n/a</td><td>7.50</td></tr>
          </tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["rate"] == 7.50


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------

class TestRunFailureModes:
    def test_row_with_no_td_cells_produces_no_records(self):
        """A divider-only row must contribute zero records."""
        html = """
        <html><head><title>H.15 May 2025</title></head>
        <body><table>
          <thead><tr><th>Instruments</th><th>May 12, 2025</th></tr></thead>
          <tbody>
            <tr><th colspan="2">Federal funds</th></tr>
          </tbody>
        </table></body></html>
        """
        assert run(html) == []

    def test_empty_series_name_row_skipped(self):
        """A row where the <th> is blank must produce no records."""
        html = """
        <html><head><title>H.15 May 2025</title></head>
        <body><table>
          <thead><tr><th>Instruments</th><th>May 12, 2025</th></tr></thead>
          <tbody>
            <tr><th></th><td>7.50</td></tr>
          </tbody>
        </table></body></html>
        """
        assert run(html) == []

    def test_malformed_date_column_is_skipped(self):
        """An unrecognised date header produces no records for that column."""
        html = """
        <html><head><title>H.15 May 2025</title></head>
        <body><table>
          <thead><tr><th>Instruments</th><th>not-a-date</th><th>May 12, 2025</th></tr></thead>
          <tbody>
            <tr><th>Prime</th><td>7.50</td><td>7.51</td></tr>
          </tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["period_date"] == "2025-05-12"


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------

class TestParseDate:
    def test_full_format_with_year(self):
        assert _parse_date("May 12, 2025", 2024) == "2025-05-12"

    def test_short_format_uses_fallback_year(self):
        assert _parse_date("May 12", 2025) == "2025-05-12"

    def test_invalid_returns_none(self):
        assert _parse_date("not-a-date", 2025) is None

    def test_single_digit_day(self):
        assert _parse_date("January 5, 2025", 2024) == "2025-01-05"


class TestExtractMaturity:
    def test_3_month(self):
        assert _extract_maturity("Treasury bills 3-month") == "3m"

    def test_6_month(self):
        assert _extract_maturity("6-month T-bill") == "6m"

    def test_1_year(self):
        assert _extract_maturity("1-year Treasury") == "1y"

    def test_10_yr(self):
        assert _extract_maturity("10-yr constant maturity") == "10y"

    def test_no_maturity(self):
        assert _extract_maturity("Federal funds (effective)") == ""

    def test_prime_no_maturity(self):
        assert _extract_maturity("Prime") == ""


class TestExtractFrequency:
    def test_daily_default(self):
        assert _extract_frequency("Federal funds (effective)") == "daily"

    def test_weekly_detected(self):
        assert _extract_frequency("weekly average rate") == "weekly"

    def test_monthly_detected(self):
        assert _extract_frequency("Monthly prime rate") == "monthly"

    def test_case_insensitive(self):
        assert _extract_frequency("WEEKLY series") == "weekly"


# ---------------------------------------------------------------------------
# Proto stub tests
# ---------------------------------------------------------------------------

class TestRecordToProto:
    def test_proto_fields_populated(self, sample_html):
        records = run(sample_html)
        assert records, "Need at least one record"
        msg = _record_to_proto(records[0])
        assert isinstance(msg, FedH15Record)
        assert msg.period_date == records[0]["period_date"]
        assert msg.series_name == records[0]["series_name"]
        assert msg.rate == records[0]["rate"]
        assert msg.source_url == records[0]["source_url"]
        assert msg.fetch_time != ""

    def test_nd_proto_rate_is_minus_one(self, sample_html):
        nd = [r for r in run(sample_html) if r["rate"] == -1.0]
        assert nd
        msg = _record_to_proto(nd[0])
        assert msg.rate == -1.0


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------

class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><head><title>H.15 May 2025</title></head>
        <body><table>
          <thead><tr><th>Instruments</th><th>May 12, 2025</th></tr></thead>
          <tbody>
            <tr><th>Prime</th><td>7.50</td></tr>
          </tbody>
        </table></body></html>
        """
        with patch("src.scrapers.fed_h15_rates.fetch", return_value=fake_resp) as mock_fetch, \
             patch("src.scrapers.fed_h15_rates.time.sleep"):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) == 1

    def test_scrape_sleeps_at_least_3_seconds(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = """
        <html><head><title>H.15 May 2025</title></head>
        <body><table>
          <thead><tr><th>Instruments</th><th>May 12, 2025</th></tr></thead>
          <tbody>
            <tr><th>Prime</th><td>7.50</td></tr>
          </tbody>
        </table></body></html>
        """
        sleep_calls: list[float] = []
        with patch("src.scrapers.fed_h15_rates.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_h15_rates.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3s; calls: {sleep_calls}"

    def test_scrape_returns_same_as_run(self):
        test_html = """
        <html><head><title>H.15 May 2025</title></head>
        <body><table>
          <thead><tr><th>Instruments</th><th>May 12, 2025</th><th>May 13, 2025</th></tr></thead>
          <tbody>
            <tr><th>Prime</th><td>7.50</td><td>7.50</td></tr>
            <tr><th>Federal funds (effective)</th><td>4.33</td><td>ND</td></tr>
          </tbody>
        </table></body></html>
        """
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = test_html

        with patch("src.scrapers.fed_h15_rates.fetch", return_value=fake_resp), \
             patch("src.scrapers.fed_h15_rates.time.sleep"):
            scraped = scrape()

        direct = run(test_html)
        assert scraped == direct
