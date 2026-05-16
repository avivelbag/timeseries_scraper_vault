"""Tests for src/scrapers/noaa_co2.py.

All tests use a static text fixture or inline strings — no live network calls
are made.
"""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.noaa_co2 import (
    SOURCE_URL,
    _MIN_RECORDS,
    parse_lines,
    scrape,
)
from protos.noaa_co2_pb2 import NoaaCo2Record

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "noaa_co2_sample.txt"
)


@pytest.fixture
def fixture_lines() -> list[str]:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return fh.read().splitlines()


@pytest.fixture
def fixture_records(fixture_lines) -> list[NoaaCo2Record]:
    return parse_lines(fixture_lines)


def _make_data_lines(n: int) -> list[str]:
    """Generate n syntactically valid CO2 data lines for threshold tests."""
    return [
        f"{1958 + i // 12}   {(i % 12) + 1}   {1958.0 + i / 12:.4f}   "
        f"{315.0 + i * 0.01:.2f}   {315.0 + i * 0.01:.2f}   {314.0 + i * 0.01:.2f}   28"
        for i in range(n)
    ]


class TestParseHappyPath:
    def test_record_count_from_fixture(self, fixture_records):
        # 10 data rows in fixture minus 1 row with -99.99 sentinel = 9
        assert len(fixture_records) == 9

    def test_1958_march_year_field(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 1958 and r.month == 3)
        assert rec.year == 1958

    def test_1958_march_month_field(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 1958 and r.month == 3)
        assert rec.month == 3

    def test_1958_march_monthly_avg_ppm(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 1958 and r.month == 3)
        assert rec.monthly_avg_ppm == pytest.approx(315.71)

    def test_1958_march_deseasonalized_ppm(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 1958 and r.month == 3)
        assert rec.deseasonalized_ppm == pytest.approx(314.62)

    def test_1958_march_decimal_year(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 1958 and r.month == 3)
        assert rec.decimal_year == pytest.approx(1958.2083)

    def test_2023_december_monthly_avg_ppm(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 2023 and r.month == 12)
        assert rec.monthly_avg_ppm == pytest.approx(419.40)

    def test_2023_december_deseasonalized_ppm(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 2023 and r.month == 12)
        assert rec.deseasonalized_ppm == pytest.approx(416.79)

    def test_source_url_set_on_all_records(self, fixture_records):
        for rec in fixture_records:
            assert rec.source_url == SOURCE_URL

    def test_fetch_time_is_iso8601(self, fixture_records):
        for rec in fixture_records:
            datetime.fromisoformat(rec.fetch_time)

    def test_all_records_share_same_fetch_time(self, fixture_lines):
        records = parse_lines(fixture_lines)
        times = {r.fetch_time for r in records}
        assert len(times) == 1

    def test_large_input_all_valid(self):
        """800 synthetic rows should produce 800 records."""
        lines = _make_data_lines(800)
        records = parse_lines(lines)
        assert len(records) == 800


class TestCommentLineSkipping:
    def test_fixture_comment_lines_skipped(self, fixture_records):
        """Fixture has many '#' header lines; none should appear as records."""
        assert all(isinstance(r, NoaaCo2Record) for r in fixture_records)

    def test_inline_comment_line_skipped(self):
        lines = [
            "# yr  mon   decimal    average  interpolated  trend  #days",
            "1958   3    1958.2083     315.71       315.71   314.62     -1",
        ]
        records = parse_lines(lines)
        assert len(records) == 1
        assert records[0].year == 1958

    def test_comment_with_no_data_returns_empty(self):
        lines = [
            "# This is a comment",
            "# Another comment line",
            "#  yr  mon   decimal    average  interpolated  trend  #days",
        ]
        assert parse_lines(lines) == []

    def test_blank_lines_skipped(self):
        lines = ["", "   ", "\t"]
        assert parse_lines(lines) == []


class TestSentinelHandling:
    def test_fixture_june_1958_skipped(self, fixture_records):
        """1958-06 has monthly_avg -99.99 in fixture and must be absent."""
        june_1958 = [r for r in fixture_records if r.year == 1958 and r.month == 6]
        assert june_1958 == []

    def test_sentinel_row_not_included(self):
        lines = ["1960   1   1960.0417   -99.99   316.00   315.00   28"]
        records = parse_lines(lines)
        assert records == []

    def test_non_sentinel_row_included(self):
        lines = ["1960   1   1960.0417   316.43   316.43   315.12   28"]
        records = parse_lines(lines)
        assert len(records) == 1
        assert records[0].monthly_avg_ppm == pytest.approx(316.43)

    def test_mixed_sentinel_and_valid(self):
        lines = [
            "1960   1   1960.0417   -99.99   316.00   315.00   28",
            "1960   2   1960.1250   316.43   316.43   315.12   28",
            "1960   3   1960.2083   -99.99   317.00   316.00   -1",
            "1960   4   1960.2917   317.20   317.20   316.30   25",
        ]
        records = parse_lines(lines)
        assert len(records) == 2
        assert {r.month for r in records} == {2, 4}


class TestTruncatedInputError:
    def test_scrape_raises_value_error_on_truncated_response(self):
        """scrape() must raise ValueError when fewer than 700 records are parsed."""
        few_lines = _make_data_lines(10)
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "\n".join(few_lines)
        with patch("src.scrapers.noaa_co2.fetch", return_value=fake_resp):
            with pytest.raises(ValueError, match=str(_MIN_RECORDS)):
                scrape()

    def test_scrape_does_not_raise_on_sufficient_records(self):
        """scrape() succeeds when at least 700 records are parsed."""
        many_lines = _make_data_lines(_MIN_RECORDS)
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "\n".join(many_lines)
        with patch("src.scrapers.noaa_co2.fetch", return_value=fake_resp):
            records = scrape()
        assert len(records) == _MIN_RECORDS

    def test_parse_lines_empty_input_returns_empty_list(self):
        assert parse_lines([]) == []

    def test_parse_lines_all_comments_returns_empty_list(self):
        lines = ["# comment"] * 50
        assert parse_lines(lines) == []

    def test_parse_lines_all_sentinel_returns_empty_list(self):
        lines = [
            f"1958   {m}   1958.{m:04d}   -99.99   317.00   316.00   -1"
            for m in range(1, 13)
        ]
        assert parse_lines(lines) == []

    def test_parse_lines_malformed_rows_skipped(self):
        lines = [
            "not a data row",
            "1958   3",
            "1958   3   1958.2083   abc   315.71   314.62   -1",
        ]
        assert parse_lines(lines) == []


class TestRecordStructure:
    def test_record_is_noaa_co2_record(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec, NoaaCo2Record)

    def test_year_field_type(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.year, int)

    def test_month_field_type(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.month, int)

    def test_month_range(self, fixture_records):
        for rec in fixture_records:
            assert 1 <= rec.month <= 12

    def test_decimal_year_is_float(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.decimal_year, float)

    def test_monthly_avg_ppm_is_float(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.monthly_avg_ppm, float)

    def test_deseasonalized_ppm_is_float(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.deseasonalized_ppm, float)

    def test_default_record_fields(self):
        rec = NoaaCo2Record()
        assert rec.year == 0
        assert rec.month == 0
        assert rec.decimal_year == 0.0
        assert rec.monthly_avg_ppm == 0.0
        assert rec.deseasonalized_ppm == 0.0
        assert rec.source_url == ""
        assert rec.fetch_time == ""


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self):
        many_lines = _make_data_lines(_MIN_RECORDS)
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "\n".join(many_lines)
        with patch("src.scrapers.noaa_co2.fetch", return_value=fake_resp) as mock_fetch:
            scrape()
        mock_fetch.assert_called_once_with(SOURCE_URL)

    def test_scrape_no_live_network(self):
        many_lines = _make_data_lines(_MIN_RECORDS)
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "\n".join(many_lines)
        with patch("src.scrapers.noaa_co2.fetch", return_value=fake_resp) as mock_fetch:
            scrape()
        assert mock_fetch.call_count == 1

    def test_scrape_returns_noaa_co2_records(self):
        many_lines = _make_data_lines(_MIN_RECORDS)
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "\n".join(many_lines)
        with patch("src.scrapers.noaa_co2.fetch", return_value=fake_resp):
            records = scrape()
        assert all(isinstance(r, NoaaCo2Record) for r in records)
