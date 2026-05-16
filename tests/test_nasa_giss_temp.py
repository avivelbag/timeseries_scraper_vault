"""Tests for src/scrapers/nasa_giss_temp.py.

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

from src.scrapers.nasa_giss_temp import (
    SOURCE_URL,
    parse_lines,
    scrape,
)
from protos.nasa_giss_temp_pb2 import NasaGissTempRecord

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "nasa_giss_temp_sample.txt"
)


@pytest.fixture
def fixture_lines() -> list[str]:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return fh.read().splitlines()


@pytest.fixture
def fixture_records(fixture_lines) -> list[NasaGissTempRecord]:
    return parse_lines(fixture_lines)


class TestParseHappyPath:
    def test_returns_records(self, fixture_records):
        assert len(fixture_records) > 0

    def test_total_record_count(self, fixture_records):
        # 5 full years × 12 months + 4 months for 2024 (May–Dec are ****)
        assert len(fixture_records) == 64

    def test_1880_jan_anomaly(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 1880 and r.month == 1)
        assert rec.anomaly_c == pytest.approx(-0.29)

    def test_2023_jan_anomaly_approx_1_1c(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 2023 and r.month == 1)
        assert rec.anomaly_c == pytest.approx(1.12)

    def test_2023_dec_anomaly(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 2023 and r.month == 12)
        assert rec.anomaly_c == pytest.approx(1.27)

    def test_2000_mar_anomaly(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 2000 and r.month == 3)
        assert rec.anomaly_c == pytest.approx(0.56)

    def test_2022_sep_anomaly(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 2022 and r.month == 9)
        assert rec.anomaly_c == pytest.approx(0.44)

    def test_year_month_formatting(self, fixture_records):
        """year_month must be YYYY-MM for all records."""
        for rec in fixture_records:
            assert len(rec.year_month) == 7
            datetime.strptime(rec.year_month, "%Y-%m")

    def test_1880_jan_year_month(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 1880 and r.month == 1)
        assert rec.year_month == "1880-01"

    def test_2023_jun_year_month(self, fixture_records):
        rec = next(r for r in fixture_records if r.year == 2023 and r.month == 6)
        assert rec.year_month == "2023-06"

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

    def test_year_and_month_fields_match_year_month_string(self, fixture_records):
        for rec in fixture_records:
            expected = f"{rec.year}-{rec.month:02d}"
            assert rec.year_month == expected


class TestSentinelHandling:
    def test_sentinel_cells_silently_skipped(self, fixture_records):
        """2024 May–Dec are **** in the fixture; only Jan–Apr should appear."""
        recs_2024 = [r for r in fixture_records if r.year == 2024]
        months_2024 = {r.month for r in recs_2024}
        assert months_2024 == {1, 2, 3, 4}

    def test_no_record_with_sentinel_value(self, fixture_lines):
        records = parse_lines(fixture_lines)
        for rec in records:
            assert rec.anomaly_c != float("inf")

    def test_inline_sentinel_skipped(self):
        lines = ["2020   100  ****  200  ****  300  400  500  600  700  800  900  1000"]
        records = parse_lines(lines)
        months = {r.month for r in records}
        assert months == {1, 3, 5, 6, 7, 8, 9, 10, 11, 12}
        assert 2 not in months
        assert 4 not in months


class TestEdgeCases:
    def test_empty_input_returns_empty_list(self):
        assert parse_lines([]) == []

    def test_blank_lines_skipped(self):
        lines = ["", "   ", "\t"]
        assert parse_lines(lines) == []

    def test_header_lines_skipped(self):
        lines = [
            "GLOBAL Surface Temperature Change (C) Analysis by GISS",
            "Based on GHCN v4/ERSST v5",
            "Anomaly with respect to 1951-1980",
            "Year   Jan  Feb  Mar",
        ]
        assert parse_lines(lines) == []

    def test_year_line_with_no_monthly_data_produces_no_records(self):
        lines = ["2020"]
        assert parse_lines(lines) == []

    def test_partial_year_fewer_than_12_months(self):
        lines = ["2024   135  125  175"]
        records = parse_lines(lines)
        assert len(records) == 3
        assert {r.month for r in records} == {1, 2, 3}

    def test_all_months_sentinel_produces_no_records_for_year(self):
        parts = ["2025"] + ["****"] * 12
        lines = [" ".join(parts)]
        records = parse_lines(lines)
        assert records == []

    def test_large_input_all_valid(self):
        """100 years × 12 months should produce 1200 records."""
        lines = [
            f"{year}   10   20   30   40   50   60   70   80   90  100  110  120"
            for year in range(1900, 2000)
        ]
        records = parse_lines(lines)
        assert len(records) == 1200

    def test_non_integer_non_sentinel_cell_skipped(self):
        lines = ["2020   100  1.5  200  300  400  500  600  700  800  900 1000 1100"]
        records = parse_lines(lines)
        months = {r.month for r in records}
        assert 2 not in months

    def test_negative_anomaly_correct_sign(self):
        lines = ["1960  -50  -30  -20   10   20   30   40   50   60   70   80   90"]
        records = parse_lines(lines)
        jan = next(r for r in records if r.month == 1)
        assert jan.anomaly_c == pytest.approx(-0.50)

    def test_3digit_year_not_parsed(self):
        lines = ["880    10   20   30   40   50   60   70   80   90  100  110  120"]
        assert parse_lines(lines) == []

    def test_5digit_year_not_parsed(self):
        lines = ["20230   10   20   30   40   50   60   70   80   90  100  110  120"]
        assert parse_lines(lines) == []


class TestRecordStructure:
    def test_record_is_nasa_giss_temp_record(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec, NasaGissTempRecord)

    def test_year_field_type(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.year, int)

    def test_month_field_type(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.month, int)

    def test_month_range(self, fixture_records):
        for rec in fixture_records:
            assert 1 <= rec.month <= 12

    def test_year_month_is_str(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.year_month, str)

    def test_anomaly_c_is_float(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.anomaly_c, float)

    def test_default_record_fields(self):
        rec = NasaGissTempRecord()
        assert rec.year == 0
        assert rec.month == 0
        assert rec.year_month == ""
        assert rec.anomaly_c == 0.0
        assert rec.source_url == ""
        assert rec.fetch_time == ""


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, fixture_lines):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "\n".join(fixture_lines)
        with patch("src.scrapers.nasa_giss_temp.fetch", return_value=fake_resp) as mock_fetch:
            records = scrape()
        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) > 0

    def test_scrape_returns_same_as_parse_lines(self, fixture_lines):
        fake_resp = MagicMock(spec=requests.Response)
        text = "\n".join(fixture_lines)
        fake_resp.text = text
        with patch("src.scrapers.nasa_giss_temp.fetch", return_value=fake_resp):
            scraped = scrape()
        direct = parse_lines(text.splitlines())
        assert len(scraped) == len(direct)
        for s, d in zip(scraped, direct):
            assert s.year == d.year
            assert s.month == d.month
            assert s.year_month == d.year_month
            assert s.anomaly_c == pytest.approx(d.anomaly_c)

    def test_scrape_no_live_network(self, fixture_lines):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = "\n".join(fixture_lines)
        with patch("src.scrapers.nasa_giss_temp.fetch", return_value=fake_resp) as mock_fetch:
            scrape()
        assert mock_fetch.call_count == 1
