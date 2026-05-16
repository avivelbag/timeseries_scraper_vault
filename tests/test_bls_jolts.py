"""Tests for src/scrapers/bls_jolts.py.

All tests use a static HTML fixture — no live network calls are made.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.bls_jolts import (
    parse_jolts_table,
    _parse_period,
    _record_to_proto,
    _T01_URL,
    JOLTS_TABLES,
    fetch_jolts,
)
from protos.bls_jolts_pb2 import BlsJoltsRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "bls_jolts_t01.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


class TestParsePeriod:
    def test_standard_month_year(self):
        assert _parse_period("Nov 2024") == "2024-11"

    def test_preliminary_marker_stripped(self):
        assert _parse_period("Jan 2025(p)") == "2025-01"

    def test_all_twelve_months(self):
        expected = {
            "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
            "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
            "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
        }
        for month, num in expected.items():
            assert _parse_period(f"{month} 2024") == f"2024-{num}"

    def test_empty_string_returns_none(self):
        assert _parse_period("") is None

    def test_year_only_returns_none(self):
        assert _parse_period("2024") is None

    def test_nonsense_text_returns_none(self):
        assert _parse_period("not a date") is None

    def test_period_with_space_before_marker(self):
        assert _parse_period("Mar 2025 (p)") == "2025-03"


class TestParseJoltsTableHappyPath:
    def test_returns_at_least_10_records(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        assert len(records) >= 10

    def test_period_format_is_yyyy_mm(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        for record in records:
            assert re.match(r"^\d{4}-\d{2}$", record["period"]), \
                f"Invalid period: {record['period']}"

    def test_no_none_level_thousands(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        for record in records:
            assert record["level_thousands"] is not None

    def test_all_data_type_job_openings(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        for record in records:
            assert record["data_type"] == "job_openings"

    def test_source_url_stored_in_all_records(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        for record in records:
            assert record["source_url"] == _T01_URL

    def test_level_thousands_positive_floats(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        for record in records:
            assert isinstance(record["level_thousands"], float)
            assert record["level_thousands"] > 0

    def test_rate_pct_is_float(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        for record in records:
            assert isinstance(record["rate_pct"], float)

    def test_industry_field_nonempty(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        for record in records:
            assert record["industry"], "industry must not be empty"

    def test_series_id_contains_data_type(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        for record in records:
            assert "job_openings" in record["series_id"]

    def test_fixture_contains_preliminary_period(self, sample_html):
        """Preliminary periods like 'Jan 2025(p)' must parse to YYYY-MM correctly."""
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        periods = {r["period"] for r in records}
        assert "2025-01" in periods, "Expected 2025-01 from 'Jan 2025(p)' in fixture"

    def test_fixture_multiple_industries(self, sample_html):
        """Fixture has Total nonfarm, Total private, Government — all should appear."""
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        industries = {r["industry"] for r in records}
        assert len(industries) >= 2, f"Expected ≥2 industries, got: {industries}"

    def test_known_value_nov_2024_total_nonfarm(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        nov_total = [
            r for r in records
            if r["period"] == "2024-11" and r["industry"] == "Total nonfarm"
        ]
        assert len(nov_total) == 1
        assert abs(nov_total[0]["level_thousands"] - 8098.0) < 1.0
        assert abs(nov_total[0]["rate_pct"] - 4.9) < 0.01


class TestParseJoltsTableEdgeCases:
    def test_empty_html_returns_empty_list(self):
        assert parse_jolts_table("", _T01_URL, "job_openings") == []

    def test_html_without_table_returns_empty_list(self):
        html = "<html><body><p>No table here</p></body></html>"
        assert parse_jolts_table(html, _T01_URL, "job_openings") == []

    def test_single_row_header_returns_empty_or_no_crash(self):
        """A single-row thead cannot form a valid two-row column map; no crash."""
        html = """<html><body>
        <table>
          <thead><tr><th>Period</th><th>Level</th><th>Rate</th></tr></thead>
          <tbody><tr><td>Jan 2025</td><td>7000</td><td>4.5</td></tr></tbody>
        </table>
        </body></html>"""
        result = parse_jolts_table(html, _T01_URL, "job_openings")
        assert isinstance(result, list)

    def test_preliminary_period_parses_correctly(self):
        html = """<html><body>
        <table>
          <thead>
            <tr><th rowspan="2">Period</th><th colspan="2">Total nonfarm</th></tr>
            <tr><th>Level (in thousands)</th><th>Rate</th></tr>
          </thead>
          <tbody>
            <tr><td>Jan 2025(p)</td><td>7,500</td><td>4.6</td></tr>
          </tbody>
        </table>
        </body></html>"""
        records = parse_jolts_table(html, _T01_URL, "job_openings")
        assert len(records) == 1
        assert records[0]["period"] == "2025-01"

    def test_comma_in_level_stripped(self):
        html = """<html><body>
        <table>
          <thead>
            <tr><th rowspan="2">Period</th><th colspan="2">Total nonfarm</th></tr>
            <tr><th>Level (in thousands)</th><th>Rate</th></tr>
          </thead>
          <tbody>
            <tr><td>Mar 2025</td><td>7,192</td><td>4.4</td></tr>
          </tbody>
        </table>
        </body></html>"""
        records = parse_jolts_table(html, _T01_URL, "job_openings")
        assert len(records) == 1
        assert records[0]["level_thousands"] == 7192.0

    def test_large_input_all_records_valid(self):
        rows = "\n".join(
            f"<tr><td>Jan {2000 + i}</td><td>{7000 + i}</td><td>4.5</td></tr>"
            for i in range(50)
        )
        html = f"""<html><body>
        <table>
          <thead>
            <tr><th rowspan="2">Period</th><th colspan="2">Total nonfarm</th></tr>
            <tr><th>Level (in thousands)</th><th>Rate</th></tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>"""
        records = parse_jolts_table(html, _T01_URL, "job_openings")
        assert len(records) == 50
        for r in records:
            assert r["level_thousands"] is not None
            assert re.match(r"^\d{4}-\d{2}$", r["period"])

    def test_row_with_invalid_period_skipped(self):
        html = """<html><body>
        <table>
          <thead>
            <tr><th rowspan="2">Period</th><th colspan="2">Total nonfarm</th></tr>
            <tr><th>Level (in thousands)</th><th>Rate</th></tr>
          </thead>
          <tbody>
            <tr><td>not-a-period</td><td>7000</td><td>4.5</td></tr>
            <tr><td>Feb 2025</td><td>7200</td><td>4.4</td></tr>
          </tbody>
        </table>
        </body></html>"""
        records = parse_jolts_table(html, _T01_URL, "job_openings")
        assert len(records) == 1
        assert records[0]["period"] == "2025-02"

    def test_footnote_superscripts_in_level_stripped(self):
        html = """<html><body>
        <table>
          <thead>
            <tr><th rowspan="2">Period</th><th colspan="2">Total nonfarm</th></tr>
            <tr><th>Level (in thousands)</th><th>Rate</th></tr>
          </thead>
          <tbody>
            <tr><td>Dec 2024</td><td>8,000<sup>1</sup></td><td>4.9</td></tr>
          </tbody>
        </table>
        </body></html>"""
        records = parse_jolts_table(html, _T01_URL, "job_openings")
        assert len(records) == 1
        assert records[0]["level_thousands"] == 8000.0


class TestRecordToProto:
    def test_all_fields_populated_from_dict(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        assert records, "need at least one record"
        msg = _record_to_proto(records[0])
        assert isinstance(msg, BlsJoltsRecord)
        assert msg.series_id != ""
        assert msg.period != ""
        assert msg.data_type == "job_openings"
        assert msg.industry != ""
        assert msg.level_thousands > 0
        assert msg.source_url == _T01_URL

    def test_fetch_time_is_set(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        msg = _record_to_proto(records[0])
        assert msg.fetch_time._dt is not None

    def test_period_format_preserved_in_proto(self, sample_html):
        records = parse_jolts_table(sample_html, _T01_URL, "job_openings")
        for record in records:
            msg = _record_to_proto(record)
            assert re.match(r"^\d{4}-\d{2}$", msg.period)


class TestFetchJolts:
    def _make_simple_html(self) -> str:
        return """<html><body>
        <table>
          <thead>
            <tr><th rowspan="2">Period</th><th colspan="2">Total nonfarm</th></tr>
            <tr><th>Level (in thousands)</th><th>Rate</th></tr>
          </thead>
          <tbody>
            <tr><td>Jan 2025</td><td>7500</td><td>4.5</td></tr>
          </tbody>
        </table>
        </body></html>"""

    def test_calls_fetch_for_each_table_url(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = self._make_simple_html()

        with patch("src.scrapers.bls_jolts.fetch", return_value=fake_resp) as mock_fetch, \
             patch("src.scrapers.bls_jolts.time.sleep"), \
             patch("src.scrapers.bls_jolts.upload_rows", return_value=3):
            fetch_jolts()

        assert mock_fetch.call_count == len(JOLTS_TABLES)

    def test_sleeps_at_least_3s_after_each_fetch(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = self._make_simple_html()

        sleep_calls: list[float] = []
        with patch("src.scrapers.bls_jolts.fetch", return_value=fake_resp), \
             patch("src.scrapers.bls_jolts.time.sleep", side_effect=sleep_calls.append), \
             patch("src.scrapers.bls_jolts.upload_rows", return_value=3):
            fetch_jolts()

        assert len(sleep_calls) == len(JOLTS_TABLES)
        assert all(s >= 3 for s in sleep_calls)

    def test_upload_called_once_with_merged_records(self):
        """All records from all three tables are merged into one upload call."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = self._make_simple_html()

        with patch("src.scrapers.bls_jolts.fetch", return_value=fake_resp), \
             patch("src.scrapers.bls_jolts.time.sleep"), \
             patch("src.scrapers.bls_jolts.upload_rows", return_value=3) as mock_upload:
            fetch_jolts()

        mock_upload.assert_called_once()
        uploaded_rows = mock_upload.call_args[0][1]
        # 3 tables × 1 period × 1 industry = 3 BlsJoltsRecord instances
        assert len(uploaded_rows) == 3
        assert all(isinstance(r, BlsJoltsRecord) for r in uploaded_rows)

    def test_empty_html_per_table_still_calls_upload(self):
        """No records extracted still results in upload being called (with empty list)."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>No table</p></body></html>"

        with patch("src.scrapers.bls_jolts.fetch", return_value=fake_resp), \
             patch("src.scrapers.bls_jolts.time.sleep"), \
             patch("src.scrapers.bls_jolts.upload_rows", return_value=0) as mock_upload:
            fetch_jolts()

        mock_upload.assert_called_once_with("bls_jolts", [])
