"""Tests for src/scrapers/us_bankruptcy_filings.py.

All tests use a static HTML fixture or inline HTML — no live network calls.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.us_bankruptcy_filings import (
    SOURCE_URL,
    _parse_chapter,
    _parse_filings,
    _parse_quarter,
    _record_to_proto,
    run,
    scrape,
)
from protos.us_bankruptcy_filings_pb2 import UsCourtsBankruptcyRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "us_bankruptcy_filings.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_nine_records(self, sample_html):
        """3 data rows × 3 chapter columns (7, 11, 13) = 9 records total."""
        assert len(run(sample_html)) == 9

    def test_chapter_values_are_7_11_13(self, sample_html):
        chapters = {r["chapter"] for r in run(sample_html)}
        assert chapters == {7, 11, 13}

    def test_years_parsed_correctly(self, sample_html):
        years = {r["period_year"] for r in run(sample_html)}
        assert years == {2023, 2024}

    def test_quarters_parsed_correctly(self, sample_html):
        quarters = {r["period_quarter"] for r in run(sample_html)}
        assert quarters == {1, 2, 4}

    def test_filings_nonzero(self, sample_html):
        for r in run(sample_html):
            assert r["filings"] > 0, f"Expected positive filings, got {r['filings']}"

    def test_source_url_stored(self, sample_html):
        for r in run(sample_html, source_url="https://example.com"):
            assert r["source_url"] == "https://example.com"

    def test_total_column_excluded(self, sample_html):
        """The 'Total' column must not produce records (it is not a chapter column)."""
        chapters = {r["chapter"] for r in run(sample_html)}
        assert None not in chapters

    def test_comma_separated_filings_parsed(self, sample_html):
        """Chapter 7 Q1 2024 is 95,234 — commas must be stripped before parsing."""
        ch7_q1_2024 = [
            r for r in run(sample_html)
            if r["chapter"] == 7 and r["period_year"] == 2024 and r["period_quarter"] == 1
        ]
        assert len(ch7_q1_2024) == 1
        assert ch7_q1_2024[0]["filings"] == 95234

    def test_chapter_11_filings_present(self, sample_html):
        ch11 = [r for r in run(sample_html) if r["chapter"] == 11]
        assert len(ch11) == 3
        for r in ch11:
            assert r["filings"] > 0

    def test_chapter_13_filings_present(self, sample_html):
        ch13 = [r for r in run(sample_html) if r["chapter"] == 13]
        assert len(ch13) == 3

    def test_filings_are_integers(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["filings"], int)

    def test_years_are_integers(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["period_year"], int)

    def test_quarters_are_integers(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["period_quarter"], int)


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError, match="No bankruptcy filing records"):
            run("")

    def test_no_chapter_columns_raises_value_error(self):
        html = """
        <html><body>
        <table>
          <thead><tr><th>Year</th><th>Quarter</th><th>Total</th></tr></thead>
          <tbody><tr><td>2024</td><td>1</td><td>125000</td></tr></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No bankruptcy filing records"):
            run(html)

    def test_large_table_all_records_valid(self):
        """4 years × 4 quarters × 3 chapters = 48 records, all positive."""
        years = [2021, 2022, 2023, 2024]
        rows = ""
        for y in years:
            for q in range(1, 5):
                rows += (
                    f"<tr><td>{y}</td><td>{q}</td>"
                    f"<td>{90000 + q * 1000}</td>"
                    f"<td>{1500 + q * 100}</td>"
                    f"<td>{28000 + q * 500}</td>"
                    f"<td>{119500 + q * 1600}</td></tr>"
                )
        html = f"""
        <html><body>
        <table>
          <thead><tr><th>Year</th><th>Quarter</th>
            <th>Chapter 7</th><th>Chapter 11</th><th>Chapter 13</th><th>Total</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 48
        for r in records:
            assert r["filings"] > 0

    def test_q_format_quarter_parsed(self):
        """Quarter cells like 'Q1', 'Q2' must be accepted."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Year</th><th>Quarter</th><th>Chapter 7</th></tr></thead>
          <tbody>
            <tr><td>2024</td><td>Q3</td><td>97,500</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["period_quarter"] == 3

    def test_rowspan_year_propagated(self):
        """When the year cell is absent (rowspan), last seen year is reused."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Year</th><th>Quarter</th><th>Chapter 7</th></tr></thead>
          <tbody>
            <tr><td>2024</td><td>1</td><td>95,000</td></tr>
            <tr><td></td><td>2</td><td>102,000</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        years = {r["period_year"] for r in records}
        assert years == {2024}
        assert len(records) == 2


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_no_tables_raises_value_error(self):
        html = "<html><body><p>No tables here.</p></body></html>"
        with pytest.raises(ValueError):
            run(html)

    def test_non_numeric_filings_cell_skipped(self):
        """Cells containing 'n/a' must be skipped; valid cells in the same row kept."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Year</th><th>Quarter</th>
            <th>Chapter 7</th><th>Chapter 11</th></tr></thead>
          <tbody>
            <tr><td>2024</td><td>1</td><td>n/a</td><td>1,500</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0]["chapter"] == 11
        assert records[0]["filings"] == 1500

    def test_invalid_quarter_row_skipped(self):
        """Rows where the quarter cell contains non-quarter text produce no records."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Year</th><th>Quarter</th><th>Chapter 7</th></tr></thead>
          <tbody>
            <tr><td>2024</td><td>Annual</td><td>400,000</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError):
            run(html)

    def test_year_out_of_range_skipped(self):
        """Rows with a year outside 1900-2100 produce no records."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Year</th><th>Quarter</th><th>Chapter 7</th></tr></thead>
          <tbody>
            <tr><td>1850</td><td>1</td><td>1,000</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError):
            run(html)


# ---------------------------------------------------------------------------
# Helper-function unit tests
# ---------------------------------------------------------------------------


class TestParseChapter:
    def test_chapter_7(self):
        assert _parse_chapter("Chapter 7") == 7

    def test_chapter_11(self):
        assert _parse_chapter("Chapter 11") == 11

    def test_chapter_13(self):
        assert _parse_chapter("Chapter 13") == 13

    def test_case_insensitive(self):
        assert _parse_chapter("CHAPTER 7") == 7

    def test_no_match_returns_none(self):
        assert _parse_chapter("Total") is None

    def test_embedded_in_text(self):
        assert _parse_chapter("Filings under Chapter 12") == 12


class TestParseQuarter:
    def test_numeric_1(self):
        assert _parse_quarter("1") == 1

    def test_numeric_4(self):
        assert _parse_quarter("4") == 4

    def test_q_prefix(self):
        assert _parse_quarter("Q2") == 2

    def test_lowercase_q(self):
        assert _parse_quarter("q3") == 3

    def test_zero_returns_none(self):
        assert _parse_quarter("0") is None

    def test_five_returns_none(self):
        assert _parse_quarter("5") is None

    def test_text_returns_none(self):
        assert _parse_quarter("Annual") is None


class TestParseFilings:
    def test_plain_integer(self):
        assert _parse_filings("95234") == 95234

    def test_comma_separated(self):
        assert _parse_filings("95,234") == 95234

    def test_empty_returns_none(self):
        assert _parse_filings("") is None

    def test_non_numeric_returns_none(self):
        assert _parse_filings("n/a") is None

    def test_whitespace_stripped(self):
        assert _parse_filings("  1,532  ") == 1532


# ---------------------------------------------------------------------------
# Proto stub tests
# ---------------------------------------------------------------------------


class TestRecordToProto:
    def test_proto_fields_populated(self, sample_html):
        records = run(sample_html)
        assert records, "Need at least one record from fixture"
        msg = _record_to_proto(records[0])
        assert isinstance(msg, UsCourtsBankruptcyRecord)
        assert msg.period_year == records[0]["period_year"]
        assert msg.period_quarter == records[0]["period_quarter"]
        assert msg.chapter == records[0]["chapter"]
        assert msg.filings == records[0]["filings"]
        assert msg.source_url == records[0]["source_url"]

    def test_fetch_time_present(self, sample_html):
        records = run(sample_html)
        msg = _record_to_proto(records[0])
        assert msg.fetch_time != ""

    def test_chapter_7_proto_value(self, sample_html):
        ch7 = [r for r in run(sample_html) if r["chapter"] == 7][0]
        msg = _record_to_proto(ch7)
        assert msg.chapter == 7

    def test_filings_type_in_proto(self, sample_html):
        records = run(sample_html)
        msg = _record_to_proto(records[0])
        assert isinstance(msg.filings, int)


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.us_bankruptcy_filings.fetch", return_value=fake_resp) as mock_fetch, \
             patch("src.scrapers.us_bankruptcy_filings.time.sleep"):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with patch("src.scrapers.us_bankruptcy_filings.fetch", return_value=fake_resp), \
             patch("src.scrapers.us_bankruptcy_filings.time.sleep",
                   side_effect=lambda s: sleep_calls.append(s)):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3s; calls: {sleep_calls}"

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body>no tables</body></html>"

        with patch("src.scrapers.us_bankruptcy_filings.fetch", return_value=fake_resp), \
             patch("src.scrapers.us_bankruptcy_filings.time.sleep"):
            with pytest.raises(ValueError):
                scrape()

    def test_scrape_returns_same_as_run(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.us_bankruptcy_filings.fetch", return_value=fake_resp), \
             patch("src.scrapers.us_bankruptcy_filings.time.sleep"):
            scraped = scrape()

        direct = run(sample_html)
        assert scraped == direct
