"""Tests for src/scrapers/census_housing_starts.py.

All tests use a static HTML fixture or inline HTML — zero live network calls.
The fixture contains 2 region tables (Northeast, Midwest), each with 3 structure-type
rows and 3 period columns, yielding 18 expected records.
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.census_housing_starts import (
    SOURCE_URL,
    _build_column_map,
    _is_footnote_row,
    _parse_period_date,
    _parse_numeric,
    _record_to_proto,
    run,
    scrape,
)
from protos.census_housing_starts_pb2 import CensusHousingStartsRecord

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "census_housing_starts.html")


@pytest.fixture
def sample_html() -> str:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Happy-path tests against the fixture
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_at_least_six_records(self, sample_html):
        """Fixture has 2 tables × 3 rows × 3 periods = 18 records."""
        assert len(run(sample_html)) >= 6

    def test_returns_eighteen_records(self, sample_html):
        assert len(run(sample_html)) == 18

    def test_period_dates_are_valid_iso8601(self, sample_html):
        """All period_date values must parse as real calendar dates in YYYY-MM-DD format."""
        import re
        pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for r in run(sample_html):
            assert pattern.match(r["period_date"]), f"Bad date: {r['period_date']!r}"
            parsed = date.fromisoformat(r["period_date"])
            assert parsed.year >= 2000

    def test_period_dates_are_first_of_month(self, sample_html):
        """period_date must always be the first day of the month."""
        for r in run(sample_html):
            assert r["period_date"].endswith("-01"), f"Not first of month: {r['period_date']!r}"

    def test_all_numeric_fields_are_non_negative_floats(self, sample_html):
        """All four numeric metrics must be non-negative floats."""
        numeric_keys = (
            "starts_thousands",
            "permits_thousands",
            "completions_thousands",
            "under_construction_thousands",
        )
        for r in run(sample_html):
            for key in numeric_keys:
                assert isinstance(r[key], float), f"{key} not float: {r[key]!r}"
                assert r[key] >= 0.0, f"{key} is negative: {r[key]}"

    def test_three_distinct_period_dates(self, sample_html):
        """Fixture spans 3 periods: Jan 2024 (current), Dec 2023 (prior), Jan 2023 (year ago)."""
        dates = {r["period_date"] for r in run(sample_html)}
        assert len(dates) == 3
        assert "2024-01-01" in dates
        assert "2023-12-01" in dates
        assert "2023-01-01" in dates

    def test_two_regions_present(self, sample_html):
        regions = {r["region"] for r in run(sample_html)}
        assert len(regions) == 2

    def test_three_structure_types_present(self, sample_html):
        types = {r["structure_type"] for r in run(sample_html)}
        assert "1 unit" in types
        assert "2 to 4 units" in types
        assert "5 units or more" in types

    def test_source_url_stored(self, sample_html):
        for r in run(sample_html, source_url="https://example.com"):
            assert r["source_url"] == "https://example.com"

    def test_known_northeast_1unit_current(self, sample_html):
        """Northeast / 1 unit / Jan 2024: starts=90, permits=95, completions=92, uc=400."""
        records = run(sample_html)
        match = [
            r for r in records
            if "northeast" in r["region"].lower()
            and r["structure_type"] == "1 unit"
            and r["period_date"] == "2024-01-01"
        ]
        assert len(match) == 1
        assert match[0]["starts_thousands"] == 90.0
        assert match[0]["permits_thousands"] == 95.0
        assert match[0]["completions_thousands"] == 92.0
        assert match[0]["under_construction_thousands"] == 400.0

    def test_known_midwest_5plus_prior(self, sample_html):
        """Midwest / 5 units or more / Dec 2023: starts=70, permits=76, completions=65, uc=290."""
        records = run(sample_html)
        match = [
            r for r in records
            if "midwest" in r["region"].lower()
            and r["structure_type"] == "5 units or more"
            and r["period_date"] == "2023-12-01"
        ]
        assert len(match) == 1
        assert match[0]["starts_thousands"] == 70.0
        assert match[0]["permits_thousands"] == 76.0
        assert match[0]["completions_thousands"] == 65.0
        assert match[0]["under_construction_thousands"] == 290.0

    def test_footnote_rows_skipped(self, sample_html):
        """No record's structure_type should be a Census footnote (digit followed by '/')."""
        import re
        footnote_pattern = re.compile(r"^\d+/")
        for r in run(sample_html):
            assert not footnote_pattern.match(r["structure_type"]), (
                f"Footnote row leaked into records: {r['structure_type']!r}"
            )

    def test_no_none_in_required_fields(self, sample_html):
        required = (
            "period_date", "region", "structure_type",
            "starts_thousands", "permits_thousands",
            "completions_thousands", "under_construction_thousands",
            "source_url",
        )
        for r in run(sample_html):
            for key in required:
                assert r[key] is not None, f"None in {key}: {r}"


# ---------------------------------------------------------------------------
# Edge-case tests
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def test_empty_html_raises_value_error(self):
        """run() must raise ValueError when given an empty string."""
        with pytest.raises(ValueError):
            run("")

    def test_whitespace_only_html_raises_value_error(self):
        with pytest.raises(ValueError):
            run("   \n\t  ")

    def test_no_table_raises_value_error(self):
        html = "<html><body><p>No tables here.</p></body></html>"
        with pytest.raises(ValueError):
            run(html)

    def test_table_without_region_heading_raises_value_error(self):
        """A table with no single-colspan region heading row yields no records → ValueError."""
        html = """
        <html><body>
        <table>
          <tr><th></th><th colspan="3">Housing Starts</th></tr>
          <tr><th></th><th>Jan 2024</th><th>Dec 2023</th><th>Jan 2023</th></tr>
          <tr><td>1 unit</td><td>100</td><td>95</td><td>98</td></tr>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError):
            run(html)

    def test_cells_with_dashes_skipped(self):
        """Blank/dash cells for a metric produce 0.0 default, and valid cells still parse."""
        html = """
        <html><body>
        <table>
          <tr><th colspan="4">South</th></tr>
          <tr>
            <th></th>
            <th colspan="3">Housing Starts</th>
          </tr>
          <tr>
            <th></th>
            <th>Jan 2024 (p)</th><th>Dec 2023 (r)</th><th>Jan 2023</th>
          </tr>
          <tr>
            <td>1 unit</td>
            <td>200</td><td>--</td><td>195</td>
          </tr>
        </table>
        </body></html>
        """
        records = run(html)
        assert any(r["starts_thousands"] == 200.0 for r in records)
        assert any(r["starts_thousands"] == 195.0 for r in records)
        dates = {r["period_date"] for r in records}
        assert "2024-01-01" in dates
        assert "2023-01-01" in dates

    def test_numbers_with_commas_parsed_correctly(self):
        """Numeric cells with thousands-separator commas must parse correctly."""
        html = """
        <html><body>
        <table>
          <tr><th colspan="4">West</th></tr>
          <tr><th></th><th colspan="3">Housing Starts</th></tr>
          <tr><th></th><th>Jan 2024 (p)</th><th>Dec 2023 (r)</th><th>Jan 2023</th></tr>
          <tr><td>1 unit</td><td>1,234</td><td>1,100</td><td>1,050</td></tr>
        </table>
        </body></html>
        """
        records = run(html)
        starts = {r["starts_thousands"] for r in records}
        assert 1234.0 in starts
        assert 1100.0 in starts
        assert 1050.0 in starts

    def test_large_table_all_records_valid(self):
        """A table with 20 structure-type rows × 3 periods = 60 records, all valid."""
        data_rows = ""
        for i in range(20):
            data_rows += (
                f"<tr><td>Type {i}</td>"
                f"<td>{100 + i}</td><td>{95 + i}</td><td>{90 + i}</td>"
                f"</tr>"
            )
        html = f"""
        <html><body>
        <table>
          <tr><th colspan="4">Northeast</th></tr>
          <tr><th></th><th colspan="3">Housing Starts</th></tr>
          <tr><th></th><th>Jan 2024 (p)</th><th>Dec 2023 (r)</th><th>Jan 2023</th></tr>
          {data_rows}
        </table>
        </body></html>
        """
        records = run(html)
        assert len(records) == 60
        for r in records:
            assert date.fromisoformat(r["period_date"])
            assert r["starts_thousands"] >= 0.0


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_header_only_table_raises_value_error(self):
        """A table with headers but no data rows raises ValueError."""
        html = """
        <html><body>
        <table>
          <tr><th colspan="4">Northeast</th></tr>
          <tr><th></th><th colspan="3">Housing Starts</th></tr>
          <tr><th></th><th>Jan 2024</th><th>Dec 2023</th><th>Jan 2023</th></tr>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)

    def test_all_rows_are_footnotes_raises_value_error(self):
        """When every data row is a footnote, ValueError is raised."""
        html = """
        <html><body>
        <table>
          <tr><th colspan="4">Northeast</th></tr>
          <tr><th></th><th colspan="3">Housing Starts</th></tr>
          <tr><th></th><th>Jan 2024 (p)</th><th>Dec 2023 (r)</th><th>Jan 2023</th></tr>
          <tr><td>1/ See notes.</td><td></td><td></td><td></td></tr>
          <tr><td>2/ Revised figures.</td><td></td><td></td><td></td></tr>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)

    def test_no_recognised_metric_columns_raises_value_error(self):
        """A table whose metric row has no recognisable metric names yields no records → ValueError."""
        html = """
        <html><body>
        <table>
          <tr><th colspan="4">Northeast</th></tr>
          <tr><th></th><th colspan="3">FooBar Metric</th></tr>
          <tr><th></th><th>Jan 2024 (p)</th><th>Dec 2023 (r)</th><th>Jan 2023</th></tr>
          <tr><td>1 unit</td><td>90</td><td>85</td><td>88</td></tr>
        </table>
        </body></html>
        """
        with pytest.raises(ValueError, match="No records extracted"):
            run(html)


# ---------------------------------------------------------------------------
# _parse_period_date unit tests
# ---------------------------------------------------------------------------


class TestParsePeriodDate:
    def test_abbreviated_month(self):
        assert _parse_period_date("Jan 2024") == "2024-01-01"

    def test_full_month_name(self):
        assert _parse_period_date("January 2024") == "2024-01-01"

    def test_strips_preliminary_annotation(self):
        assert _parse_period_date("Jan 2024 (p)") == "2024-01-01"

    def test_strips_revised_annotation(self):
        assert _parse_period_date("Dec 2023 (r)") == "2023-12-01"

    def test_year_ago(self):
        assert _parse_period_date("Jan 2023") == "2023-01-01"

    def test_december(self):
        assert _parse_period_date("December 2022") == "2022-12-01"

    def test_invalid_raises_value_error(self):
        with pytest.raises(ValueError):
            _parse_period_date("Q1 2024")

    def test_iso_date_raises_value_error(self):
        with pytest.raises(ValueError):
            _parse_period_date("2024-01-15")


# ---------------------------------------------------------------------------
# _parse_numeric unit tests
# ---------------------------------------------------------------------------


class TestParseNumeric:
    def test_plain_integer(self):
        assert _parse_numeric("90") == 90.0

    def test_with_commas(self):
        assert _parse_numeric("1,234") == 1234.0

    def test_with_decimal(self):
        assert _parse_numeric("1,234.5") == 1234.5

    def test_with_revised_annotation(self):
        assert _parse_numeric("85 (r)") == 85.0

    def test_blank_returns_none(self):
        assert _parse_numeric("") is None

    def test_dash_returns_none(self):
        assert _parse_numeric("--") is None

    def test_na_returns_none(self):
        assert _parse_numeric("NA") is None

    def test_non_numeric_returns_none(self):
        assert _parse_numeric("n/a") is None


# ---------------------------------------------------------------------------
# _is_footnote_row unit tests
# ---------------------------------------------------------------------------


class TestIsFootnoteRow:
    def _make_cells(self, texts: list[str]):
        from bs4 import BeautifulSoup
        html = "<tr>" + "".join(f"<td>{t}</td>" for t in texts) + "</tr>"
        soup = BeautifulSoup(html, "lxml")
        return soup.find("tr").find_all("td")

    def test_digit_start_is_footnote(self):
        cells = self._make_cells(["1/ See notes.", "", ""])
        assert _is_footnote_row(cells) is True

    def test_all_blank_non_label_is_footnote(self):
        cells = self._make_cells(["Note", "", "--", "NA"])
        assert _is_footnote_row(cells) is True

    def test_valid_data_row_not_footnote(self):
        cells = self._make_cells(["1 unit", "90", "85", "88"])
        assert _is_footnote_row(cells) is False

    def test_empty_cells_list_is_footnote(self):
        assert _is_footnote_row([]) is True


# ---------------------------------------------------------------------------
# _build_column_map unit tests
# ---------------------------------------------------------------------------


class TestBuildColumnMap:
    def _make_rows(self, metric_html: str, period_html: str):
        from bs4 import BeautifulSoup
        html = f"<table><tr>{metric_html}</tr><tr>{period_html}</tr></table>"
        soup = BeautifulSoup(html, "lxml")
        rows = soup.find("table").find_all("tr")
        return rows[0], rows[1]

    def test_basic_single_metric(self):
        metric_row, period_row = self._make_rows(
            '<th></th><th colspan="3">Housing Starts</th>',
            "<th></th><th>Jan 2024 (p)</th><th>Dec 2023 (r)</th><th>Jan 2023</th>",
        )
        col_map, period_dates = _build_column_map(metric_row, period_row)
        assert col_map[1] == ("starts_thousands", 0)
        assert col_map[2] == ("starts_thousands", 1)
        assert col_map[3] == ("starts_thousands", 2)
        assert period_dates == ["2024-01-01", "2023-12-01", "2023-01-01"]

    def test_all_four_metrics(self):
        metric_row, period_row = self._make_rows(
            '<th></th>'
            '<th colspan="3">Housing Starts</th>'
            '<th colspan="3">Building Permits</th>'
            '<th colspan="3">Housing Completions</th>'
            '<th colspan="3">Units Under Construction</th>',
            "<th></th>"
            + "<th>Jan 2024</th><th>Dec 2023</th><th>Jan 2023</th>" * 4,
        )
        col_map, period_dates = _build_column_map(metric_row, period_row)
        assert col_map[1] == ("starts_thousands", 0)
        assert col_map[4] == ("permits_thousands", 0)
        assert col_map[7] == ("completions_thousands", 0)
        assert col_map[10] == ("under_construction_thousands", 0)
        assert len(period_dates) == 3

    def test_period_dates_deduplicated_to_n_periods(self):
        """Period row repeats dates for each metric; only first n_periods are captured."""
        metric_row, period_row = self._make_rows(
            '<th></th><th colspan="2">Housing Starts</th>',
            "<th></th><th>Jan 2024</th><th>Dec 2023</th><th>Jan 2024</th><th>Dec 2023</th>",
        )
        _, period_dates = _build_column_map(metric_row, period_row)
        assert len(period_dates) == 2
        assert period_dates[0] == "2024-01-01"
        assert period_dates[1] == "2023-12-01"


# ---------------------------------------------------------------------------
# Proto stub tests
# ---------------------------------------------------------------------------


class TestRecordToProto:
    def test_proto_fields_populated(self, sample_html):
        records = run(sample_html)
        assert records, "Need at least one record from fixture"
        msg = _record_to_proto(records[0])
        assert isinstance(msg, CensusHousingStartsRecord)
        assert msg.period_date == records[0]["period_date"]
        assert msg.region == records[0]["region"]
        assert msg.structure_type == records[0]["structure_type"]
        assert msg.starts_thousands == records[0]["starts_thousands"]
        assert msg.permits_thousands == records[0]["permits_thousands"]
        assert msg.completions_thousands == records[0]["completions_thousands"]
        assert msg.under_construction_thousands == records[0]["under_construction_thousands"]
        assert msg.source_url == records[0]["source_url"]

    def test_fetch_time_is_iso8601(self, sample_html):
        records = run(sample_html)
        msg = _record_to_proto(records[0])
        assert "T" in msg.fetch_time
        assert msg.fetch_time != ""

    def test_source_url_non_empty(self, sample_html):
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
            patch("src.scrapers.census_housing_starts.fetch", return_value=fake_resp) as mock_fetch,
            patch("src.scrapers.census_housing_starts.time.sleep"),
        ):
            records = scrape()

        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_sleeps_at_least_3_seconds(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html
        sleep_calls: list[float] = []

        with (
            patch("src.scrapers.census_housing_starts.fetch", return_value=fake_resp),
            patch(
                "src.scrapers.census_housing_starts.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
        ):
            scrape()

        assert any(s >= 3 for s in sleep_calls), f"No sleep >= 3s; calls: {sleep_calls}"

    def test_scrape_propagates_value_error(self):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "<html><body><p>no tables</p></body></html>"

        with (
            patch("src.scrapers.census_housing_starts.fetch", return_value=fake_resp),
            patch("src.scrapers.census_housing_starts.time.sleep"),
        ):
            with pytest.raises(ValueError):
                scrape()

    def test_scrape_returns_same_as_run(self, sample_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = sample_html

        with (
            patch("src.scrapers.census_housing_starts.fetch", return_value=fake_resp),
            patch("src.scrapers.census_housing_starts.time.sleep"),
        ):
            scraped = scrape()

        direct = run(sample_html)
        assert scraped == direct
