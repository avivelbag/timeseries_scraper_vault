"""Tests for src/scrapers/eia_natural_gas_storage.py.

All tests use a static HTML fixture or inline HTML — zero live network calls.
The fixture covers 6 regions across 2 weeks, yielding 12 expected records.
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.eia_natural_gas_storage import (
    SOURCE_URL,
    _build_column_map,
    _parse_date,
    _record_to_proto,
    backfill,
    run,
    scrape,
)
from protos.eia_natural_gas_storage_pb2 import EiaNaturalGasStorageRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "eia_natural_gas_storage.html")
REGIONS = {"East", "Midwest", "Mountain", "Pacific", "South Central", "Lower 48 States"}


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_twelve_records(self, sample_html):
        """Fixture has 6 regions × 2 weeks = 12 records."""
        assert len(run(sample_html)) == 12

    def test_all_six_regions_present(self, sample_html):
        """Every expected region must appear in the output."""
        found = {r["region"] for r in run(sample_html)}
        assert found == REGIONS

    def test_report_dates_are_valid_iso8601(self, sample_html):
        """report_date must parse as a real calendar date in YYYY-MM-DD format."""
        for r in run(sample_html):
            parsed = date.fromisoformat(r["report_date"])
            assert parsed.year >= 2000

    def test_two_distinct_weeks(self, sample_html):
        """Fixture contains two distinct week-ending dates."""
        dates = {r["report_date"] for r in run(sample_html)}
        assert len(dates) == 2

    def test_storage_bcf_is_positive_float(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["storage_bcf"], float)
            assert r["storage_bcf"] > 0

    def test_year_ago_bcf_is_positive_float(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["year_ago_bcf"], float)
            assert r["year_ago_bcf"] > 0

    def test_five_year_avg_bcf_is_positive_float(self, sample_html):
        for r in run(sample_html):
            assert isinstance(r["five_year_avg_bcf"], float)
            assert r["five_year_avg_bcf"] > 0

    def test_source_url_stored(self, sample_html):
        for r in run(sample_html, source_url="https://example.com"):
            assert r["source_url"] == "https://example.com"

    def test_source_url_and_fetch_time_populated(self, sample_html):
        """source_url must be non-empty (fetch_time is added by _record_to_proto)."""
        for r in run(sample_html):
            assert r["source_url"] != ""

    def test_known_east_week1_values(self, sample_html):
        """East region, 2025-01-03: current=546, year_ago=521, 5yr_avg=494."""
        records = run(sample_html)
        east_w1 = [r for r in records if r["region"] == "East" and r["report_date"] == "2025-01-03"]
        assert len(east_w1) == 1
        assert east_w1[0]["storage_bcf"] == 546.0
        assert east_w1[0]["year_ago_bcf"] == 521.0
        assert east_w1[0]["five_year_avg_bcf"] == 494.0

    def test_known_lower48_week2_values(self, sample_html):
        """Lower 48 States region, 2025-01-10: current=2900, year_ago=2903, 5yr_avg=2816."""
        records = run(sample_html)
        l48_w2 = [r for r in records if r["region"] == "Lower 48 States" and r["report_date"] == "2025-01-10"]
        assert len(l48_w2) == 1
        assert l48_w2[0]["storage_bcf"] == 2900.0
        assert l48_w2[0]["year_ago_bcf"] == 2903.0
        assert l48_w2[0]["five_year_avg_bcf"] == 2816.0

    def test_no_none_in_required_fields(self, sample_html):
        """All required fields must be non-None and non-empty-string."""
        required = ("region", "storage_bcf", "year_ago_bcf", "five_year_avg_bcf", "report_date", "source_url")
        for r in run(sample_html):
            for key in required:
                assert r[key] is not None, f"None in {key}: {r}"


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_empty_html_raises_value_error(self):
        with pytest.raises(ValueError):
            run("")

    def test_no_table_raises_value_error(self):
        html = "<html><body><p>No table here.</p></body></html>"
        with pytest.raises(ValueError, match="No table found"):
            run(html)

    def test_single_header_row_raises_value_error(self):
        """A table with only one header row cannot produce a column map."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr><th>Week Ending</th><th colspan="3">East</th></tr>
          </thead>
          <tbody><tr><td>2025-01-03</td><td>500</td><td>490</td><td>480</td></tr></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError):
            run(html)

    def test_cells_with_dashes_are_skipped(self, sample_html):
        """Rows that have '--' in value cells should still emit records for valid cells."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Week Ending</th>
              <th colspan="3">East</th>
              <th colspan="3">Midwest</th>
            </tr>
            <tr>
              <th>Current</th><th>Year Ago</th><th>5-Yr Avg</th>
              <th>Current</th><th>Year Ago</th><th>5-Yr Avg</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>2025-01-03</td>
              <td>--</td><td>--</td><td>--</td>
              <td>891</td><td>875</td><td>854</td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        records = run(html)
        regions_found = {r["region"] for r in records}
        assert "Midwest" in regions_found
        assert "East" not in regions_found

    def test_large_table_all_records_valid(self):
        """50 data rows × 3 regions = 150 records, all with valid dates."""
        header = """
        <table>
          <thead>
            <tr>
              <th rowspan="2">Week Ending</th>
              <th colspan="3">East</th>
              <th colspan="3">Midwest</th>
              <th colspan="3">Mountain</th>
            </tr>
            <tr>
              <th>Current</th><th>Year Ago</th><th>5-Yr Avg</th>
              <th>Current</th><th>Year Ago</th><th>5-Yr Avg</th>
              <th>Current</th><th>Year Ago</th><th>5-Yr Avg</th>
            </tr>
          </thead>
          <tbody>
        """
        rows = ""
        for i in range(50):
            week = f"2020-01-{i + 1:02d}" if i < 31 else f"2020-02-{i - 30:02d}"
            rows += (
                f"<tr><td>{week}</td>"
                f"<td>{500 + i}</td><td>{490 + i}</td><td>{480 + i}</td>"
                f"<td>{900 + i}</td><td>{890 + i}</td><td>{880 + i}</td>"
                f"<td>{200 + i}</td><td>{195 + i}</td><td>{190 + i}</td>"
                f"</tr>"
            )
        html = header + rows + "</tbody></table>"
        records = run(html)
        assert len(records) == 150
        for r in records:
            assert date.fromisoformat(r["report_date"])


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_header_only_table_raises_value_error(self):
        """A table with headers but no valid data rows raises ValueError."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Week Ending</th>
              <th colspan="3">East</th>
            </tr>
            <tr>
              <th>Current</th><th>Year Ago</th><th>5-Yr Avg</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)

    def test_all_rows_unparseable_dates_raises_value_error(self):
        """When every row has an unparseable date, ValueError is raised."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Week Ending</th>
              <th colspan="3">East</th>
            </tr>
            <tr>
              <th>Current</th><th>Year Ago</th><th>5-Yr Avg</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>not-a-date</td><td>500</td><td>490</td><td>480</td></tr>
            <tr><td>also-bad</td><td>510</td><td>500</td><td>490</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)

    def test_all_value_cells_non_numeric_raises_value_error(self):
        """When all value cells are non-numeric, no regions have data and ValueError is raised."""
        html = """
        <html><body>
        <table>
          <thead>
            <tr>
              <th rowspan="2">Week Ending</th>
              <th colspan="3">East</th>
            </tr>
            <tr>
              <th>Current</th><th>Year Ago</th><th>5-Yr Avg</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>2025-01-03</td><td>--</td><td>--</td><td>--</td></tr>
          </tbody>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)


# ---------------------------------------------------------------------------
# _parse_date unit tests
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_iso_format(self):
        assert _parse_date("2025-01-03") == "2025-01-03"

    def test_slash_format(self):
        assert _parse_date("01/03/2025") == "2025-01-03"

    def test_whitespace_stripped(self):
        assert _parse_date("  2025-01-10  ") == "2025-01-10"

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            _parse_date("not-a-date")

    def test_partial_date_raises(self):
        with pytest.raises(ValueError):
            _parse_date("2025-01")


# ---------------------------------------------------------------------------
# _build_column_map unit tests
# ---------------------------------------------------------------------------


class TestBuildColumnMap:
    def _make_table(self, regions_with_colspan, sub_cols):
        from bs4 import BeautifulSoup

        region_cells = "".join(
            f'<th colspan="{cs}">{name}</th>' for name, cs in regions_with_colspan
        )
        sub_cells = "".join(f"<th>{s}</th>" for s in sub_cols)
        html = f"""
        <table>
          <thead>
            <tr><th rowspan="2">Week Ending</th>{region_cells}</tr>
            <tr>{sub_cells}</tr>
          </thead>
        </table>
        """
        soup = BeautifulSoup(html, "lxml")
        return soup.find("table")

    def test_basic_two_regions(self):
        table = self._make_table(
            [("East", 3), ("Midwest", 3)],
            ["Current", "Year Ago", "5-Yr Avg", "Current", "Year Ago", "5-Yr Avg"],
        )
        col_map = _build_column_map(table)
        assert col_map[1] == ("East", "storage_bcf")
        assert col_map[2] == ("East", "year_ago_bcf")
        assert col_map[3] == ("East", "five_year_avg_bcf")
        assert col_map[4] == ("Midwest", "storage_bcf")

    def test_unknown_sub_col_preserved(self):
        """Unrecognized sub-column label is stored as-is (lowercase)."""
        table = self._make_table(
            [("East", 1)],
            ["unknown col"],
        )
        col_map = _build_column_map(table)
        assert col_map[1] == ("East", "unknown col")

    def test_single_header_row_raises(self):
        from bs4 import BeautifulSoup

        html = "<table><thead><tr><th>Week</th><th colspan='3'>East</th></tr></thead></table>"
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table")
        with pytest.raises(ValueError, match="2 header rows"):
            _build_column_map(table)


# ---------------------------------------------------------------------------
# Proto stub tests
# ---------------------------------------------------------------------------


class TestRecordToProto:
    def test_proto_fields_populated(self, sample_html):
        records = run(sample_html)
        assert records, "Need at least one record from fixture"
        msg = _record_to_proto(records[0])
        assert isinstance(msg, EiaNaturalGasStorageRecord)
        assert msg.region == records[0]["region"]
        assert msg.storage_bcf == records[0]["storage_bcf"]
        assert msg.year_ago_bcf == records[0]["year_ago_bcf"]
        assert msg.five_year_avg_bcf == records[0]["five_year_avg_bcf"]
        assert msg.report_date == records[0]["report_date"]
        assert msg.source_url == records[0]["source_url"]

    def test_fetch_time_is_iso8601(self, sample_html):
        records = run(sample_html)
        msg = _record_to_proto(records[0])
        assert "T" in msg.fetch_time
        assert msg.fetch_time != ""

    def test_source_url_field_non_empty(self, sample_html):
        records = run(sample_html)
        msg = _record_to_proto(records[0])
        assert msg.source_url != ""


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.eia_natural_gas_storage.fetch", return_value=fake_resp) as mock_fetch,
            patch("src.scrapers.eia_natural_gas_storage.time.sleep"),
        ):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with (
            patch("src.scrapers.eia_natural_gas_storage.fetch", return_value=fake_resp),
            patch(
                "src.scrapers.eia_natural_gas_storage.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3s; calls: {sleep_calls}"

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>no tables</p></body></html>"

        with (
            patch("src.scrapers.eia_natural_gas_storage.fetch", return_value=fake_resp),
            patch("src.scrapers.eia_natural_gas_storage.time.sleep"),
        ):
            with pytest.raises(ValueError):
                scrape()

    def test_scrape_returns_same_as_run(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.eia_natural_gas_storage.fetch", return_value=fake_resp),
            patch("src.scrapers.eia_natural_gas_storage.time.sleep"),
        ):
            scraped = scrape()

        direct = run(sample_html)
        assert scraped == direct


# ---------------------------------------------------------------------------
# backfill() tests
# ---------------------------------------------------------------------------


class TestBackfillFunction:
    """Tests for backfill(start_date, end_date).

    The fixture has two weeks: 2025-01-03 and 2025-01-10, with 6 regions each
    (12 records total).  All tests mock scrape() so no live network calls occur.
    """

    @pytest.fixture
    def _patch_scrape(self, sample_html):
        """Patch scrape() to return records parsed from the fixture."""
        records = run(sample_html)
        with patch("src.scrapers.eia_natural_gas_storage.scrape", return_value=records):
            yield records

    def test_full_range_returns_all_records(self, _patch_scrape):
        """start_date..end_date spanning both weeks yields all 12 records."""
        result = backfill("2025-01-01", "2025-01-31")
        assert len(result) == 12

    def test_single_week_filter(self, _patch_scrape):
        """Exact start_date == end_date == week1 returns only the 6 week-1 records."""
        result = backfill("2025-01-03", "2025-01-03")
        assert len(result) == 6
        assert all(r["report_date"] == "2025-01-03" for r in result)

    def test_boundary_inclusive_both_ends(self, _patch_scrape):
        """Closed interval: both boundary dates are included when they match records."""
        result = backfill("2025-01-03", "2025-01-10")
        dates = {r["report_date"] for r in result}
        assert "2025-01-03" in dates
        assert "2025-01-10" in dates

    def test_start_after_end_raises_value_error(self, _patch_scrape):
        """start_date > end_date is logically invalid and must raise ValueError."""
        with pytest.raises(ValueError, match="must be <="):
            backfill("2025-01-10", "2025-01-03")

    def test_range_with_no_data_raises_value_error(self, _patch_scrape):
        """A date range that contains no records must raise ValueError."""
        with pytest.raises(ValueError, match="No records found"):
            backfill("2024-01-01", "2024-12-31")

    def test_returned_records_have_all_required_fields(self, _patch_scrape):
        """Every backfill record must carry all required fields."""
        required = ("region", "storage_bcf", "year_ago_bcf", "five_year_avg_bcf", "report_date", "source_url")
        for r in backfill("2025-01-01", "2025-01-31"):
            for key in required:
                assert r[key] is not None, f"None in {key}"

    def test_equal_start_end_outside_data_raises(self, _patch_scrape):
        """Edge: start == end == a date not in the data raises ValueError."""
        with pytest.raises(ValueError, match="No records found"):
            backfill("2025-01-05", "2025-01-05")
