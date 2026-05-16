"""Tests for src/scrapers/fed_g17_industrial_production.py.

All tests use static HTML fixtures or inline HTML — zero live network calls.
The fixture contains 2 tables:
  Table 1: Total Industry, Manufacturing, Mining × 3 months × 2 sub-series = 9 group-date pairs
  Table 2: Durable Goods, Nondurable Goods × 3 months × 2 sub-series = 6 group-date pairs
Total = 15 records, 2 of which contain n.a. sentinel values.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.fed_g17_industrial_production import (
    SOURCE_URL,
    _SENTINEL,
    _build_column_schema,
    _classify_subseries,
    _is_suppressed,
    _parse_value,
    run,
    scrape,
)
from protos.fed_g17_industrial_production_pb2 import FedG17Record

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "fed_g17_sample.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_records_from_fixture(self, sample_html):
        """Fixture should yield at least 3 data records covering multiple series."""
        records = run(sample_html)
        assert len(records) >= 3

    def test_correct_total_record_count(self, sample_html):
        """Fixture has 2 tables × 3 months × number-of-groups = 15 records."""
        records = run(sample_html)
        assert len(records) == 15

    def test_reference_dates_are_yyyy_mm(self, sample_html):
        """All reference_date values must match YYYY-MM format."""
        import re
        pattern = re.compile(r"^\d{4}-\d{2}$")
        for rec in run(sample_html):
            assert pattern.match(rec.reference_date), f"Bad date: {rec.reference_date!r}"

    def test_all_known_dates_present(self, sample_html):
        """Fixture spans 2024-10, 2024-11, 2024-12."""
        dates = {rec.reference_date for rec in run(sample_html)}
        assert "2024-10" in dates
        assert "2024-11" in dates
        assert "2024-12" in dates

    def test_total_industry_series_present(self, sample_html):
        """Total Industry group must appear in the records."""
        series_ids = {rec.series_id for rec in run(sample_html)}
        assert any("Total Industry" in sid for sid in series_ids)

    def test_manufacturing_series_present(self, sample_html):
        """Manufacturing group must appear in the records."""
        series_ids = {rec.series_id for rec in run(sample_html)}
        assert any("Manufacturing" in sid for sid in series_ids)

    def test_known_index_value_total_industry_oct(self, sample_html):
        """Total Industry / 2024-10 index_value must be 102.4 (from fixture)."""
        records = run(sample_html)
        match = [
            r for r in records
            if "Total Industry" in r.series_id and r.reference_date == "2024-10"
        ]
        assert len(match) == 1
        assert match[0].index_value == pytest.approx(102.4)

    def test_capacity_utilization_populated_where_available(self, sample_html):
        """Total Industry / 2024-10 must have capacity_utilization_pct = 78.6."""
        records = run(sample_html)
        match = [
            r for r in records
            if "Total Industry" in r.series_id and r.reference_date == "2024-10"
        ]
        assert len(match) == 1
        assert match[0].capacity_utilization_pct == pytest.approx(78.6)

    def test_suppressed_cells_yield_sentinel(self, sample_html):
        """Mining / 2024-12 has n.a. cells; index_value must be -1.0 (sentinel)."""
        records = run(sample_html)
        match = [
            r for r in records
            if "Mining" in r.series_id and r.reference_date == "2024-12"
        ]
        assert len(match) == 1
        assert match[0].index_value == pytest.approx(_SENTINEL)

    def test_suppressed_utilization_is_none(self, sample_html):
        """Mining / 2024-12 n.a. utilization cell must produce capacity_utilization_pct=None."""
        records = run(sample_html)
        match = [
            r for r in records
            if "Mining" in r.series_id and r.reference_date == "2024-12"
        ]
        assert len(match) == 1
        assert match[0].capacity_utilization_pct is None

    def test_source_url_stored(self, sample_html):
        """source_url must be stored in every record."""
        for rec in run(sample_html, source_url="https://example.com"):
            assert rec.source_url == "https://example.com"

    def test_fetch_time_is_iso8601(self, sample_html):
        """fetch_time must be a non-empty ISO-8601 string."""
        for rec in run(sample_html):
            assert "T" in rec.fetch_time
            assert rec.fetch_time != ""

    def test_all_records_are_fed_g17_record_instances(self, sample_html):
        """Every returned object must be a FedG17Record."""
        for rec in run(sample_html):
            assert isinstance(rec, FedG17Record)

    def test_no_footnote_records_emitted(self, sample_html):
        """Footnote rows (1/, 2/) must not produce records."""
        import re
        pattern = re.compile(r"^\d+/")
        for rec in run(sample_html):
            assert not pattern.match(rec.reference_date), (
                f"Footnote leaked as date: {rec.reference_date!r}"
            )

    def test_unit_field_populated(self, sample_html):
        """unit field must be non-empty for all records."""
        for rec in run(sample_html):
            assert rec.unit != ""

    def test_series_id_uses_dot_separator(self, sample_html):
        """series_id should contain a dot separating group from sub-series."""
        for rec in run(sample_html):
            assert "." in rec.series_id, f"No dot in series_id: {rec.series_id!r}"


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_empty_html_raises_value_error(self):
        """run() must raise ValueError for empty input."""
        with pytest.raises(ValueError):
            run("")

    def test_whitespace_only_html_raises_value_error(self):
        with pytest.raises(ValueError):
            run("   \n\t  ")

    def test_no_tables_raises_value_error(self):
        html = "<html><body><p>No data here.</p></body></html>"
        with pytest.raises(ValueError):
            run(html)

    def test_table_without_two_row_thead_skipped(self):
        """A table with a single-row thead yields no records, causing ValueError."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Period</th><th>Total</th></tr></thead>
          <tbody><tr><td>2024-10</td><td>102.4</td></tr></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError):
            run(html)

    def test_large_table_all_months_parsed(self):
        """A table with 24 monthly rows must produce 24 records (one group)."""
        rows = ""
        for year in range(2022, 2024):
            for month in range(1, 13):
                rows += f"<tr><td>{year}-{month:02d}</td><td>{100 + month}</td><td>{75 + month}</td></tr>"
        html = f"""
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Period</th>
              <th colspan="2">Total Industry</th>
            </tr>
            <tr>
              <th>Index</th>
              <th>% of capacity</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 24
        for rec in records:
            assert rec.index_value > 0
            assert rec.capacity_utilization_pct is not None

    def test_all_na_cells_produce_sentinel(self):
        """All n.a. cells must produce index_value=-1.0, not raise."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Period</th>
              <th colspan="2">Total Industry</th>
            </tr>
            <tr><th>Index</th><th>% of capacity</th></tr>
          </thead>
          <tbody>
            <tr><td>2024-01</td><td>n.a.</td><td>n.a.</td></tr>
            <tr><td>2024-02</td><td>n.a.</td><td>n.a.</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 2
        for rec in records:
            assert rec.index_value == pytest.approx(_SENTINEL)
            assert rec.capacity_utilization_pct is None

    def test_mixed_valid_and_na_rows_no_crash(self):
        """Mix of valid values and n.a. within same table must not crash."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Period</th>
              <th colspan="2">Manufacturing</th>
            </tr>
            <tr><th>Index</th><th>% of capacity</th></tr>
          </thead>
          <tbody>
            <tr><td>2024-01</td><td>100.5</td><td>76.0</td></tr>
            <tr><td>2024-02</td><td>n.a.</td><td>n.a.</td></tr>
            <tr><td>2024-03</td><td>101.2</td><td>77.1</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 3
        valid = [r for r in records if r.index_value != _SENTINEL]
        sentinel = [r for r in records if r.index_value == _SENTINEL]
        assert len(valid) == 2
        assert len(sentinel) == 1


# ---------------------------------------------------------------------------
# Failure-mode / error behavior tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_header_only_table_raises_value_error(self):
        """A table with headers but no data rows raises ValueError."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Period</th>
              <th colspan="2">Total Industry</th>
            </tr>
            <tr><th>Index</th><th>% of capacity</th></tr>
          </thead>
          <tbody></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)

    def test_no_date_rows_raises_value_error(self):
        """Rows whose first cell is not YYYY-MM are skipped, causing ValueError."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Period</th>
              <th colspan="2">Total Industry</th>
            </tr>
            <tr><th>Index</th><th>% of capacity</th></tr>
          </thead>
          <tbody>
            <tr><td>January 2024</td><td>102.4</td><td>78.6</td></tr>
            <tr><td colspan="3">1/ Footnote text here.</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)

    def test_suppressed_cells_do_not_raise(self, sample_html):
        """Parsing fixture with n.a. cells must not raise any exception."""
        try:
            run(sample_html)
        except Exception as exc:
            pytest.fail(f"run() raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# _parse_value unit tests
# ---------------------------------------------------------------------------


class TestParseValue:
    def test_plain_float(self):
        assert _parse_value("102.4") == pytest.approx(102.4)

    def test_with_comma(self):
        assert _parse_value("1,234.5") == pytest.approx(1234.5)

    def test_na_returns_sentinel(self):
        assert _parse_value("n.a.") == pytest.approx(_SENTINEL)

    def test_blank_returns_sentinel(self):
        assert _parse_value("") == pytest.approx(_SENTINEL)

    def test_dash_returns_sentinel(self):
        assert _parse_value("--") == pytest.approx(_SENTINEL)

    def test_footnote_marker_returns_sentinel(self):
        assert _parse_value("1/ preliminary") == pytest.approx(_SENTINEL)

    def test_non_numeric_returns_sentinel(self):
        assert _parse_value("n/a") == pytest.approx(_SENTINEL)


# ---------------------------------------------------------------------------
# _is_suppressed unit tests
# ---------------------------------------------------------------------------


class TestIsSuppressed:
    def test_blank_is_suppressed(self):
        assert _is_suppressed("") is True

    def test_na_is_suppressed(self):
        assert _is_suppressed("n.a.") is True

    def test_dash_is_suppressed(self):
        assert _is_suppressed("--") is True

    def test_valid_number_not_suppressed(self):
        assert _is_suppressed("102.4") is False


# ---------------------------------------------------------------------------
# _classify_subseries unit tests
# ---------------------------------------------------------------------------


class TestClassifySubseries:
    def test_index_label(self):
        assert _classify_subseries("Index") == "index"

    def test_percent_of_capacity(self):
        assert _classify_subseries("% of capacity") == "utilization"

    def test_percent_of_capacity_mixed_case(self):
        assert _classify_subseries("Percent of Capacity") == "utilization"

    def test_utilization_keyword(self):
        assert _classify_subseries("Utilization Rate") == "utilization"

    def test_unknown_label(self):
        result = _classify_subseries("SomethingElse")
        assert result == "somethingelse"


# ---------------------------------------------------------------------------
# _build_column_schema unit tests
# ---------------------------------------------------------------------------


class TestBuildColumnSchema:
    def _make_rows(self, row1_html: str, row2_html: str):
        from bs4 import BeautifulSoup
        html = f"<table><thead><tr>{row1_html}</tr><tr>{row2_html}</tr></thead></table>"
        soup = BeautifulSoup(html, "lxml")
        rows = soup.find("thead").find_all("tr")
        return rows[0], rows[1]

    def test_single_group_two_subs(self):
        row1, row2 = self._make_rows(
            '<th rowspan="2">Period</th><th colspan="2">Total Industry</th>',
            "<th>Index</th><th>% of capacity</th>",
        )
        schema = _build_column_schema(row1, row2)
        assert len(schema) == 2
        assert schema[0] == ("Total Industry", "Index", 1)
        assert schema[1] == ("Total Industry", "% of capacity", 2)

    def test_two_groups(self):
        row1, row2 = self._make_rows(
            '<th rowspan="2">Period</th>'
            '<th colspan="2">Total Industry</th>'
            '<th colspan="2">Manufacturing</th>',
            "<th>Index</th><th>% of capacity</th><th>Index</th><th>% of capacity</th>",
        )
        schema = _build_column_schema(row1, row2)
        assert len(schema) == 4
        groups = {s[0] for s in schema}
        assert "Total Industry" in groups
        assert "Manufacturing" in groups

    def test_col_indices_start_at_one(self):
        row1, row2 = self._make_rows(
            '<th rowspan="2">Period</th><th colspan="2">Mining</th>',
            "<th>Index</th><th>% of capacity</th>",
        )
        schema = _build_column_schema(row1, row2)
        col_indices = [s[2] for s in schema]
        assert col_indices[0] == 1
        assert col_indices[1] == 2


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.fed_g17_industrial_production.fetch", return_value=fake_resp) as mock_fetch,
            patch("src.scrapers.fed_g17_industrial_production.time.sleep"),
        ):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with (
            patch("src.scrapers.fed_g17_industrial_production.fetch", return_value=fake_resp),
            patch(
                "src.scrapers.fed_g17_industrial_production.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3s; calls: {sleep_calls}"

    def test_scrape_returns_fed_g17_records(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.fed_g17_industrial_production.fetch", return_value=fake_resp),
            patch("src.scrapers.fed_g17_industrial_production.time.sleep"),
        ):
            records = scrape()

        assert all(isinstance(r, FedG17Record) for r in records)

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>no tables</p></body></html>"

        with (
            patch("src.scrapers.fed_g17_industrial_production.fetch", return_value=fake_resp),
            patch("src.scrapers.fed_g17_industrial_production.time.sleep"),
        ):
            with pytest.raises(ValueError):
                scrape()
