"""Tests for src/scrapers/fed_z1_financial_accounts.py.

All tests use static HTML fixtures or inline HTML — zero live network calls.
The fixture contains 10 series × 3 quarters = 30 records. Period dates cover
2024-Q1, 2024-Q2, and 2024-Q3. Several cells carry "r" (revised) and "p"
(preliminary) suffixes that must be stripped before float parsing.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protos.fed_z1_financial_accounts_pb2 import FedZ1Record
from src.scrapers.fed_z1_financial_accounts import (
    SOURCE_URL,
    _heading_matches,
    _normalize_period,
    _parse_value,
    run,
    scrape,
)

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "fed_z1_financial_accounts.html"
)

_PERIOD_PATTERN = re.compile(r"^\d{4}-Q[1-4]$")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_at_least_eight_records(self, sample_html):
        records = run(sample_html)
        assert len(records) >= 8

    def test_exact_record_count(self, sample_html):
        """Fixture has 10 series × 3 quarters = 30 records."""
        records = run(sample_html)
        assert len(records) == 30

    def test_period_dates_match_yyyy_qn(self, sample_html):
        for rec in run(sample_html):
            assert _PERIOD_PATTERN.match(rec.period_date), (
                f"Bad period_date: {rec.period_date!r}"
            )

    def test_all_three_quarters_present(self, sample_html):
        dates = {rec.period_date for rec in run(sample_html)}
        assert "2024-Q1" in dates
        assert "2024-Q2" in dates
        assert "2024-Q3" in dates

    def test_value_billions_usd_nonzero_float(self, sample_html):
        for rec in run(sample_html):
            assert isinstance(rec.value_billions_usd, float)
            assert rec.value_billions_usd != 0.0

    def test_household_net_worth_series_present(self, sample_html):
        names = {rec.series_name for rec in run(sample_html)}
        assert any("net worth" in n.lower() for n in names)

    def test_total_nonfinancial_debt_present(self, sample_html):
        names = {rec.series_name for rec in run(sample_html)}
        assert any("nonfinancial debt" in n.lower() for n in names)

    def test_revised_suffix_stripped_correctly(self, sample_html):
        """2024-Q3 household net worth fixture value is 168,732.5r → 168732.5."""
        records = run(sample_html)
        match = [
            r for r in records
            if "net worth" in r.series_name.lower() and r.period_date == "2024-Q3"
        ]
        assert len(match) == 1
        assert match[0].value_billions_usd == pytest.approx(168732.5)

    def test_preliminary_suffix_stripped_correctly(self, sample_html):
        """2024-Q3 total nonfinancial debt is 37,432.6p → 37432.6."""
        records = run(sample_html)
        match = [
            r for r in records
            if "total nonfinancial debt" in r.series_name.lower()
            and r.period_date == "2024-Q3"
        ]
        assert len(match) == 1
        assert match[0].value_billions_usd == pytest.approx(37432.6)

    def test_source_url_stored_in_all_records(self, sample_html):
        for rec in run(sample_html, source_url="https://example.com"):
            assert rec.source_url == "https://example.com"

    def test_fetch_time_is_iso8601(self, sample_html):
        for rec in run(sample_html):
            assert "T" in rec.fetch_time
            assert rec.fetch_time != ""

    def test_all_records_are_fed_z1_record_instances(self, sample_html):
        for rec in run(sample_html):
            assert isinstance(rec, FedZ1Record)

    def test_series_names_are_nonempty_strings(self, sample_html):
        for rec in run(sample_html):
            assert isinstance(rec.series_name, str)
            assert rec.series_name != ""


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_table_with_colon_period_format(self):
        """Period header '2023:Q4' must normalize to '2023-Q4'."""
        html = """
        <html><body>
        <h3>Household Net Worth</h3>
        <table>
          <thead><tr><th>Series</th><th>2023:Q4</th></tr></thead>
          <tbody>
            <tr><td>Net worth</td><td>150,000.0</td></tr>
            <tr><td>Total assets</td><td>170,000.0</td></tr>
            <tr><td>Real assets</td><td>45,000.0</td></tr>
            <tr><td>Financial assets</td><td>125,000.0</td></tr>
            <tr><td>Total liabilities</td><td>20,000.0</td></tr>
            <tr><td>Mortgage debt</td><td>13,000.0</td></tr>
            <tr><td>Consumer credit</td><td>4,800.0</td></tr>
            <tr><td>Total nonfinancial debt</td><td>35,000.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert all(rec.period_date == "2023-Q4" for rec in records)
        assert len(records) == 8

    def test_suppressed_na_cells_skipped(self):
        """Cells containing 'n.a.' must not generate records."""
        html = """
        <html><body>
        <h3>Household Net Worth</h3>
        <table>
          <thead><tr><th>Series</th><th>2024:Q1</th><th>2024:Q2</th></tr></thead>
          <tbody>
            <tr><td>Net worth</td><td>156,272.4</td><td>n.a.</td></tr>
            <tr><td>Total assets</td><td>178,312.8</td><td>186,120.3</td></tr>
            <tr><td>Real assets</td><td>47,126.5</td><td>48,013.2</td></tr>
            <tr><td>Financial assets</td><td>n.a.</td><td>138,107.1</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        dates = [(r.series_name, r.period_date) for r in records]
        assert ("Net worth", "2024-Q2") not in dates
        assert ("Financial assets", "2024-Q1") not in dates
        assert ("Net worth", "2024-Q1") in dates
        assert ("Total assets", "2024-Q2") in dates

    def test_parenthetical_negative_parsed_correctly(self):
        """Values in the form '(1,234.5)' must parse as -1234.5."""
        html = """
        <html><body>
        <h3>Household Net Worth</h3>
        <table>
          <thead><tr><th>Series</th><th>2024:Q1</th></tr></thead>
          <tbody>
            <tr><td>Net worth deficit</td><td>(5,000.0)</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0].value_billions_usd == pytest.approx(-5000.0)

    def test_table_without_period_headers_skipped(self):
        """A table with no parseable quarter columns raises ValueError."""
        html = """
        <html><body>
        <h3>Household Net Worth</h3>
        <table>
          <thead><tr><th>Series</th><th>Jan 2024</th></tr></thead>
          <tbody><tr><td>Net worth</td><td>156,272.4</td></tr></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)

    def test_large_fixture_all_rows_parsed(self):
        """20 series × 4 quarters = 80 records."""
        header = "<tr><th>Series</th>" + "".join(
            f"<th>2023:Q{q}</th>" for q in range(1, 5)
        ) + "</tr>"
        rows = "".join(
            f"<tr><td>Series {i}</td>" + "".join(
                f"<td>{10000 + i * 10 + q}.0</td>" for q in range(1, 5)
            ) + "</tr>"
            for i in range(1, 21)
        )
        html = f"""
        <html><body>
        <h3>Household Net Worth</h3>
        <table>
          <thead>{header}</thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 80
        for rec in records:
            assert _PERIOD_PATTERN.match(rec.period_date)
            assert rec.value_billions_usd > 0


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_empty_html_raises_value_error(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("")

    def test_whitespace_html_raises_value_error(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("   \n\t  ")

    def test_no_tables_raises_value_error(self):
        with pytest.raises(ValueError, match="No tables"):
            run("<html><body><p>No data.</p></body></html>")

    def test_tables_with_no_parseable_data_raises_value_error(self):
        """Tables with no valid quarter headers and no data cells raise ValueError."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Label</th><th>Details</th></tr></thead>
          <tbody><tr><td>Some text</td><td>n.a.</td></tr></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)


# ---------------------------------------------------------------------------
# _normalize_period unit tests
# ---------------------------------------------------------------------------


class TestNormalizePeriod:
    def test_colon_format(self):
        assert _normalize_period("2024:Q3") == "2024-Q3"

    def test_dash_format(self):
        assert _normalize_period("2024-Q3") == "2024-Q3"

    def test_q1(self):
        assert _normalize_period("2023:Q1") == "2023-Q1"

    def test_q4(self):
        assert _normalize_period("2022:Q4") == "2022-Q4"

    def test_invalid_returns_none(self):
        assert _normalize_period("Jan 2024") is None
        assert _normalize_period("2024-01") is None
        assert _normalize_period("") is None


# ---------------------------------------------------------------------------
# _parse_value unit tests
# ---------------------------------------------------------------------------


class TestParseValue:
    def test_plain_float(self):
        assert _parse_value("156,272.4") == pytest.approx(156272.4)

    def test_revised_marker_stripped(self):
        assert _parse_value("168,732.5r") == pytest.approx(168732.5)

    def test_preliminary_marker_stripped(self):
        assert _parse_value("37,432.6p") == pytest.approx(37432.6)

    def test_na_returns_none(self):
        assert _parse_value("n.a.") is None

    def test_blank_returns_none(self):
        assert _parse_value("") is None

    def test_dash_returns_none(self):
        assert _parse_value("--") is None

    def test_parenthetical_negative(self):
        assert _parse_value("(1,234.5)") == pytest.approx(-1234.5)

    def test_dollar_sign_stripped(self):
        assert _parse_value("$1,000.0") == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# _heading_matches unit tests
# ---------------------------------------------------------------------------


class TestHeadingMatches:
    def test_household_keyword(self):
        assert _heading_matches("Household Net Worth") is True

    def test_net_worth_keyword(self):
        assert _heading_matches("Total Net Worth and Assets") is True

    def test_nonfinancial_debt_keyword(self):
        assert _heading_matches("Nonfinancial Debt Outstanding") is True

    def test_case_insensitive(self):
        assert _heading_matches("HOUSEHOLD BALANCE SHEET") is True

    def test_unrelated_heading(self):
        assert _heading_matches("Consumer Price Index") is False


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch(
                "src.scrapers.fed_z1_financial_accounts.fetch", return_value=fake_resp
            ) as mock_fetch,
            patch("src.scrapers.fed_z1_financial_accounts.time.sleep"),
        ):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with (
            patch("src.scrapers.fed_z1_financial_accounts.fetch", return_value=fake_resp),
            patch(
                "src.scrapers.fed_z1_financial_accounts.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >=3s; calls: {sleep_calls}"

    def test_scrape_returns_fed_z1_records(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.fed_z1_financial_accounts.fetch", return_value=fake_resp),
            patch("src.scrapers.fed_z1_financial_accounts.time.sleep"),
        ):
            records = scrape()

        assert all(isinstance(r, FedZ1Record) for r in records)

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>no tables</p></body></html>"

        with (
            patch("src.scrapers.fed_z1_financial_accounts.fetch", return_value=fake_resp),
            patch("src.scrapers.fed_z1_financial_accounts.time.sleep"),
        ):
            with pytest.raises(ValueError):
                scrape()
