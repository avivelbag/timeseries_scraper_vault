"""Tests for src/scrapers/ism_manufacturing_pmi.py.

All tests use the static fixture at tests/fixtures/ism_pmi_sample.html or
inline HTML — zero live network calls.  The fixture contains 3 months of
data (November 2024, December 2024, January 2025) with all 11 sub-index
fields populated for each month.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from protos.ism_manufacturing_pmi_pb2 import IsmManufacturingPmiRecord
from src.scrapers.ism_manufacturing_pmi import (
    SOURCE_URL,
    _build_column_map,
    _header_rows,
    _normalize_label,
    _parse_float,
    _parse_month_header,
    run,
    scrape,
)

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "ism_pmi_sample.html")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-01$")

_ALL_SUB_INDEX_FIELDS = [
    "pmi",
    "new_orders",
    "production",
    "employment",
    "supplier_deliveries",
    "inventories",
    "customer_inventories",
    "prices",
    "backlog_of_orders",
    "new_export_orders",
    "imports",
]


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_three_records(self, sample_html):
        records = run(sample_html)
        assert len(records) == 3

    def test_all_records_are_correct_type(self, sample_html):
        for rec in run(sample_html):
            assert isinstance(rec, IsmManufacturingPmiRecord)

    def test_report_date_is_first_of_month(self, sample_html):
        """All report_date values must match YYYY-MM-01."""
        for rec in run(sample_html):
            assert _DATE_RE.match(rec.report_date), (
                f"report_date {rec.report_date!r} is not a first-of-month date"
            )

    def test_records_sorted_ascending_by_date(self, sample_html):
        records = run(sample_html)
        dates = [r.report_date for r in records]
        assert dates == sorted(dates)

    def test_earliest_date_is_nov_2024(self, sample_html):
        records = run(sample_html)
        assert records[0].report_date == "2024-11-01"

    def test_latest_date_is_jan_2025(self, sample_html):
        records = run(sample_html)
        assert records[-1].report_date == "2025-01-01"

    def test_pmi_within_30_to_70(self, sample_html):
        """Acceptance criterion: PMI must be in [30, 70] for all records."""
        for rec in run(sample_html):
            assert 30 <= rec.pmi <= 70, f"pmi={rec.pmi} out of range for {rec.report_date}"

    def test_no_none_sub_index_values(self, sample_html):
        """Acceptance criterion: no sub-index field may be None."""
        for rec in run(sample_html):
            for field in _ALL_SUB_INDEX_FIELDS:
                assert getattr(rec, field) is not None, (
                    f"{field} is None for {rec.report_date}"
                )

    def test_jan_2025_pmi_value(self, sample_html):
        records = run(sample_html)
        jan = next(r for r in records if r.report_date == "2025-01-01")
        assert jan.pmi == pytest.approx(49.3)

    def test_dec_2024_pmi_value(self, sample_html):
        records = run(sample_html)
        dec = next(r for r in records if r.report_date == "2024-12-01")
        assert dec.pmi == pytest.approx(49.3)

    def test_nov_2024_pmi_value(self, sample_html):
        records = run(sample_html)
        nov = next(r for r in records if r.report_date == "2024-11-01")
        assert nov.pmi == pytest.approx(48.4)

    def test_direction_columns_not_parsed_as_float(self, sample_html):
        """Direction text (e.g. 'Contracting') must not appear as a field value."""
        for rec in run(sample_html):
            for field in _ALL_SUB_INDEX_FIELDS:
                val = getattr(rec, field)
                if val is not None:
                    assert isinstance(val, float), (
                        f"{field}={val!r} is not a float for {rec.report_date}"
                    )

    def test_source_url_stored_in_each_record(self, sample_html):
        for rec in run(sample_html, source_url="https://example.com/pmi"):
            assert rec.source_url == "https://example.com/pmi"

    def test_fetch_time_is_iso8601(self, sample_html):
        for rec in run(sample_html):
            assert "T" in rec.fetch_time and rec.fetch_time != ""

    def test_new_orders_sub_index(self, sample_html):
        records = run(sample_html)
        jan = next(r for r in records if r.report_date == "2025-01-01")
        assert jan.new_orders == pytest.approx(52.5)

    def test_backlog_of_orders_sub_index(self, sample_html):
        records = run(sample_html)
        jan = next(r for r in records if r.report_date == "2025-01-01")
        assert jan.backlog_of_orders == pytest.approx(44.9)

    def test_imports_sub_index(self, sample_html):
        records = run(sample_html)
        dec = next(r for r in records if r.report_date == "2024-12-01")
        assert dec.imports == pytest.approx(51.3)


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_abbreviated_month_header_parsed(self):
        """'Jan 2025' header (abbreviated) is parsed to 2025-01-01."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Index</th>
              <th colspan="4">Jan 2025</th>
              <th colspan="4">Dec 2024</th>
              <th colspan="4">Nov 2024</th>
            </tr>
            <tr>
              <th>Series Index</th><th>Series Direction</th><th>Rate of Change</th><th>Trend (Months)</th>
              <th>Series Index</th><th>Series Direction</th><th>Rate of Change</th><th>Trend (Months)</th>
              <th>Series Index</th><th>Series Direction</th><th>Rate of Change</th><th>Trend (Months)</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Manufacturing PMI</td>
              <td>49.3</td><td>Contracting</td><td>Faster</td><td>2</td>
              <td>49.3</td><td>Contracting</td><td>Same</td><td>1</td>
              <td>48.4</td><td>Contracting</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>New Orders</td>
              <td>52.5</td><td>Growing</td><td>Faster</td><td>2</td>
              <td>52.1</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>50.4</td><td>Growing</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Production</td>
              <td>52.5</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>49.9</td><td>Contracting</td><td>Faster</td><td>1</td>
              <td>46.8</td><td>Contracting</td><td>Slower</td><td>2</td>
            </tr>
            <tr>
              <td>Employment</td>
              <td>50.3</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>45.3</td><td>Contracting</td><td>Faster</td><td>2</td>
              <td>46.1</td><td>Contracting</td><td>Slower</td><td>3</td>
            </tr>
            <tr>
              <td>Supplier Deliveries</td>
              <td>50.9</td><td>Slowing</td><td>Faster</td><td>1</td>
              <td>49.8</td><td>Faster</td><td>Same</td><td>1</td>
              <td>48.7</td><td>Faster</td><td>Faster</td><td>2</td>
            </tr>
            <tr>
              <td>Inventories</td>
              <td>45.9</td><td>Contracting</td><td>Slower</td><td>2</td>
              <td>48.4</td><td>Contracting</td><td>Faster</td><td>1</td>
              <td>47.2</td><td>Contracting</td><td>Faster</td><td>2</td>
            </tr>
            <tr>
              <td>Customers' Inventories</td>
              <td>46.4</td><td>Too Low</td><td>Same</td><td>3</td>
              <td>46.3</td><td>Too Low</td><td>Same</td><td>2</td>
              <td>46.3</td><td>Too Low</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Prices</td>
              <td>54.9</td><td>Increasing</td><td>Faster</td><td>2</td>
              <td>52.5</td><td>Increasing</td><td>Faster</td><td>1</td>
              <td>50.3</td><td>Increasing</td><td>Faster</td><td>2</td>
            </tr>
            <tr>
              <td>Backlog of Orders</td>
              <td>44.9</td><td>Contracting</td><td>Faster</td><td>4</td>
              <td>41.3</td><td>Contracting</td><td>Faster</td><td>3</td>
              <td>41.8</td><td>Contracting</td><td>Slower</td><td>2</td>
            </tr>
            <tr>
              <td>New Export Orders</td>
              <td>52.4</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>52.6</td><td>Growing</td><td>Faster</td><td>2</td>
              <td>48.7</td><td>Contracting</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Imports</td>
              <td>52.6</td><td>Growing</td><td>Faster</td><td>2</td>
              <td>51.3</td><td>Growing</td><td>Slower</td><td>1</td>
              <td>48.7</td><td>Contracting</td><td>Faster</td><td>1</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 3
        jan = next(r for r in records if r.report_date == "2025-01-01")
        assert jan.pmi == pytest.approx(49.3)

    def test_na_cell_becomes_none(self):
        """N/A cells in the Series Index column must produce None, not raise."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Index</th>
              <th colspan="4">March 2025</th>
              <th colspan="4">February 2025</th>
              <th colspan="4">January 2025</th>
            </tr>
            <tr>
              <th>Series Index</th><th>Series Direction</th><th>Rate of Change</th><th>Trend (Months)</th>
              <th>Series Index</th><th>Series Direction</th><th>Rate of Change</th><th>Trend (Months)</th>
              <th>Series Index</th><th>Series Direction</th><th>Rate of Change</th><th>Trend (Months)</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Manufacturing PMI</td>
              <td>N/A</td><td>--</td><td>--</td><td>--</td>
              <td>49.0</td><td>Contracting</td><td>Faster</td><td>2</td>
              <td>50.9</td><td>Growing</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>New Orders</td>
              <td>48.6</td><td>Contracting</td><td>Faster</td><td>1</td>
              <td>48.6</td><td>Contracting</td><td>Faster</td><td>1</td>
              <td>55.1</td><td>Growing</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Production</td>
              <td>50.7</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>50.7</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>52.5</td><td>Growing</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Employment</td>
              <td>44.7</td><td>Contracting</td><td>Faster</td><td>3</td>
              <td>47.6</td><td>Contracting</td><td>Faster</td><td>2</td>
              <td>50.3</td><td>Growing</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Supplier Deliveries</td>
              <td>53.0</td><td>Slowing</td><td>Faster</td><td>1</td>
              <td>53.0</td><td>Slowing</td><td>Same</td><td>1</td>
              <td>50.9</td><td>Slowing</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Inventories</td>
              <td>44.8</td><td>Contracting</td><td>Faster</td><td>3</td>
              <td>45.9</td><td>Contracting</td><td>Slower</td><td>2</td>
              <td>45.9</td><td>Contracting</td><td>Slower</td><td>2</td>
            </tr>
            <tr>
              <td>Customers' Inventories</td>
              <td>46.6</td><td>Too Low</td><td>Same</td><td>4</td>
              <td>46.4</td><td>Too Low</td><td>Same</td><td>3</td>
              <td>46.4</td><td>Too Low</td><td>Same</td><td>3</td>
            </tr>
            <tr>
              <td>Prices</td>
              <td>62.4</td><td>Increasing</td><td>Faster</td><td>3</td>
              <td>62.4</td><td>Increasing</td><td>Faster</td><td>2</td>
              <td>54.9</td><td>Increasing</td><td>Faster</td><td>2</td>
            </tr>
            <tr>
              <td>Backlog of Orders</td>
              <td>44.5</td><td>Contracting</td><td>Slower</td><td>5</td>
              <td>44.5</td><td>Contracting</td><td>Slower</td><td>4</td>
              <td>44.9</td><td>Contracting</td><td>Faster</td><td>4</td>
            </tr>
            <tr>
              <td>New Export Orders</td>
              <td>49.6</td><td>Contracting</td><td>Faster</td><td>2</td>
              <td>51.4</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>52.4</td><td>Growing</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Imports</td>
              <td>50.1</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>50.9</td><td>Growing</td><td>Slower</td><td>2</td>
              <td>52.6</td><td>Growing</td><td>Faster</td><td>2</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 3
        mar = next(r for r in records if r.report_date == "2025-03-01")
        assert mar.pmi is None

    def test_unknown_sub_index_rows_skipped(self):
        """Rows with unrecognised labels (footnotes, totals) are silently skipped."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Index</th>
              <th colspan="4">February 2025</th>
              <th colspan="4">January 2025</th>
              <th colspan="4">December 2024</th>
            </tr>
            <tr>
              <th>Series Index</th><th>Series Direction</th><th>Rate of Change</th><th>Trend (Months)</th>
              <th>Series Index</th><th>Series Direction</th><th>Rate of Change</th><th>Trend (Months)</th>
              <th>Series Index</th><th>Series Direction</th><th>Rate of Change</th><th>Trend (Months)</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Manufacturing PMI&#174;</td>
              <td>50.3</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>49.3</td><td>Contracting</td><td>Same</td><td>2</td>
              <td>49.3</td><td>Contracting</td><td>Same</td><td>1</td>
            </tr>
            <tr>
              <td>Footnote: Seasonally Adjusted</td>
              <td>*</td><td>*</td><td>*</td><td>*</td>
              <td>*</td><td>*</td><td>*</td><td>*</td>
              <td>*</td><td>*</td><td>*</td><td>*</td>
            </tr>
            <tr>
              <td>New Orders</td>
              <td>52.5</td><td>Growing</td><td>Faster</td><td>2</td>
              <td>52.5</td><td>Growing</td><td>Faster</td><td>2</td>
              <td>52.1</td><td>Growing</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Production</td>
              <td>52.5</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>52.5</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>49.9</td><td>Contracting</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Employment</td>
              <td>53.0</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>50.3</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>45.3</td><td>Contracting</td><td>Faster</td><td>2</td>
            </tr>
            <tr>
              <td>Supplier Deliveries</td>
              <td>52.0</td><td>Slowing</td><td>Faster</td><td>2</td>
              <td>50.9</td><td>Slowing</td><td>Faster</td><td>1</td>
              <td>49.8</td><td>Faster</td><td>Same</td><td>1</td>
            </tr>
            <tr>
              <td>Inventories</td>
              <td>46.9</td><td>Contracting</td><td>Faster</td><td>3</td>
              <td>45.9</td><td>Contracting</td><td>Slower</td><td>2</td>
              <td>48.4</td><td>Contracting</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Customers' Inventories</td>
              <td>47.1</td><td>Too Low</td><td>Same</td><td>4</td>
              <td>46.4</td><td>Too Low</td><td>Same</td><td>3</td>
              <td>46.3</td><td>Too Low</td><td>Same</td><td>2</td>
            </tr>
            <tr>
              <td>Prices</td>
              <td>55.1</td><td>Increasing</td><td>Faster</td><td>3</td>
              <td>54.9</td><td>Increasing</td><td>Faster</td><td>2</td>
              <td>52.5</td><td>Increasing</td><td>Faster</td><td>1</td>
            </tr>
            <tr>
              <td>Backlog of Orders</td>
              <td>44.5</td><td>Contracting</td><td>Faster</td><td>5</td>
              <td>44.9</td><td>Contracting</td><td>Faster</td><td>4</td>
              <td>41.3</td><td>Contracting</td><td>Faster</td><td>3</td>
            </tr>
            <tr>
              <td>New Export Orders</td>
              <td>51.4</td><td>Growing</td><td>Slower</td><td>2</td>
              <td>52.4</td><td>Growing</td><td>Faster</td><td>1</td>
              <td>52.6</td><td>Growing</td><td>Faster</td><td>2</td>
            </tr>
            <tr>
              <td>Imports</td>
              <td>50.9</td><td>Growing</td><td>Slower</td><td>3</td>
              <td>52.6</td><td>Growing</td><td>Faster</td><td>2</td>
              <td>51.3</td><td>Growing</td><td>Slower</td><td>1</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 3
        feb = next(r for r in records if r.report_date == "2025-02-01")
        assert feb.pmi == pytest.approx(50.3)

    def test_large_table_six_months(self):
        """A table with 6 months yields 6 records."""
        month_headers = " ".join(
            f'<th colspan="4">{m}</th>'
            for m in ["June 2025", "May 2025", "April 2025",
                      "March 2025", "February 2025", "January 2025"]
        )
        sub_headers = (
            '<th>Series Index</th><th>Series Direction</th>'
            '<th>Rate of Change</th><th>Trend (Months)</th>'
        ) * 6

        def row(label: str, val: float) -> str:
            cells = "".join(
                f"<td>{val + i * 0.1:.1f}</td><td>Growing</td><td>Faster</td><td>1</td>"
                for i in range(6)
            )
            return f"<tr><td>{label}</td>{cells}</tr>"

        rows_html = "".join([
            row("Manufacturing PMI", 49.0),
            row("New Orders", 52.0),
            row("Production", 51.0),
            row("Employment", 50.0),
            row("Supplier Deliveries", 50.5),
            row("Inventories", 47.0),
            row("Customers' Inventories", 46.0),
            row("Prices", 54.0),
            row("Backlog of Orders", 44.0),
            row("New Export Orders", 51.0),
            row("Imports", 52.0),
        ])
        html = f"""
        <html><body><table>
          <thead>
            <tr><th rowspan="2">Index</th>{month_headers}</tr>
            <tr>{sub_headers}</tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table></body></html>
        """
        records = run(html)
        assert len(records) == 6


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

    def test_table_with_no_known_rows_raises(self):
        """A table with no recognised sub-index labels produces no records and raises."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Index</th>
              <th colspan="4">January 2025</th>
            </tr>
            <tr>
              <th>Series Index</th><th>Series Direction</th>
              <th>Rate of Change</th><th>Trend (Months)</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Unknown Metric A</td>
              <td>49.3</td><td>Contracting</td><td>Faster</td><td>2</td>
            </tr>
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
    def test_positive(self):
        assert _parse_float("49.3") == pytest.approx(49.3)

    def test_negative(self):
        assert _parse_float("-0.5") == pytest.approx(-0.5)

    def test_zero(self):
        assert _parse_float("0.0") == pytest.approx(0.0)

    def test_na_returns_none(self):
        assert _parse_float("N/A") is None

    def test_empty_returns_none(self):
        assert _parse_float("") is None

    def test_double_dash_returns_none(self):
        assert _parse_float("--") is None

    def test_text_direction_returns_none(self):
        assert _parse_float("Contracting") is None

    def test_whitespace_stripped(self):
        assert _parse_float("  52.5  ") == pytest.approx(52.5)


# ---------------------------------------------------------------------------
# _parse_month_header unit tests
# ---------------------------------------------------------------------------


class TestParseMonthHeader:
    def test_full_month_name(self):
        assert _parse_month_header("January 2025") == "2025-01-01"

    def test_abbreviated_month_name(self):
        assert _parse_month_header("Dec 2024") == "2024-12-01"

    def test_with_leading_whitespace(self):
        assert _parse_month_header("  March 2025  ") == "2025-03-01"

    def test_non_month_returns_none(self):
        assert _parse_month_header("Index") is None

    def test_empty_string_returns_none(self):
        assert _parse_month_header("") is None

    def test_year_only_returns_none(self):
        assert _parse_month_header("2025") is None


# ---------------------------------------------------------------------------
# _normalize_label unit tests
# ---------------------------------------------------------------------------


class TestNormalizeLabel:
    def test_strips_registered_trademark(self):
        assert _normalize_label("Manufacturing PMI®") == "manufacturing pmi"

    def test_lowercases(self):
        assert _normalize_label("New Orders") == "new orders"

    def test_strips_whitespace(self):
        assert _normalize_label("  Prices  ") == "prices"

    def test_strips_trademark_symbol(self):
        assert _normalize_label("PMI™") == "pmi"


# ---------------------------------------------------------------------------
# _build_column_map unit tests
# ---------------------------------------------------------------------------


class TestBuildColumnMap:
    def test_two_header_rows_produces_correct_value_cols(self):
        html = """
        <table>
          <thead>
            <tr>
              <th rowspan="2">Index</th>
              <th colspan="4">January 2025</th>
              <th colspan="4">December 2024</th>
            </tr>
            <tr>
              <th>Series Index</th><th>Series Direction</th>
              <th>Rate of Change</th><th>Trend (Months)</th>
              <th>Series Index</th><th>Series Direction</th>
              <th>Rate of Change</th><th>Trend (Months)</th>
            </tr>
          </thead>
        </table>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        col_map = _build_column_map(_header_rows(table))
        # col 0: label (None, False)
        # col 1: Jan value (2025-01-01, True)
        # col 2: Jan direction (2025-01-01, False)
        # col 3: Jan RoC (2025-01-01, False)
        # col 4: Jan Trend (2025-01-01, False)
        # col 5: Dec value (2024-12-01, True)
        # ...
        assert col_map[0] == (None, False)
        assert col_map[1] == ("2025-01-01", True)
        assert col_map[2] == ("2025-01-01", False)
        assert col_map[5] == ("2024-12-01", True)
        assert col_map[6] == ("2024-12-01", False)

    def test_single_header_row_all_month_cols_are_values(self):
        """When only one header row is present, any recognised month col is a value col."""
        html = """
        <table>
          <thead>
            <tr>
              <th>Index</th>
              <th>January 2025</th>
              <th>December 2024</th>
            </tr>
          </thead>
        </table>
        """
        table = BeautifulSoup(html, "lxml").find("table")
        col_map = _build_column_map(_header_rows(table))
        assert col_map[0] == (None, False)
        assert col_map[1] == ("2025-01-01", True)
        assert col_map[2] == ("2024-12-01", True)


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.ism_manufacturing_pmi.fetch", return_value=fake_resp) as mock_fetch,
            patch("src.scrapers.ism_manufacturing_pmi.time.sleep"),
        ):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with (
            patch("src.scrapers.ism_manufacturing_pmi.fetch", return_value=fake_resp),
            patch(
                "src.scrapers.ism_manufacturing_pmi.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >=3s; calls={sleep_calls}"

    def test_scrape_returns_correct_record_type(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.ism_manufacturing_pmi.fetch", return_value=fake_resp),
            patch("src.scrapers.ism_manufacturing_pmi.time.sleep"),
        ):
            records = scrape()

        assert all(isinstance(r, IsmManufacturingPmiRecord) for r in records)

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>No table</p></body></html>"

        with (
            patch("src.scrapers.ism_manufacturing_pmi.fetch", return_value=fake_resp),
            patch("src.scrapers.ism_manufacturing_pmi.time.sleep"),
        ):
            with pytest.raises(ValueError):
                scrape()
