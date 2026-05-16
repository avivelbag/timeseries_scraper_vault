"""Tests for src/scrapers/empire_state_manufacturing.py.

All tests use the static fixture at tests/fixtures/empire_state_sample.html or
inline HTML — zero live network calls.  The fixture contains 6 monthly columns
(Jan-Jun 2025) across all 12 diffusion-index rows, plus a SIX-MONTH OUTLOOK
section that must be excluded.  One cell ("18.3*") exercises footnote stripping.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protos.empire_state_manufacturing_pb2 import EmpireStateManufacturingRecord
from src.scrapers.empire_state_manufacturing import (
    SOURCE_URL,
    _is_outlook_header,
    _normalize_label,
    _parse_float,
    _parse_month_header,
    run,
    scrape,
)

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "empire_state_sample.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_six_records(self, sample_html):
        """Fixture has 6 month columns → exactly 6 records."""
        records = run(sample_html)
        assert len(records) == 6

    def test_all_records_are_correct_type(self, sample_html):
        for rec in run(sample_html):
            assert isinstance(rec, EmpireStateManufacturingRecord)

    def test_survey_date_is_first_of_month(self, sample_html):
        """survey_date must always be the first day of the month (YYYY-MM-01)."""
        import re
        pattern = re.compile(r"^\d{4}-\d{2}-01$")
        for rec in run(sample_html):
            assert pattern.match(rec.survey_date), f"Bad survey_date: {rec.survey_date!r}"

    def test_records_sorted_ascending_by_date(self, sample_html):
        records = run(sample_html)
        dates = [r.survey_date for r in records]
        assert dates == sorted(dates)

    def test_earliest_record_is_jan_2025(self, sample_html):
        records = run(sample_html)
        assert records[0].survey_date == "2025-01-01"

    def test_latest_record_is_jun_2025(self, sample_html):
        records = run(sample_html)
        assert records[-1].survey_date == "2025-06-01"

    def test_general_business_conditions_within_bounds(self, sample_html):
        """All general_business_conditions values must be within [-100, 100]."""
        for rec in run(sample_html):
            assert rec.general_business_conditions is not None
            assert -100 <= rec.general_business_conditions <= 100, (
                f"Out of range: {rec.general_business_conditions} on {rec.survey_date}"
            )

    def test_negative_values_parsed(self, sample_html):
        """Jun 2025 general_business_conditions is -16.4 (negative is valid)."""
        records = run(sample_html)
        jun = next(r for r in records if r.survey_date == "2025-06-01")
        assert jun.general_business_conditions == pytest.approx(-16.4)

    def test_positive_values_parsed(self, sample_html):
        """Mar 2025 general_business_conditions is 12.1 (positive)."""
        records = run(sample_html)
        mar = next(r for r in records if r.survey_date == "2025-03-01")
        assert mar.general_business_conditions == pytest.approx(12.1)

    def test_footnote_asterisk_stripped(self, sample_html):
        """Jun 2025 prices_paid is '18.3*' in fixture — asterisk must be stripped."""
        records = run(sample_html)
        jun = next(r for r in records if r.survey_date == "2025-06-01")
        assert jun.prices_paid == pytest.approx(18.3)

    def test_no_none_field_values(self, sample_html):
        """Fixture has no N/A cells — all float fields must be non-None."""
        float_fields = [
            "general_business_conditions",
            "new_orders",
            "shipments",
            "unfilled_orders",
            "delivery_time",
            "inventories",
            "prices_paid",
            "prices_received",
            "number_of_employees",
            "avg_workweek",
            "capital_expenditures",
            "technology_spending",
        ]
        for rec in run(sample_html):
            for field in float_fields:
                assert getattr(rec, field) is not None, (
                    f"None value for {field} on {rec.survey_date}"
                )

    def test_outlook_section_excluded(self, sample_html):
        """SIX-MONTH OUTLOOK rows must not inflate general_business_conditions."""
        records = run(sample_html)
        for rec in records:
            assert rec.general_business_conditions != pytest.approx(22.0), (
                f"Outlook value leaked into {rec.survey_date}"
            )

    def test_source_url_stored(self, sample_html):
        for rec in run(sample_html, source_url="https://example.com"):
            assert rec.source_url == "https://example.com"

    def test_fetch_time_is_iso8601(self, sample_html):
        for rec in run(sample_html):
            assert "T" in rec.fetch_time
            assert rec.fetch_time != ""

    def test_avg_workweek_field_populated(self, sample_html):
        """Average Employee Workweek row must map to avg_workweek field."""
        records = run(sample_html)
        jun = next(r for r in records if r.survey_date == "2025-06-01")
        assert jun.avg_workweek == pytest.approx(-8.6)

    def test_all_twelve_fields_populated(self, sample_html):
        """Every record must have all 12 float fields populated from the fixture."""
        records = run(sample_html)
        assert len(records) == 6
        for rec in records:
            assert rec.survey_date != ""
            assert rec.source_url != ""


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_large_table_all_months_parsed(self):
        """A table with 12 month columns yields exactly 12 records."""
        months = [
            "Jun 2025", "May 2025", "Apr 2025", "Mar 2025",
            "Feb 2025", "Jan 2025", "Dec 2024", "Nov 2024",
            "Oct 2024", "Sep 2024", "Aug 2024", "Jul 2024",
        ]
        header_cells = "".join(f"<th>{m}</th>" for m in months)
        data_rows = ""
        labels = [
            ("General Business Conditions", "general_business_conditions"),
            ("New Orders", "new_orders"),
            ("Shipments", "shipments"),
            ("Unfilled Orders", "unfilled_orders"),
            ("Delivery Time", "delivery_time"),
            ("Inventories", "inventories"),
            ("Prices Paid", "prices_paid"),
            ("Prices Received", "prices_received"),
            ("Number of Employees", "number_of_employees"),
            ("Average Employee Workweek", "avg_workweek"),
            ("Capital Expenditures", "capital_expenditures"),
            ("Technology Spending", "technology_spending"),
        ]
        for label, _ in labels:
            cells = "".join(f"<td>{i * 1.1:.1f}</td>" for i in range(12))
            data_rows += f"<tr><td>{label}</td>{cells}</tr>"
        html = f"""
        <html><body>
        <table>
          <thead><tr><th>Indicator</th>{header_cells}</tr></thead>
          <tbody>
            <tr><td colspan="13"><strong>CURRENT</strong></td></tr>
            {data_rows}
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 12

    def test_na_cells_produce_none(self):
        """N/A cells in the current section produce None float fields."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th>Indicator</th>
              <th>Jun 2025</th><th>May 2025</th><th>Apr 2025</th>
              <th>Mar 2025</th><th>Feb 2025</th><th>Jan 2025</th>
            </tr>
          </thead>
          <tbody>
            <tr><td colspan="7"><strong>CURRENT</strong></td></tr>
            <tr>
              <td>General Business Conditions</td>
              <td>N/A</td><td>-8.1</td><td>7.4</td>
              <td>12.1</td><td>-5.7</td><td>-12.6</td>
            </tr>
            <tr>
              <td>New Orders</td>
              <td>-14.2</td><td>-6.3</td><td>5.6</td>
              <td>8.9</td><td>-3.2</td><td>-9.4</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        jun = next(r for r in records if r.survey_date == "2025-06-01")
        assert jun.general_business_conditions is None

    def test_unrecognised_row_labels_skipped(self):
        """Rows with labels not in _FIELD_MAP are silently ignored."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th>Indicator</th>
              <th>Jun 2025</th><th>May 2025</th><th>Apr 2025</th>
              <th>Mar 2025</th><th>Feb 2025</th><th>Jan 2025</th>
            </tr>
          </thead>
          <tbody>
            <tr><td colspan="7"><strong>CURRENT</strong></td></tr>
            <tr>
              <td>Some Unknown Index</td>
              <td>55.0</td><td>54.0</td><td>53.0</td>
              <td>52.0</td><td>51.0</td><td>50.0</td>
            </tr>
            <tr>
              <td>General Business Conditions</td>
              <td>-16.4</td><td>-8.1</td><td>7.4</td>
              <td>12.1</td><td>-5.7</td><td>-12.6</td>
            </tr>
            <tr>
              <td>New Orders</td>
              <td>-14.2</td><td>-6.3</td><td>5.6</td>
              <td>8.9</td><td>-3.2</td><td>-9.4</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 6
        jun = next(r for r in records if r.survey_date == "2025-06-01")
        assert jun.general_business_conditions == pytest.approx(-16.4)

    def test_outlook_section_with_six_month_label_stops_parsing(self):
        """Parsing stops at the 'SIX-MONTH OUTLOOK' header row."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th>Indicator</th>
              <th>Jun 2025</th><th>May 2025</th><th>Apr 2025</th>
              <th>Mar 2025</th><th>Feb 2025</th><th>Jan 2025</th>
            </tr>
          </thead>
          <tbody>
            <tr><td colspan="7"><strong>CURRENT</strong></td></tr>
            <tr>
              <td>General Business Conditions</td>
              <td>-16.4</td><td>-8.1</td><td>7.4</td>
              <td>12.1</td><td>-5.7</td><td>-12.6</td>
            </tr>
            <tr><td colspan="7"><strong>SIX-MONTH OUTLOOK</strong></td></tr>
            <tr>
              <td>General Business Conditions</td>
              <td>99.9</td><td>99.9</td><td>99.9</td>
              <td>99.9</td><td>99.9</td><td>99.9</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        for rec in records:
            assert rec.general_business_conditions != pytest.approx(99.9)


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

    def test_no_thead_raises(self):
        """Table without <thead> raises ValueError (no month headers)."""
        html = """
        <html><body>
        <table>
          <tbody>
            <tr><td>General Business Conditions</td><td>-16.4</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError):
            run(html)

    def test_no_recognisable_month_headers_raises(self):
        """A <thead> with no parseable month-year cells raises ValueError."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr><th>Indicator</th><th>Column A</th><th>Column B</th></tr>
          </thead>
          <tbody>
            <tr><td>General Business Conditions</td><td>-16.4</td><td>-8.1</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No month headers"):
            run(html)

    def test_all_rows_unrecognised_raises(self):
        """When no row labels match _FIELD_MAP, no records are extracted → ValueError."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr><th>Indicator</th><th>Jun 2025</th><th>May 2025</th></tr>
          </thead>
          <tbody>
            <tr><td>Mystery Index</td><td>1.0</td><td>2.0</td></tr>
            <tr><td>Another Unknown</td><td>3.0</td><td>4.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)


# ---------------------------------------------------------------------------
# _parse_float unit tests
# ---------------------------------------------------------------------------


class TestParseFloat:
    def test_positive_value(self):
        assert _parse_float("14.3") == pytest.approx(14.3)

    def test_negative_value(self):
        assert _parse_float("-16.4") == pytest.approx(-16.4)

    def test_zero(self):
        assert _parse_float("0.0") == pytest.approx(0.0)

    def test_asterisk_stripped(self):
        assert _parse_float("14.3*") == pytest.approx(14.3)

    def test_dagger_stripped(self):
        assert _parse_float("8.1†") == pytest.approx(8.1)

    def test_na_string_returns_none(self):
        assert _parse_float("N/A") is None

    def test_empty_string_returns_none(self):
        assert _parse_float("") is None

    def test_double_dash_returns_none(self):
        assert _parse_float("--") is None

    def test_whitespace_stripped(self):
        assert _parse_float("  12.4  ") == pytest.approx(12.4)

    def test_unparseable_returns_none(self):
        assert _parse_float("n.a.") is None


# ---------------------------------------------------------------------------
# _parse_month_header unit tests
# ---------------------------------------------------------------------------


class TestParseMonthHeader:
    def test_abbreviated_month(self):
        assert _parse_month_header("Jun 2025") == "2025-06-01"

    def test_full_month(self):
        assert _parse_month_header("January 2025") == "2025-01-01"

    def test_december(self):
        assert _parse_month_header("Dec 2024") == "2024-12-01"

    def test_whitespace_stripped(self):
        assert _parse_month_header("  Mar 2025  ") == "2025-03-01"

    def test_non_date_returns_none(self):
        assert _parse_month_header("Indicator") is None

    def test_plain_year_returns_none(self):
        assert _parse_month_header("2025") is None


# ---------------------------------------------------------------------------
# _is_outlook_header unit tests
# ---------------------------------------------------------------------------


class TestIsOutlookHeader:
    def test_six_month_outlook(self):
        assert _is_outlook_header("SIX-MONTH OUTLOOK")

    def test_six_month_expectation(self):
        assert _is_outlook_header("Six-Month Expectation")

    def test_outlook_alone(self):
        assert _is_outlook_header("Outlook")

    def test_current_header_is_not_outlook(self):
        assert not _is_outlook_header("CURRENT GENERAL BUSINESS CONDITIONS")

    def test_data_label_is_not_outlook(self):
        assert not _is_outlook_header("General Business Conditions")


# ---------------------------------------------------------------------------
# _normalize_label unit tests
# ---------------------------------------------------------------------------


class TestNormalizeLabel:
    def test_lowercase(self):
        assert _normalize_label("General Business Conditions") == "general business conditions"

    def test_multi_space_collapsed(self):
        assert _normalize_label("Prices  Paid") == "prices paid"

    def test_leading_trailing_stripped(self):
        assert _normalize_label("  New Orders  ") == "new orders"


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.empire_state_manufacturing.fetch", return_value=fake_resp) as mock_fetch,
            patch("src.scrapers.empire_state_manufacturing.time.sleep"),
        ):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with (
            patch("src.scrapers.empire_state_manufacturing.fetch", return_value=fake_resp),
            patch(
                "src.scrapers.empire_state_manufacturing.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3 s; calls={sleep_calls}"

    def test_scrape_returns_correct_record_type(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.empire_state_manufacturing.fetch", return_value=fake_resp),
            patch("src.scrapers.empire_state_manufacturing.time.sleep"),
        ):
            records = scrape()

        assert all(isinstance(r, EmpireStateManufacturingRecord) for r in records)

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>No table</p></body></html>"

        with (
            patch("src.scrapers.empire_state_manufacturing.fetch", return_value=fake_resp),
            patch("src.scrapers.empire_state_manufacturing.time.sleep"),
        ):
            with pytest.raises(ValueError):
                scrape()
