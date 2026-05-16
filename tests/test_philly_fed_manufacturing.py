"""Tests for src/scrapers/philly_fed_manufacturing.py.

All tests use the static fixture at tests/fixtures/philly_fed_manufacturing.html
or inline HTML — zero live network calls.  The fixture contains 12 indicator
rows for the May 2026 survey month.
"""

import math
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bs4 import BeautifulSoup

from protos.philly_fed_manufacturing_pb2 import PhillyFedManufacturingRecord
from src.scrapers.philly_fed_manufacturing import (
    SOURCE_URL,
    _parse_float,
    _parse_report_date,
    run,
    scrape,
)

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "philly_fed_manufacturing.html"
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-01$")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_at_least_ten_records(self, sample_html):
        """Acceptance criterion: ≥10 indicator rows parsed."""
        records = run(sample_html)
        assert len(records) >= 10

    def test_all_records_are_correct_type(self, sample_html):
        for rec in run(sample_html):
            assert isinstance(rec, PhillyFedManufacturingRecord)

    def test_report_date_is_first_of_month(self, sample_html):
        """Acceptance criterion: report_date parses to a valid date."""
        for rec in run(sample_html):
            assert _DATE_RE.match(rec.report_date), (
                f"report_date {rec.report_date!r} does not match YYYY-MM-01"
            )

    def test_report_date_is_may_2026(self, sample_html):
        records = run(sample_html)
        assert all(r.report_date == "2026-05-01" for r in records)

    def test_current_index_is_finite_float(self, sample_html):
        """Acceptance criterion: current_index is a finite float for each row."""
        for rec in run(sample_html):
            assert rec.current_index is not None
            assert isinstance(rec.current_index, float)
            assert math.isfinite(rec.current_index), (
                f"current_index={rec.current_index!r} is not finite for {rec.indicator_name!r}"
            )

    def test_indicator_names_are_non_empty(self, sample_html):
        for rec in run(sample_html):
            assert rec.indicator_name and rec.indicator_name.strip()

    def test_general_activity_values(self, sample_html):
        records = run(sample_html)
        ga = next(r for r in records if r.indicator_name == "General Activity")
        assert ga.current_index == pytest.approx(40.2)
        assert ga.prior_month_index == pytest.approx(8.5)
        assert ga.six_month_forecast == pytest.approx(36.9)

    def test_negative_index_parses_correctly(self, sample_html):
        """Negative diffusion indexes are valid and must be stored as-is."""
        records = run(sample_html)
        inv = next(r for r in records if r.indicator_name == "Inventories")
        assert inv.current_index < 0

    def test_source_url_stored_in_each_record(self, sample_html):
        for rec in run(sample_html, source_url="https://example.com/philly"):
            assert rec.source_url == "https://example.com/philly"

    def test_fetch_time_is_iso8601(self, sample_html):
        for rec in run(sample_html):
            assert "T" in rec.fetch_time and rec.fetch_time != ""

    def test_all_twelve_indicators_present(self, sample_html):
        records = run(sample_html)
        names = {r.indicator_name for r in records}
        expected = {
            "General Activity",
            "New Orders",
            "Shipments",
            "Unfilled Orders",
            "Delivery Times",
            "Inventories",
            "Prices Paid",
            "Prices Received",
            "Number of Employees",
            "Average Employee Workweek",
            "Capital Expenditures",
            "Technology Spending",
        }
        assert expected == names

    def test_prior_month_and_forecast_parsed(self, sample_html):
        records = run(sample_html)
        no = next(r for r in records if r.indicator_name == "New Orders")
        assert no.prior_month_index == pytest.approx(12.3)
        assert no.six_month_forecast == pytest.approx(29.4)


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_abbreviated_month_in_heading(self):
        """Abbreviated month names ('May' vs 'May') still parse correctly."""
        html = """
        <html><body>
          <h2>Jan 2025 Manufacturing Business Outlook Survey</h2>
          <table>
            <thead><tr><th>Indicator</th><th>Current</th><th>Prior</th><th>Forecast</th></tr></thead>
            <tbody>
              <tr><td>General Activity</td><td>15.3</td><td>-2.1</td><td>20.4</td></tr>
              <tr><td>New Orders</td><td>10.2</td><td>5.0</td><td>18.9</td></tr>
              <tr><td>Shipments</td><td>8.7</td><td>3.2</td><td>14.5</td></tr>
              <tr><td>Unfilled Orders</td><td>-4.1</td><td>-6.3</td><td>2.8</td></tr>
              <tr><td>Delivery Times</td><td>-1.5</td><td>0.9</td><td>-1.2</td></tr>
              <tr><td>Inventories</td><td>-8.3</td><td>-12.1</td><td>1.9</td></tr>
              <tr><td>Prices Paid</td><td>42.7</td><td>38.5</td><td>45.0</td></tr>
              <tr><td>Prices Received</td><td>18.4</td><td>12.6</td><td>24.3</td></tr>
              <tr><td>Number of Employees</td><td>9.2</td><td>3.8</td><td>15.7</td></tr>
              <tr><td>Average Employee Workweek</td><td>-2.8</td><td>-5.1</td><td>1.6</td></tr>
            </tbody>
          </table>
        </body></html>
        """
        records = run(html)
        assert len(records) >= 10
        assert all(r.report_date == "2025-01-01" for r in records)

    def test_footnote_asterisk_stripped(self):
        """Values with trailing asterisks ('40.2*') parse to the numeric value."""
        html = """
        <html><body>
          <h2>March 2025 Manufacturing Business Outlook Survey</h2>
          <table>
            <thead><tr><th>Indicator</th><th>Current</th><th>Prior</th><th>Forecast</th></tr></thead>
            <tbody>
              <tr><td>General Activity</td><td>40.2*</td><td>8.5*</td><td>36.9*</td></tr>
              <tr><td>New Orders</td><td>22.5</td><td>12.3</td><td>29.4</td></tr>
              <tr><td>Shipments</td><td>18.7</td><td>-3.1</td><td>25.0</td></tr>
              <tr><td>Unfilled Orders</td><td>-5.3</td><td>-8.2</td><td>4.1</td></tr>
              <tr><td>Delivery Times</td><td>-2.1</td><td>1.4</td><td>-1.8</td></tr>
              <tr><td>Inventories</td><td>-10.4</td><td>-14.7</td><td>3.2</td></tr>
              <tr><td>Prices Paid</td><td>55.3</td><td>42.6</td><td>48.7</td></tr>
              <tr><td>Prices Received</td><td>23.8</td><td>16.1</td><td>30.5</td></tr>
              <tr><td>Number of Employees</td><td>12.1</td><td>5.4</td><td>18.3</td></tr>
              <tr><td>Average Employee Workweek</td><td>-3.7</td><td>-6.2</td><td>2.9</td></tr>
            </tbody>
          </table>
        </body></html>
        """
        records = run(html)
        ga = next(r for r in records if r.indicator_name == "General Activity")
        assert ga.current_index == pytest.approx(40.2)
        assert ga.prior_month_index == pytest.approx(8.5)
        assert ga.six_month_forecast == pytest.approx(36.9)

    def test_na_values_become_none(self):
        """N/A in prior_month or six_month columns produces None, not an error."""
        html = """
        <html><body>
          <h2>April 2025 Manufacturing Business Outlook Survey</h2>
          <table>
            <thead><tr><th>Indicator</th><th>Current</th><th>Prior</th><th>Forecast</th></tr></thead>
            <tbody>
              <tr><td>General Activity</td><td>15.0</td><td>N/A</td><td>N/A</td></tr>
              <tr><td>New Orders</td><td>10.0</td><td>5.0</td><td>18.0</td></tr>
              <tr><td>Shipments</td><td>8.0</td><td>3.0</td><td>14.0</td></tr>
              <tr><td>Unfilled Orders</td><td>-4.0</td><td>-6.0</td><td>3.0</td></tr>
              <tr><td>Delivery Times</td><td>-1.0</td><td>1.0</td><td>-1.0</td></tr>
              <tr><td>Inventories</td><td>-8.0</td><td>-12.0</td><td>2.0</td></tr>
              <tr><td>Prices Paid</td><td>42.0</td><td>38.0</td><td>45.0</td></tr>
              <tr><td>Prices Received</td><td>18.0</td><td>12.0</td><td>24.0</td></tr>
              <tr><td>Number of Employees</td><td>9.0</td><td>4.0</td><td>16.0</td></tr>
              <tr><td>Average Employee Workweek</td><td>-2.0</td><td>-5.0</td><td>2.0</td></tr>
            </tbody>
          </table>
        </body></html>
        """
        records = run(html)
        ga = next(r for r in records if r.indicator_name == "General Activity")
        assert ga.current_index == pytest.approx(15.0)
        assert ga.prior_month_index is None
        assert ga.six_month_forecast is None

    def test_large_table_many_indicators(self):
        """A table with 20 indicators produces 20 records."""
        indicators = [f"Indicator {i}" for i in range(20)]
        rows_html = "".join(
            f"<tr><td>{name}</td><td>{10.0 + i}</td><td>{5.0 + i}</td><td>{15.0 + i}</td></tr>"
            for i, name in enumerate(indicators)
        )
        html = f"""
        <html><body>
          <h2>June 2025 Manufacturing Business Outlook Survey</h2>
          <table>
            <thead><tr><th>Indicator</th><th>Current</th><th>Prior</th><th>Forecast</th></tr></thead>
            <tbody>{rows_html}</tbody>
          </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 20

    def test_rows_missing_current_index_skipped(self):
        """Rows where current_index is N/A are excluded from results."""
        html = """
        <html><body>
          <h2>July 2025 Manufacturing Business Outlook Survey</h2>
          <table>
            <thead><tr><th>Indicator</th><th>Current</th><th>Prior</th><th>Forecast</th></tr></thead>
            <tbody>
              <tr><td>General Activity</td><td>N/A</td><td>8.5</td><td>36.9</td></tr>
              <tr><td>New Orders</td><td>22.5</td><td>12.3</td><td>29.4</td></tr>
              <tr><td>Shipments</td><td>18.7</td><td>-3.1</td><td>25.0</td></tr>
              <tr><td>Unfilled Orders</td><td>-5.3</td><td>-8.2</td><td>4.1</td></tr>
              <tr><td>Delivery Times</td><td>-2.1</td><td>1.4</td><td>-1.8</td></tr>
              <tr><td>Inventories</td><td>-10.4</td><td>-14.7</td><td>3.2</td></tr>
              <tr><td>Prices Paid</td><td>55.3</td><td>42.6</td><td>48.7</td></tr>
              <tr><td>Prices Received</td><td>23.8</td><td>16.1</td><td>30.5</td></tr>
              <tr><td>Number of Employees</td><td>12.1</td><td>5.4</td><td>18.3</td></tr>
              <tr><td>Average Employee Workweek</td><td>-3.7</td><td>-6.2</td><td>2.9</td></tr>
            </tbody>
          </table>
        </body></html>
        """
        records = run(html)
        names = {r.indicator_name for r in records}
        assert "General Activity" not in names
        assert len(records) == 9


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
        html = """
        <html><body>
          <h2>May 2026 Manufacturing Business Outlook Survey</h2>
          <p>No table here.</p>
        </body></html>
        """
        with pytest.raises(ValueError, match="No table found"):
            run(html)

    def test_no_heading_raises(self):
        """When no heading matches the expected pattern, a ValueError is raised."""
        html = """
        <html><body>
          <h2>Unrelated Page Title</h2>
          <table>
            <thead><tr><th>Indicator</th><th>Current</th><th>Prior</th><th>Forecast</th></tr></thead>
            <tbody>
              <tr><td>General Activity</td><td>40.2</td><td>8.5</td><td>36.9</td></tr>
            </tbody>
          </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="Could not parse report date"):
            run(html)

    def test_table_with_no_valid_data_rows_raises(self):
        """A table with only header rows and no parseable data raises ValueError."""
        html = """
        <html><body>
          <h2>August 2025 Manufacturing Business Outlook Survey</h2>
          <table>
            <thead>
              <tr><th>Indicator</th><th>Current</th><th>Prior</th><th>Forecast</th></tr>
            </thead>
            <tbody>
            </tbody>
          </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No indicator rows extracted"):
            run(html)


# ---------------------------------------------------------------------------
# _parse_float unit tests
# ---------------------------------------------------------------------------


class TestParseFloat:
    def test_positive_float(self):
        assert _parse_float("40.2") == pytest.approx(40.2)

    def test_negative_float(self):
        assert _parse_float("-8.5") == pytest.approx(-8.5)

    def test_zero(self):
        assert _parse_float("0.0") == pytest.approx(0.0)

    def test_na_returns_none(self):
        assert _parse_float("N/A") is None

    def test_empty_returns_none(self):
        assert _parse_float("") is None

    def test_double_dash_returns_none(self):
        assert _parse_float("--") is None

    def test_asterisk_stripped(self):
        assert _parse_float("40.2*") == pytest.approx(40.2)

    def test_comma_thousands_separator_stripped(self):
        assert _parse_float("1,234.5") == pytest.approx(1234.5)

    def test_whitespace_stripped(self):
        assert _parse_float("  36.9  ") == pytest.approx(36.9)

    def test_text_only_returns_none(self):
        assert _parse_float("Increasing") is None


# ---------------------------------------------------------------------------
# _parse_report_date unit tests
# ---------------------------------------------------------------------------


class TestParseReportDate:
    def test_full_month_name_in_h2(self):
        html = "<html><body><h2>May 2026 Manufacturing Business Outlook Survey</h2></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert _parse_report_date(soup) == "2026-05-01"

    def test_abbreviated_month_in_h3(self):
        html = "<html><body><h3>Jan 2025 Manufacturing Business Outlook Survey</h3></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert _parse_report_date(soup) == "2025-01-01"

    def test_case_insensitive_match(self):
        html = "<html><body><h2>MARCH 2025 MANUFACTURING BUSINESS OUTLOOK SURVEY</h2></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert _parse_report_date(soup) == "2025-03-01"

    def test_no_matching_heading_raises(self):
        html = "<html><body><h2>Some Other Title</h2></body></html>"
        soup = BeautifulSoup(html, "lxml")
        with pytest.raises(ValueError, match="Could not parse report date"):
            _parse_report_date(soup)

    def test_heading_in_h1(self):
        html = "<html><body><h1>December 2024 Manufacturing Business Outlook Survey</h1></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert _parse_report_date(soup) == "2024-12-01"


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.philly_fed_manufacturing.fetch", return_value=fake_resp) as mock_fetch,
            patch("src.scrapers.philly_fed_manufacturing.time.sleep"),
        ):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) >= 10

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        """Acceptance criterion: minimum 3 s sleep between requests."""
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with (
            patch("src.scrapers.philly_fed_manufacturing.fetch", return_value=fake_resp),
            patch(
                "src.scrapers.philly_fed_manufacturing.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >=3s; calls={sleep_calls}"

    def test_scrape_returns_correct_record_type(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.philly_fed_manufacturing.fetch", return_value=fake_resp),
            patch("src.scrapers.philly_fed_manufacturing.time.sleep"),
        ):
            records = scrape()

        assert all(isinstance(r, PhillyFedManufacturingRecord) for r in records)

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>No table</p></body></html>"

        with (
            patch("src.scrapers.philly_fed_manufacturing.fetch", return_value=fake_resp),
            patch("src.scrapers.philly_fed_manufacturing.time.sleep"),
        ):
            with pytest.raises(ValueError):
                scrape()
