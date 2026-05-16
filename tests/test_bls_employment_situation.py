"""Tests for src/scrapers/bls_employment_situation.py.

All tests use the static fixture at tests/fixtures/bls_empsit.html — no live
network calls are made.  The fixture contains two tables with multi-level
colspan/rowspan headers, covering the four target series plus suppressed
'(1)' cells, blank cells, 'p' (preliminary) and 'r' (revised) markers, and
&nbsp;-indented subcategory rows.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.bls_employment_situation import (
    SOURCE_URL,
    REQUIRED_FIELDS,
    _build_header_grid,
    _col_periods,
    _match_series,
    _parse_cell_value,
    _record_to_proto,
    run,
    scrape,
)
from protos.bls_employment_situation_pb2 import BLSEmploymentRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "bls_empsit.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def records(sample_html: str) -> list[dict]:
    return run(sample_html)


class TestRunHappyPath:
    def test_returns_expected_record_count(self, records):
        # 4 target series × 6 periods (Oct–Dec 2024 + Jan–Mar 2025) = 24
        assert len(records) == 24

    def test_all_required_fields_present(self, records):
        required = set(REQUIRED_FIELDS)
        for r in records:
            assert required.issubset(r.keys()), f"Missing keys in: {r}"

    def test_all_period_years_valid(self, records):
        for r in records:
            assert isinstance(r["period_year"], int)
            assert r["period_year"] in (2024, 2025)

    def test_all_period_months_in_range(self, records):
        for r in records:
            assert 1 <= r["period_month"] <= 12

    def test_all_values_are_positive_floats(self, records):
        for r in records:
            assert isinstance(r["value"], float)
            assert r["value"] > 0

    def test_source_url_stored_in_all_records(self, records):
        for r in records:
            assert r["source_url"] == SOURCE_URL

    def test_all_four_series_present(self, records):
        series_ids = {r["series_id"] for r in records}
        assert "CES0000000001" in series_ids, "Missing total nonfarm payrolls"
        assert "LNS14000000" in series_ids, "Missing unemployment rate"
        assert "CES0500000003" in series_ids, "Missing average hourly earnings"
        assert "CES0500000002" in series_ids, "Missing average weekly hours"

    def test_unemployment_rate_jan_2025(self, records):
        """Unemployment rate Jan 2025 must be 4.0 with preliminary=False."""
        match = [
            r for r in records
            if r["series_id"] == "LNS14000000"
            and r["period_year"] == 2025
            and r["period_month"] == 1
        ]
        assert len(match) == 1
        assert abs(match[0]["value"] - 4.0) < 1e-6
        assert match[0]["preliminary"] is False

    def test_total_nonfarm_mar_2025_preliminary(self, records):
        """Total nonfarm Mar 2025 must be 160142.0 with preliminary=True."""
        match = [
            r for r in records
            if r["series_id"] == "CES0000000001"
            and r["period_year"] == 2025
            and r["period_month"] == 3
        ]
        assert len(match) == 1
        assert abs(match[0]["value"] - 160142.0) < 1e-6
        assert match[0]["preliminary"] is True

    def test_hourly_earnings_mar_2025_preliminary(self, records):
        """Average hourly earnings Mar 2025 must be 35.63 with preliminary=True."""
        match = [
            r for r in records
            if r["series_id"] == "CES0500000003"
            and r["period_year"] == 2025
            and r["period_month"] == 3
        ]
        assert len(match) == 1
        assert abs(match[0]["value"] - 35.63) < 1e-6
        assert match[0]["preliminary"] is True

    def test_weekly_hours_jan_2025_not_preliminary(self, records):
        """Average weekly hours Jan 2025 must be 34.2 with preliminary=False."""
        match = [
            r for r in records
            if r["series_id"] == "CES0500000002"
            and r["period_year"] == 2025
            and r["period_month"] == 1
        ]
        assert len(match) == 1
        assert abs(match[0]["value"] - 34.2) < 1e-6
        assert match[0]["preliminary"] is False

    def test_revised_marker_stripped_and_not_preliminary(self, records):
        """'159,760r' must parse to 159760.0 with preliminary=False."""
        match = [
            r for r in records
            if r["series_id"] == "CES0000000001"
            and r["period_year"] == 2024
            and r["period_month"] == 12
        ]
        assert len(match) == 1
        assert abs(match[0]["value"] - 159760.0) < 1e-6
        assert match[0]["preliminary"] is False

    def test_six_periods_covered(self, records):
        """Fixture covers Oct–Dec 2024 and Jan–Mar 2025."""
        periods = {(r["period_year"], r["period_month"]) for r in records}
        expected = {
            (2024, 10), (2024, 11), (2024, 12),
            (2025, 1), (2025, 2), (2025, 3),
        }
        assert expected == periods

    def test_units_are_correct_per_series(self, records):
        for r in records:
            if r["series_id"] == "CES0000000001":
                assert r["units"] == "thousands"
            elif r["series_id"] == "LNS14000000":
                assert r["units"] == "percent"
            elif r["series_id"] == "CES0500000003":
                assert r["units"] == "dollars"
            elif r["series_id"] == "CES0500000002":
                assert r["units"] == "hours"


class TestRunEdgeCases:
    def test_empty_string_returns_empty_list(self):
        assert run("") == []

    def test_html_with_no_tables_returns_empty_list(self):
        assert run("<html><body><p>No tables here</p></body></html>") == []

    def test_single_row_header_table_skipped(self):
        """Tables with only one header row cannot resolve (year, month) — skip."""
        html = """
        <html><body>
        <table>
          <thead><tr><th>Measure</th><th>Oct</th><th>Nov</th></tr></thead>
          <tbody>
            <tr><td>Unemployment rate (percent)</td><td>4.1</td><td>4.2</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        assert run(html) == []

    def test_no_matching_series_returns_empty_list(self):
        """Table with correct header structure but no target series rows."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr><th rowspan="2">Category</th><th colspan="2">2025</th></tr>
            <tr><th>Jan.</th><th>Feb.</th></tr>
          </thead>
          <tbody>
            <tr><td>Some other series</td><td>100.0</td><td>101.0</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        assert run(html) == []

    def test_suppressed_cells_skipped(self, sample_html):
        """Cells with '(1)' must not produce any records."""
        records = run(sample_html)
        series_labels = {r["series_label"] for r in records}
        assert "Suppressed series (1)" not in series_labels

    def test_blank_cells_skipped(self, sample_html):
        """Cells with empty text must not produce any records."""
        records = run(sample_html)
        series_labels = {r["series_label"] for r in records}
        assert "Blank series" not in series_labels

    def test_deduplication_across_tables(self, sample_html):
        """Total nonfarm appears in both Table A and B-1; only one set kept."""
        records = run(sample_html)
        nonfarm = [r for r in records if r["series_id"] == "CES0000000001"]
        assert len(nonfarm) == 6, "Expected exactly 6 records (one per period)"
        periods = [(r["period_year"], r["period_month"]) for r in nonfarm]
        assert len(periods) == len(set(periods)), "Duplicate (year, month) found"

    def test_nbsp_indented_rows_not_matched_as_target(self, sample_html):
        """'  Total private' and '  Government' must not match target series."""
        records = run(sample_html)
        labels = {r["series_label"] for r in records}
        assert "Total private" not in labels
        assert "Government" not in labels
        assert "Manufacturing" not in labels

    def test_large_synthetic_table(self):
        """Stress test: 10 years × 12 months of nonfarm data parsed correctly."""
        year_cells = "".join(f'<th colspan="12">{y}</th>' for y in range(2015, 2025))
        month_headers = "".join(
            f"<th>{m}</th>"
            for m in ["Jan.", "Feb.", "Mar.", "Apr.", "May.", "Jun.",
                      "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec."] * 10
        )
        data_cells = "".join(f"<td>{150000 + i}</td>" for i in range(120))
        html = f"""
        <html><body>
        <table>
          <thead>
            <tr><th rowspan="2">Measure</th>{year_cells}</tr>
            <tr>{month_headers}</tr>
          </thead>
          <tbody>
            <tr><td>Total nonfarm employment (thousands)</td>{data_cells}</tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 120
        assert all(r["series_id"] == "CES0000000001" for r in records)


class TestParseCellValue:
    def test_plain_float(self):
        assert _parse_cell_value("4.1") == (4.1, False)

    def test_thousands_comma(self):
        value, pre = _parse_cell_value("159,437")
        assert abs(value - 159437.0) < 1e-6
        assert pre is False

    def test_preliminary_p_marker(self):
        value, pre = _parse_cell_value("4.2p")
        assert abs(value - 4.2) < 1e-6
        assert pre is True

    def test_revised_r_marker_not_preliminary(self):
        value, pre = _parse_cell_value("159,760r")
        assert abs(value - 159760.0) < 1e-6
        assert pre is False

    def test_suppressed_returns_none(self):
        assert _parse_cell_value("(1)") == (None, False)

    def test_blank_returns_none(self):
        assert _parse_cell_value("") == (None, False)

    def test_whitespace_only_returns_none(self):
        assert _parse_cell_value("   ") == (None, False)

    def test_dash_returns_none(self):
        assert _parse_cell_value("—") == (None, False)

    def test_uppercase_p_preliminary(self):
        value, pre = _parse_cell_value("35.63P")
        assert abs(value - 35.63) < 1e-6
        assert pre is True


class TestMatchSeries:
    def test_unemployment_rate_matched(self):
        result = _match_series("Unemployment rate (percent)")
        assert result is not None
        assert result["series_id"] == "LNS14000000"

    def test_total_nonfarm_matched(self):
        result = _match_series("Total nonfarm employment (thousands)")
        assert result is not None
        assert result["series_id"] == "CES0000000001"

    def test_total_nonfarm_short_label_matched(self):
        result = _match_series("Total nonfarm")
        assert result is not None
        assert result["series_id"] == "CES0000000001"

    def test_hourly_earnings_matched(self):
        result = _match_series("Average hourly earnings, private (dollars)")
        assert result is not None
        assert result["series_id"] == "CES0500000003"

    def test_weekly_hours_matched(self):
        result = _match_series("Average weekly hours, private (hours)")
        assert result is not None
        assert result["series_id"] == "CES0500000002"

    def test_nbsp_prefix_stripped(self):
        result = _match_series("\xa0\xa0Total nonfarm")
        assert result is not None
        assert result["series_id"] == "CES0000000001"

    def test_unrelated_label_returns_none(self):
        assert _match_series("Total private employment") is None

    def test_empty_label_returns_none(self):
        assert _match_series("") is None


class TestBuildHeaderGrid:
    def test_rowspan_fills_lower_rows(self):
        from bs4 import BeautifulSoup

        html = """
        <table>
          <thead>
            <tr><th rowspan="2">Label</th><th colspan="2">2025</th></tr>
            <tr><th>Jan.</th><th>Feb.</th></tr>
          </thead>
        </table>
        """
        soup = BeautifulSoup(html, "lxml")
        rows = soup.find("thead").find_all("tr")
        grid = _build_header_grid(rows)
        assert grid[(0, 0)] == "Label"
        assert grid[(1, 0)] == "Label"  # propagated by rowspan
        assert grid[(0, 1)] == "2025"
        assert grid[(0, 2)] == "2025"  # propagated by colspan
        assert grid[(1, 1)] == "Jan."
        assert grid[(1, 2)] == "Feb."

    def test_col_periods_two_row_header(self):
        from bs4 import BeautifulSoup

        html = """
        <table>
          <thead>
            <tr><th rowspan="2">Measure</th><th colspan="2">2024</th><th colspan="1">2025</th></tr>
            <tr><th>Nov.</th><th>Dec.</th><th>Jan.</th></tr>
          </thead>
        </table>
        """
        soup = BeautifulSoup(html, "lxml")
        rows = soup.find("thead").find_all("tr")
        grid = _build_header_grid(rows)
        periods = _col_periods(grid, 2)
        assert periods[1] == (2024, 11)
        assert periods[2] == (2024, 12)
        assert periods[3] == (2025, 1)
        assert 0 not in periods  # label column excluded


class TestProtoFieldPopulation:
    def test_proto_fields_populated(self, records):
        assert records, "Need at least one record for proto test"
        msg = _record_to_proto(records[0])
        r = records[0]
        assert isinstance(msg, BLSEmploymentRecord)
        assert msg.period_year == r["period_year"]
        assert msg.period_month == r["period_month"]
        assert msg.series_id == r["series_id"]
        assert msg.series_label == r["series_label"]
        assert abs(msg.value - r["value"]) < 1e-9
        assert msg.units == r["units"]
        assert msg.preliminary == r["preliminary"]
        assert msg.source_url == SOURCE_URL

    def test_fetch_time_is_iso8601_string(self, records):
        msg = _record_to_proto(records[0])
        assert isinstance(msg.fetch_time, str)
        assert len(msg.fetch_time) > 0
        assert "T" in msg.fetch_time  # ISO-8601 datetime separator

    def test_proto_preliminary_propagated(self, records):
        preliminary_records = [r for r in records if r["preliminary"]]
        assert preliminary_records, "Fixture must have at least one preliminary record"
        msg = _record_to_proto(preliminary_records[0])
        assert msg.preliminary is True

    def test_proto_non_preliminary_propagated(self, records):
        non_preliminary = [r for r in records if not r["preliminary"]]
        assert non_preliminary
        msg = _record_to_proto(non_preliminary[0])
        assert msg.preliminary is False


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.bls_employment_situation.fetch", return_value=fake_resp) as mock_fetch, \
             patch("src.scrapers.bls_employment_situation.time.sleep"):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) == 24

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        sleep_calls: list[float] = []
        with patch("src.scrapers.bls_employment_situation.fetch", return_value=fake_resp), \
             patch("src.scrapers.bls_employment_situation.time.sleep",
                   side_effect=lambda s: sleep_calls.append(s)):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3 s; calls: {sleep_calls}"

    def test_scrape_returns_same_as_run(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with patch("src.scrapers.bls_employment_situation.fetch", return_value=fake_resp), \
             patch("src.scrapers.bls_employment_situation.time.sleep"):
            scraped = scrape()

        direct = run(sample_html)
        assert scraped == direct
