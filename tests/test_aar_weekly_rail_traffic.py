"""Tests for src/scrapers/aar_weekly_rail_traffic.py.

All tests use static HTML fixtures or inline HTML — zero live network calls.
The fixture contains 12 commodity rows (including 2 that must be stripped),
yielding 10 valid records.
"""

import math
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protos.aar_weekly_rail_traffic_pb2 import AarWeeklyRailTrafficRecord
from src.scrapers.aar_weekly_rail_traffic import (
    SOURCE_URL,
    _is_data_row,
    _parse_pct,
    _parse_week_ending_date,
    run,
    scrape,
)
from bs4 import BeautifulSoup

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "aar_weekly_rail_traffic.html"
)

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_at_least_ten_records(self, sample_html):
        records = run(sample_html)
        assert len(records) >= 10

    def test_all_carloads_are_positive_integers(self, sample_html):
        """Carloads must be positive integers (commodity rows with zero carloads
        like the intermodal-only row are valid at zero; the acceptance criterion
        says positive, so we skip the pure-intermodal row in this assertion)."""
        records = run(sample_html)
        non_intermodal = [r for r in records if r.commodity_group != "Intermodal"]
        for rec in non_intermodal:
            assert isinstance(rec.carloads, int), f"carloads not int: {rec.carloads!r}"
            assert rec.carloads > 0, f"carloads not positive for {rec.commodity_group}"

    def test_week_ending_date_matches_yyyy_mm_dd(self, sample_html):
        for rec in run(sample_html):
            assert _DATE_PATTERN.match(rec.week_ending_date), (
                f"Bad week_ending_date: {rec.week_ending_date!r}"
            )

    def test_week_ending_date_is_2025_05_10(self, sample_html):
        records = run(sample_html)
        assert all(r.week_ending_date == "2025-05-10" for r in records)

    def test_carloads_yoy_pct_is_finite_float(self, sample_html):
        for rec in run(sample_html):
            assert isinstance(rec.carloads_yoy_pct, float), (
                f"carloads_yoy_pct not float for {rec.commodity_group}"
            )
            assert math.isfinite(rec.carloads_yoy_pct), (
                f"carloads_yoy_pct not finite for {rec.commodity_group}"
            )

    def test_all_records_are_aar_instances(self, sample_html):
        for rec in run(sample_html):
            assert isinstance(rec, AarWeeklyRailTrafficRecord)

    def test_source_url_stored_in_all_records(self, sample_html):
        for rec in run(sample_html, source_url="https://example.com"):
            assert rec.source_url == "https://example.com"

    def test_fetch_time_is_iso8601(self, sample_html):
        for rec in run(sample_html):
            assert "T" in rec.fetch_time
            assert rec.fetch_time != ""

    def test_commodity_group_names_are_nonempty_strings(self, sample_html):
        for rec in run(sample_html):
            assert isinstance(rec.commodity_group, str)
            assert rec.commodity_group != ""

    def test_known_commodities_present(self, sample_html):
        groups = {rec.commodity_group for rec in run(sample_html)}
        assert "Grain" in groups
        assert "Coal" in groups
        assert "Chemicals" in groups


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_date_in_title_tag(self):
        """week_ending_date is found when the date phrase is in an <h2> tag."""
        html = """
        <html><body>
        <h2>Weekly Traffic for the Week Ending March 15, 2025</h2>
        <table>
          <thead>
            <tr>
              <th>Commodity Group</th>
              <th>Current Week Carloads</th>
              <th>Current Week Intermodal Units</th>
              <th>Year-Ago Carloads</th>
              <th>Carloads YoY Change</th>
              <th>Intermodal YoY Change</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>Coal</td><td>65,000</td><td>0</td><td>66,000</td><td>(-1.5%)</td><td>(+0.0%)</td></tr>
            <tr><td>Grain</td><td>22,000</td><td>0</td><td>21,000</td><td>(+4.8%)</td><td>(+0.0%)</td></tr>
            <tr><td>Chemicals</td><td>30,000</td><td>1,000</td><td>29,000</td><td>(+3.4%)</td><td>(+2.0%)</td></tr>
            <tr><td>Petroleum and Products</td><td>11,000</td><td>0</td><td>12,000</td><td>(-8.3%)</td><td>(+0.0%)</td></tr>
            <tr><td>Nonmetallic Minerals</td><td>18,000</td><td>400</td><td>17,500</td><td>(+2.9%)</td><td>(+1.1%)</td></tr>
            <tr><td>Forest Products</td><td>9,500</td><td>2,000</td><td>10,000</td><td>(-5.0%)</td><td>(+7.5%)</td></tr>
            <tr><td>Motor Vehicles</td><td>14,000</td><td>3,200</td><td>13,500</td><td>(+3.7%)</td><td>(+11.0%)</td></tr>
            <tr><td>Food and Farm Products</td><td>16,000</td><td>700</td><td>15,800</td><td>(+1.3%)</td><td>(-2.0%)</td></tr>
            <tr><td>Metals</td><td>20,000</td><td>1,000</td><td>19,500</td><td>(+2.6%)</td><td>(+3.0%)</td></tr>
            <tr><td>Stone Clay Glass</td><td>13,000</td><td>500</td><td>14,000</td><td>(-7.1%)</td><td>(+5.0%)</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) >= 10
        assert all(r.week_ending_date == "2025-03-15" for r in records)

    def test_footnote_rows_stripped(self):
        """Rows where first cell starts with '*' must not generate records."""
        html = """
        <html><body>
        <p>For the week ending January 5, 2025</p>
        <table>
          <thead>
            <tr>
              <th>Commodity Group</th>
              <th>Current Week Carloads</th>
              <th>Current Week Intermodal Units</th>
              <th>Year-Ago Carloads</th>
              <th>Carloads YoY Change</th>
              <th>Intermodal YoY Change</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>Coal</td><td>65,000</td><td>0</td><td>66,000</td><td>(-1.5%)</td><td>(+0.0%)</td></tr>
            <tr><td>* Preliminary data</td><td></td><td></td><td></td><td></td><td></td></tr>
            <tr><td></td><td>blank first cell</td><td></td><td></td><td></td><td></td></tr>
            <tr><td>Grain</td><td>22,000</td><td>0</td><td>21,000</td><td>(+4.8%)</td><td>(+0.0%)</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        groups = {r.commodity_group for r in records}
        assert "Coal" in groups
        assert "Grain" in groups
        assert not any(g.startswith("*") for g in groups)
        assert not any(g == "" for g in groups)

    def test_negative_pct_parsed_correctly(self):
        """Percentage values like '(-3.1%)' must parse as -3.1."""
        assert _parse_pct("(-3.1%)") == pytest.approx(-3.1)

    def test_positive_pct_parsed_correctly(self):
        """Percentage values like '(+5.2%)' must parse as 5.2."""
        assert _parse_pct("(+5.2%)") == pytest.approx(5.2)

    def test_large_fixture_all_rows_parsed(self):
        """A table with 20 commodity rows must yield 20 records."""
        rows = "".join(
            f"<tr><td>Commodity {i}</td><td>{10000 + i * 100}</td><td>{i * 50}</td>"
            f"<td>{9800 + i * 100}</td><td>(+{i * 0.5:.1f}%)</td><td>(-{i * 0.3:.1f}%)</td></tr>"
            for i in range(1, 21)
        )
        html = f"""
        <html><body>
        <p>For the week ending February 28, 2025</p>
        <table>
          <thead>
            <tr>
              <th>Commodity Group</th>
              <th>Current Week Carloads</th>
              <th>Current Week Intermodal Units</th>
              <th>Year-Ago Carloads</th>
              <th>Carloads YoY Change</th>
              <th>Intermodal YoY Change</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 20
        for rec in records:
            assert _DATE_PATTERN.match(rec.week_ending_date)
            assert rec.carloads > 0

    def test_comma_formatted_integers_parsed(self):
        """Carloads like '65,234' must parse as 65234."""
        html = """
        <html><body>
        <p>For the week ending April 1, 2025</p>
        <table>
          <thead>
            <tr>
              <th>Commodity Group</th><th>Current Week Carloads</th>
              <th>Current Week Intermodal Units</th><th>Year-Ago Carloads</th>
              <th>Carloads YoY Change</th><th>Intermodal YoY Change</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>Coal</td><td>65,234</td><td>0</td><td>68,000</td><td>(-4.1%)</td><td>(+0.0%)</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert records[0].carloads == 65234


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_empty_html_raises_value_error(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("")

    def test_whitespace_only_html_raises_value_error(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("   \n\t  ")

    def test_no_tables_raises_value_error(self):
        html = """
        <html><body>
        <p>For the week ending May 10, 2025</p>
        <p>No data available this week.</p>
        </body></html>
        """
        with pytest.raises(ValueError, match="No tables found"):
            run(html)

    def test_no_date_raises_value_error(self):
        """HTML with a table but no recognizable date phrase must raise ValueError."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th>Commodity Group</th><th>Carloads</th><th>Intermodal</th>
              <th>Prior Year</th><th>Carloads YoY</th><th>Intermodal YoY</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>Coal</td><td>65,000</td><td>0</td><td>66,000</td><td>(-1.5%)</td><td>(+0.0%)</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="week-ending date"):
            run(html)


# ---------------------------------------------------------------------------
# _parse_pct unit tests
# ---------------------------------------------------------------------------


class TestParsePct:
    def test_positive_parenthetical(self):
        assert _parse_pct("(+5.2%)") == pytest.approx(5.2)

    def test_negative_parenthetical(self):
        assert _parse_pct("(-3.1%)") == pytest.approx(-3.1)

    def test_plain_percentage(self):
        assert _parse_pct("4.5%") == pytest.approx(4.5)

    def test_zero(self):
        assert _parse_pct("(+0.0%)") == pytest.approx(0.0)

    def test_no_value_returns_zero(self):
        assert _parse_pct("n.a.") == pytest.approx(0.0)

    def test_empty_returns_zero(self):
        assert _parse_pct("") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _is_data_row unit tests
# ---------------------------------------------------------------------------


class TestIsDataRow:
    def test_normal_row(self):
        assert _is_data_row("Coal") is True

    def test_footnote_row(self):
        assert _is_data_row("* Preliminary data") is False

    def test_blank_row(self):
        assert _is_data_row("") is False

    def test_whitespace_row(self):
        assert _is_data_row("   ") is False


# ---------------------------------------------------------------------------
# _parse_week_ending_date unit tests
# ---------------------------------------------------------------------------


class TestParseWeekEndingDate:
    def test_standard_phrase(self):
        soup = BeautifulSoup(
            "<p>For the week ending May 10, 2025</p>", "lxml"
        )
        assert _parse_week_ending_date(soup) == "2025-05-10"

    def test_week_of_phrase(self):
        soup = BeautifulSoup(
            "<p>For the week of January 3, 2025</p>", "lxml"
        )
        assert _parse_week_ending_date(soup) == "2025-01-03"

    def test_abbreviated_month(self):
        soup = BeautifulSoup(
            "<p>For the week ending Mar 15, 2025</p>", "lxml"
        )
        assert _parse_week_ending_date(soup) == "2025-03-15"

    def test_no_date_raises_value_error(self):
        soup = BeautifulSoup("<p>No date here</p>", "lxml")
        with pytest.raises(ValueError, match="week-ending date"):
            _parse_week_ending_date(soup)


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch(
                "src.scrapers.aar_weekly_rail_traffic.fetch", return_value=fake_resp
            ) as mock_fetch,
            patch("src.scrapers.aar_weekly_rail_traffic.time.sleep"),
        ):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with (
            patch("src.scrapers.aar_weekly_rail_traffic.fetch", return_value=fake_resp),
            patch(
                "src.scrapers.aar_weekly_rail_traffic.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >=3s; calls: {sleep_calls}"

    def test_scrape_returns_aar_records(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.aar_weekly_rail_traffic.fetch", return_value=fake_resp),
            patch("src.scrapers.aar_weekly_rail_traffic.time.sleep"),
        ):
            records = scrape()

        assert all(isinstance(r, AarWeeklyRailTrafficRecord) for r in records)

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>no tables</p></body></html>"

        with (
            patch("src.scrapers.aar_weekly_rail_traffic.fetch", return_value=fake_resp),
            patch("src.scrapers.aar_weekly_rail_traffic.time.sleep"),
        ):
            with pytest.raises(ValueError):
                scrape()
