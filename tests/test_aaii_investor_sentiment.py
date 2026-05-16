"""Tests for src/scrapers/aaii_investor_sentiment.py.

All tests use the static fixture at tests/fixtures/aaii_sentiment.html or
inline HTML — zero live network calls. The fixture contains 8 weekly rows
spanning 2025-03-27 through 2025-05-15.
"""

import math
import os
import re
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protos.aaii_investor_sentiment_pb2 import AaiiInvestorSentimentRecord
from src.scrapers.aaii_investor_sentiment import (
    SOURCE_URL,
    _parse_date,
    _parse_pct,
    run,
    scrape,
)

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "aaii_sentiment.html"
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_correct_row_count(self, sample_html):
        """Acceptance criterion: fixture has 8 data rows, all must be parsed."""
        records = run(sample_html)
        assert len(records) == 8

    def test_all_records_correct_type(self, sample_html):
        for rec in run(sample_html):
            assert isinstance(rec, AaiiInvestorSentimentRecord)

    def test_pct_fields_are_floats_in_range(self, sample_html):
        """Acceptance criterion: pct fields are floats in [0, 100]."""
        for rec in run(sample_html):
            for field_val in (rec.bullish_pct, rec.neutral_pct, rec.bearish_pct):
                assert isinstance(field_val, float)
                assert 0.0 <= field_val <= 100.0, (
                    f"pct field {field_val} out of [0,100] for date {rec.date}"
                )

    def test_bull_bear_spread_equals_bullish_minus_bearish(self, sample_html):
        """Acceptance criterion: bull_bear_spread == bullish_pct - bearish_pct."""
        for rec in run(sample_html):
            expected = rec.bullish_pct - rec.bearish_pct
            assert math.isclose(rec.bull_bear_spread, expected, abs_tol=0.5), (
                f"date={rec.date}: spread={rec.bull_bear_spread} != "
                f"bullish-bearish={expected}"
            )

    def test_dates_parse_to_valid_iso_dates(self, sample_html):
        """Acceptance criterion: date parses to a valid date."""
        for rec in run(sample_html):
            assert _DATE_RE.match(rec.date), (
                f"date {rec.date!r} does not match YYYY-MM-DD"
            )
            year, month, day = rec.date.split("-")
            date(int(year), int(month), int(day))

    def test_first_row_values(self, sample_html):
        records = run(sample_html)
        first = records[0]
        assert first.date == "2025-05-15"
        assert first.bullish_pct == pytest.approx(40.0)
        assert first.neutral_pct == pytest.approx(30.9)
        assert first.bearish_pct == pytest.approx(29.1)
        assert first.bull_bear_spread == pytest.approx(10.9)
        assert first.bullish_average == pytest.approx(38.0)
        assert first.bearish_average == pytest.approx(30.5)

    def test_negative_spread_row(self, sample_html):
        """Bearish-dominated weeks produce a negative bull_bear_spread."""
        records = run(sample_html)
        bearish_rows = [r for r in records if r.bull_bear_spread < 0]
        assert len(bearish_rows) >= 1

    def test_source_url_stored(self, sample_html):
        for rec in run(sample_html, source_url="https://example.com/aaii"):
            assert rec.source_url == "https://example.com/aaii"

    def test_fetch_time_is_iso8601(self, sample_html):
        for rec in run(sample_html):
            assert "T" in rec.fetch_time and rec.fetch_time != ""

    def test_pct_fields_sum_near_100(self, sample_html):
        """bullish + neutral + bearish must sum within 1% of 100 for each row."""
        for rec in run(sample_html):
            total = rec.bullish_pct + rec.neutral_pct + rec.bearish_pct
            assert math.isclose(total, 100.0, abs_tol=1.0), (
                f"date={rec.date}: sum={total}"
            )


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_rows_with_invalid_sum_are_discarded(self):
        """Rows whose pct fields don't sum to 100 ± 1 are silently skipped."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr><th>Date</th><th>Bullish</th><th>Neutral</th><th>Bearish</th>
                <th>Total</th><th>Bull-Bear Spread</th><th>8-week Mov Avg</th>
                <th>Bull Average</th><th>Bear Average</th></tr>
          </thead>
          <tbody>
            <tr><td>5/15/25</td><td>40.0%</td><td>30.0%</td><td>29.0%</td>
                <td>99.0%</td><td>11.0%</td><td>35.0%</td><td>38.0%</td><td>30.5%</td></tr>
            <tr><td>5/8/25</td><td>50.0%</td><td>50.0%</td><td>50.0%</td>
                <td>150.0%</td><td>0.0%</td><td>35.0%</td><td>38.0%</td><td>30.5%</td></tr>
            <tr><td>5/1/25</td><td>38.0%</td><td>31.0%</td><td>31.0%</td>
                <td>100.0%</td><td>7.0%</td><td>35.0%</td><td>38.0%</td><td>30.5%</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 2
        assert records[0].date == "2025-05-15"
        assert records[1].date == "2025-05-01"

    def test_na_in_averages_becomes_zero(self):
        """N/A in Bull Average / Bear Average columns defaults to 0.0."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr><th>Date</th><th>Bullish</th><th>Neutral</th><th>Bearish</th>
                <th>Total</th><th>Bull-Bear Spread</th><th>8-week Mov Avg</th>
                <th>Bull Average</th><th>Bear Average</th></tr>
          </thead>
          <tbody>
            <tr><td>5/15/25</td><td>40.0%</td><td>30.0%</td><td>30.0%</td>
                <td>100.0%</td><td>10.0%</td><td>35.0%</td><td>N/A</td><td>N/A</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0].bullish_average == pytest.approx(0.0)
        assert records[0].bearish_average == pytest.approx(0.0)

    def test_rows_with_unparseable_date_are_skipped(self):
        """Rows whose date column cannot be parsed are skipped individually."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr><th>Date</th><th>Bullish</th><th>Neutral</th><th>Bearish</th>
                <th>Total</th><th>Bull-Bear Spread</th><th>8-week Mov Avg</th>
                <th>Bull Average</th><th>Bear Average</th></tr>
          </thead>
          <tbody>
            <tr><td>not-a-date</td><td>40.0%</td><td>30.0%</td><td>30.0%</td>
                <td>100.0%</td><td>10.0%</td><td>35.0%</td><td>38.0%</td><td>30.5%</td></tr>
            <tr><td>5/1/25</td><td>38.0%</td><td>31.0%</td><td>31.0%</td>
                <td>100.0%</td><td>7.0%</td><td>35.0%</td><td>38.0%</td><td>30.5%</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0].date == "2025-05-01"

    def test_large_table_many_rows(self):
        """A table with 50 valid rows produces 50 records."""
        rows_html = "".join(
            f"<tr><td>1/{i + 1}/25</td>"
            f"<td>{30 + (i % 10)}.0%</td>"
            f"<td>{30.0}%</td>"
            f"<td>{40 - (i % 10)}.0%</td>"
            f"<td>100.0%</td>"
            f"<td>{(30 + (i % 10)) - (40 - (i % 10))}.0%</td>"
            f"<td>35.0%</td><td>38.0%</td><td>30.5%</td></tr>"
            for i in range(28)
        )
        html = f"""
        <html><body>
        <table>
          <thead>
            <tr><th>Date</th><th>Bullish</th><th>Neutral</th><th>Bearish</th>
                <th>Total</th><th>Bull-Bear Spread</th><th>8-week Mov Avg</th>
                <th>Bull Average</th><th>Bear Average</th></tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 28

    def test_rows_with_too_few_cells_are_skipped(self):
        """Rows with fewer than 9 cells are silently skipped."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr><th>Date</th><th>Bullish</th><th>Neutral</th><th>Bearish</th>
                <th>Total</th><th>Bull-Bear Spread</th><th>8-week Mov Avg</th>
                <th>Bull Average</th><th>Bear Average</th></tr>
          </thead>
          <tbody>
            <tr><td>5/15/25</td><td>40.0%</td><td>30.0%</td></tr>
            <tr><td>5/8/25</td><td>38.0%</td><td>31.0%</td><td>31.0%</td>
                <td>100.0%</td><td>7.0%</td><td>35.0%</td><td>38.0%</td><td>30.5%</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 1
        assert records[0].date == "2025-05-08"


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_empty_html_raises(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("   \n\t  ")

    def test_no_table_raises(self):
        html = "<html><body><p>No table here.</p></body></html>"
        with pytest.raises(ValueError, match="No table found"):
            run(html)

    def test_table_with_only_headers_raises(self):
        """A table with only header rows and no parseable data rows raises ValueError."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr><th>Date</th><th>Bullish</th><th>Neutral</th><th>Bearish</th>
                <th>Total</th><th>Bull-Bear Spread</th><th>8-week Mov Avg</th>
                <th>Bull Average</th><th>Bear Average</th></tr>
          </thead>
          <tbody></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No valid rows extracted"):
            run(html)

    def test_all_rows_bad_sum_raises(self):
        """When every row fails the 100% sum check, ValueError is raised."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr><th>Date</th><th>Bullish</th><th>Neutral</th><th>Bearish</th>
                <th>Total</th><th>Bull-Bear Spread</th><th>8-week Mov Avg</th>
                <th>Bull Average</th><th>Bear Average</th></tr>
          </thead>
          <tbody>
            <tr><td>5/15/25</td><td>60.0%</td><td>60.0%</td><td>60.0%</td>
                <td>180.0%</td><td>0.0%</td><td>35.0%</td><td>38.0%</td><td>30.5%</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No valid rows extracted"):
            run(html)


# ---------------------------------------------------------------------------
# _parse_pct unit tests
# ---------------------------------------------------------------------------


class TestParsePct:
    def test_pct_suffix_stripped(self):
        assert _parse_pct("40.0%") == pytest.approx(40.0)

    def test_no_suffix(self):
        assert _parse_pct("40.0") == pytest.approx(40.0)

    def test_negative_pct(self):
        assert _parse_pct("-4.1%") == pytest.approx(-4.1)

    def test_na_returns_none(self):
        assert _parse_pct("N/A") is None

    def test_empty_returns_none(self):
        assert _parse_pct("") is None

    def test_double_dash_returns_none(self):
        assert _parse_pct("--") is None

    def test_whitespace_stripped(self):
        assert _parse_pct("  30.5%  ") == pytest.approx(30.5)

    def test_zero(self):
        assert _parse_pct("0.0%") == pytest.approx(0.0)

    def test_text_only_returns_none(self):
        assert _parse_pct("N.A.") is None


# ---------------------------------------------------------------------------
# _parse_date unit tests
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_two_digit_year(self):
        assert _parse_date("5/15/25") == "2025-05-15"

    def test_four_digit_year(self):
        assert _parse_date("5/15/2025") == "2025-05-15"

    def test_single_digit_month_and_day(self):
        assert _parse_date("1/3/25") == "2025-01-03"

    def test_historic_date(self):
        assert _parse_date("7/24/87") == "1987-07-24"

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError, match="Cannot parse AAII date"):
            _parse_date("not-a-date")

    def test_iso_format_raises(self):
        with pytest.raises(ValueError, match="Cannot parse AAII date"):
            _parse_date("2025-05-15")


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.aaii_investor_sentiment.fetch", return_value=fake_resp) as mock_fetch,
            patch("src.scrapers.aaii_investor_sentiment.time.sleep"),
        ):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) == 8

    def test_scrape_sleeps_at_least_2_seconds(self, sample_html):
        """Acceptance criterion: sleep ≥ 2 s between paginated requests."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with (
            patch("src.scrapers.aaii_investor_sentiment.fetch", return_value=fake_resp),
            patch(
                "src.scrapers.aaii_investor_sentiment.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            scrape()

        assert any(s >= 2 for s in sleep_calls), f"No sleep >=2s found; calls={sleep_calls}"

    def test_scrape_returns_correct_record_type(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.aaii_investor_sentiment.fetch", return_value=fake_resp),
            patch("src.scrapers.aaii_investor_sentiment.time.sleep"),
        ):
            records = scrape()

        assert all(isinstance(r, AaiiInvestorSentimentRecord) for r in records)

    def test_scrape_propagates_value_error(self):
        """Propagates ValueError from run() when no valid table is found."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>No table</p></body></html>"

        with (
            patch("src.scrapers.aaii_investor_sentiment.fetch", return_value=fake_resp),
            patch("src.scrapers.aaii_investor_sentiment.time.sleep"),
        ):
            with pytest.raises(ValueError):
                scrape()
