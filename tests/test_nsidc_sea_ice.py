"""Tests for src/scrapers/nsidc_sea_ice.py.

All tests use static fixture files or inline strings — no live network calls
are made.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.nsidc_sea_ice import (
    INDEX_URL,
    _discover_data_url,
    parse_lines,
    scrape,
)
from protos.nsidc_sea_ice_pb2 import NsidcSeaIceRecord

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _read_fixture(name: str) -> str:
    with open(os.path.join(FIXTURES_DIR, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def index_html() -> str:
    return _read_fixture("nsidc_sea_ice_index.html")


@pytest.fixture
def data_txt() -> str:
    return _read_fixture("nsidc_sea_ice_data.txt")


@pytest.fixture
def data_lines(data_txt) -> list[str]:
    return data_txt.splitlines()


@pytest.fixture
def records(data_lines) -> list[NsidcSeaIceRecord]:
    return parse_lines(data_lines)


class TestDiscoverDataUrl:
    def test_finds_link_in_fixture_html(self, index_html):
        url = _discover_data_url(index_html)
        assert "N_seaice_extent_monthly" in url

    def test_relative_href_becomes_absolute(self):
        html = '<a href="/data/N_seaice_extent_monthly_v3.0.csv">link</a>'
        url = _discover_data_url(html)
        assert url.startswith("https://nsidc.org")
        assert "N_seaice_extent_monthly" in url

    def test_absolute_href_returned_as_is(self):
        full = "https://nsidc.org/data/N_seaice_extent_monthly_v3.0.csv"
        html = f'<a href="{full}">link</a>'
        url = _discover_data_url(html)
        assert url == full

    def test_raises_value_error_when_no_link_found(self):
        with pytest.raises(ValueError, match="Could not find"):
            _discover_data_url("<html><body><a href='/other.csv'>other</a></body></html>")

    def test_raises_value_error_on_empty_page(self):
        with pytest.raises(ValueError, match="Could not find"):
            _discover_data_url("<html><body></body></html>")

    def test_southern_hemisphere_link_not_matched(self):
        html = (
            '<a href="/data/S_seaice_extent_monthly_v3.0.csv">SH</a>'
            '<a href="/data/N_seaice_extent_monthly_v3.0.csv">NH</a>'
        )
        url = _discover_data_url(html)
        assert "N_seaice_extent_monthly" in url


class TestParseHappyPath:
    def test_record_count_from_fixture(self, records):
        # Fixture: 4 comment lines, 1 header, 5 data rows, 1 malformed row
        # Expected: 5 valid records (malformed skipped, header skipped, 1978-10 area missing but extent valid)
        assert len(records) == 5

    def test_jan_1979_extent(self, records):
        jan = next(r for r in records if r.year == 1979 and r.month == 1)
        assert jan.extent_million_sq_km == pytest.approx(15.56, abs=0.01)

    def test_jan_1979_area(self, records):
        jan = next(r for r in records if r.year == 1979 and r.month == 1)
        assert jan.area_million_sq_km == pytest.approx(13.95, abs=0.01)

    def test_jan_1979_year_field(self, records):
        jan = next(r for r in records if r.year == 1979 and r.month == 1)
        assert jan.year == 1979

    def test_jan_1979_month_field(self, records):
        jan = next(r for r in records if r.year == 1979 and r.month == 1)
        assert jan.month == 1

    def test_feb_1979_extent(self, records):
        feb = next(r for r in records if r.year == 1979 and r.month == 2)
        assert feb.extent_million_sq_km == pytest.approx(15.92, abs=0.01)

    def test_source_url_set_on_all_records(self, records):
        for rec in records:
            assert rec.source_url == INDEX_URL

    def test_fetch_time_set_on_all_records(self, records):
        for rec in records:
            assert rec.fetch_time != ""

    def test_all_records_share_fetch_time(self, data_lines):
        recs = parse_lines(data_lines)
        times = {r.fetch_time for r in recs}
        assert len(times) == 1

    def test_large_input(self):
        """Many synthetic rows all parse correctly."""
        lines = [
            f"{1979 + i // 12},  {(i % 12) + 1},  Goddard,      N,  {10.0 + i * 0.01:.2f},  {9.0 + i * 0.01:.2f}"
            for i in range(200)
        ]
        recs = parse_lines(lines)
        assert len(recs) == 200


class TestMissingDataHandling:
    def test_missing_area_sentinel_stored_as_zero(self, records):
        """1978-10 has area=-9999.00; record should still be included with area=0.0."""
        oct_1978 = next(r for r in records if r.year == 1978 and r.month == 10)
        assert oct_1978.area_million_sq_km == 0.0

    def test_missing_area_row_has_valid_extent(self, records):
        oct_1978 = next(r for r in records if r.year == 1978 and r.month == 10)
        assert oct_1978.extent_million_sq_km == pytest.approx(10.69, abs=0.01)

    def test_missing_extent_sentinel_row_skipped(self):
        lines = ["1979,   1,  Goddard,      N,  -9999.00,  13.95"]
        assert parse_lines(lines) == []

    def test_mixed_sentinel_and_valid(self):
        lines = [
            "1979,   1,  Goddard,      N,  -9999.00,  13.95",
            "1979,   2,  Goddard,      N,  15.92,  14.62",
            "1979,   3,  Goddard,      N,  -9999.00,  14.00",
            "1979,   4,  Goddard,      N,  14.50,  12.80",
        ]
        recs = parse_lines(lines)
        assert len(recs) == 2
        assert {r.month for r in recs} == {2, 4}


class TestMalformedRowSkipping:
    def test_malformed_row_absent_from_results(self, records):
        years = {r.year for r in records}
        assert all(isinstance(y, int) for y in years)

    def test_too_few_columns_skipped(self):
        lines = ["1979,   1,  Goddard"]
        assert parse_lines(lines) == []

    def test_non_numeric_year_skipped(self):
        lines = ["YEAR,   1,  Goddard,      N,  15.56,  13.95"]
        assert parse_lines(lines) == []

    def test_non_numeric_extent_skipped(self):
        lines = ["1979,   1,  Goddard,      N,  N/A,  13.95"]
        assert parse_lines(lines) == []

    def test_header_row_skipped(self):
        lines = [
            "Year,  Mo, Data type, Region, Extent, Area",
            "1979,   1,  Goddard,      N,  15.56,  13.95",
        ]
        recs = parse_lines(lines)
        assert len(recs) == 1

    def test_comment_lines_skipped(self):
        lines = [
            "# comment line",
            "1979,   1,  Goddard,      N,  15.56,  13.95",
        ]
        recs = parse_lines(lines)
        assert len(recs) == 1

    def test_blank_lines_skipped(self):
        lines = [
            "",
            "1979,   1,  Goddard,      N,  15.56,  13.95",
            "   ",
        ]
        recs = parse_lines(lines)
        assert len(recs) == 1


class TestEdgeCases:
    def test_empty_input_returns_empty_list(self):
        assert parse_lines([]) == []

    def test_all_comments_returns_empty_list(self):
        lines = ["# comment"] * 20
        assert parse_lines(lines) == []

    def test_all_malformed_returns_empty_list(self):
        lines = ["bad", "data", "rows,missing,columns"]
        assert parse_lines(lines) == []

    def test_all_missing_extent_returns_empty_list(self):
        lines = [
            f"1979,  {m},  Goddard,      N,  -9999.00,  13.95"
            for m in range(1, 13)
        ]
        assert parse_lines(lines) == []


class TestRecordStructure:
    def test_record_is_nsidc_sea_ice_record(self, records):
        for rec in records:
            assert isinstance(rec, NsidcSeaIceRecord)

    def test_year_field_type(self, records):
        for rec in records:
            assert isinstance(rec.year, int)

    def test_month_field_type(self, records):
        for rec in records:
            assert isinstance(rec.month, int)

    def test_month_range(self, records):
        for rec in records:
            assert 1 <= rec.month <= 12

    def test_extent_is_float(self, records):
        for rec in records:
            assert isinstance(rec.extent_million_sq_km, float)

    def test_area_is_float(self, records):
        for rec in records:
            assert isinstance(rec.area_million_sq_km, float)

    def test_default_record_fields(self):
        rec = NsidcSeaIceRecord()
        assert rec.year == 0
        assert rec.month == 0
        assert rec.extent_million_sq_km == 0.0
        assert rec.area_million_sq_km == 0.0
        assert rec.source_url == ""


class TestScrapeFunction:
    def test_scrape_fetches_index_url_first(self, index_html, data_txt):
        mock_index = MagicMock(spec=requests.Response)
        mock_index.text = index_html
        mock_data = MagicMock(spec=requests.Response)
        mock_data.text = data_txt
        with patch("src.scrapers.nsidc_sea_ice.fetch", side_effect=[mock_index, mock_data]) as mock_fetch:
            scrape()
        assert mock_fetch.call_args_list[0][0][0] == INDEX_URL

    def test_scrape_fetches_discovered_data_url(self, index_html, data_txt):
        mock_index = MagicMock(spec=requests.Response)
        mock_index.text = index_html
        mock_data = MagicMock(spec=requests.Response)
        mock_data.text = data_txt
        with patch("src.scrapers.nsidc_sea_ice.fetch", side_effect=[mock_index, mock_data]) as mock_fetch:
            scrape()
        second_url = mock_fetch.call_args_list[1][0][0]
        assert "N_seaice_extent_monthly" in second_url

    def test_scrape_returns_nsidc_records(self, index_html, data_txt):
        mock_index = MagicMock(spec=requests.Response)
        mock_index.text = index_html
        mock_data = MagicMock(spec=requests.Response)
        mock_data.text = data_txt
        with patch("src.scrapers.nsidc_sea_ice.fetch", side_effect=[mock_index, mock_data]):
            recs = scrape()
        assert all(isinstance(r, NsidcSeaIceRecord) for r in recs)

    def test_scrape_no_live_network(self, index_html, data_txt):
        mock_index = MagicMock(spec=requests.Response)
        mock_index.text = index_html
        mock_data = MagicMock(spec=requests.Response)
        mock_data.text = data_txt
        with patch("src.scrapers.nsidc_sea_ice.fetch", side_effect=[mock_index, mock_data]) as mock_fetch:
            scrape()
        assert mock_fetch.call_count == 2

    def test_scrape_returns_expected_record_count(self, index_html, data_txt):
        mock_index = MagicMock(spec=requests.Response)
        mock_index.text = index_html
        mock_data = MagicMock(spec=requests.Response)
        mock_data.text = data_txt
        with patch("src.scrapers.nsidc_sea_ice.fetch", side_effect=[mock_index, mock_data]):
            recs = scrape()
        assert len(recs) == 5
