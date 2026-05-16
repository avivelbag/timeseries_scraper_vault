"""Tests for src/scrapers/cftc_cot.py.

All tests use the static HTML fixture at tests/fixtures/cftc_cot_sample.html
or inline HTML strings.  No live network calls are made.
"""

import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scrapers.cftc_cot import (
    HISTORICAL_URL_TEMPLATE,
    SOURCE_URL,
    _extract_report_date,
    _find_pre_with_data,
    parse_html,
    scrape,
    scrape_range,
    scrape_year,
)
from protos.cftc_cot_pb2 import CotRecord
from bs4 import BeautifulSoup

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "cftc_cot_sample.html"
)


@pytest.fixture
def fixture_html() -> str:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def fixture_records(fixture_html) -> list[CotRecord]:
    return parse_html(fixture_html)


class TestParseHappyPath:
    def test_returns_records(self, fixture_records):
        assert len(fixture_records) > 0

    def test_exactly_six_records(self, fixture_records):
        assert len(fixture_records) == 6

    def test_report_date_is_correct(self, fixture_records):
        for rec in fixture_records:
            assert rec.report_date == "2025-01-14"

    def test_report_date_is_valid_iso(self, fixture_records):
        for rec in fixture_records:
            datetime.fromisoformat(rec.report_date)

    def test_wheat_srw_present(self, fixture_records):
        names = [r.commodity_name for r in fixture_records]
        assert any("WHEAT-SRW" in name for name in names)

    def test_crude_oil_present(self, fixture_records):
        names = [r.commodity_name for r in fixture_records]
        assert any("CRUDE OIL" in name for name in names)

    def test_wheat_srw_noncommercial_long(self, fixture_records):
        rec = next(r for r in fixture_records if "WHEAT-SRW" in r.commodity_name)
        assert rec.noncommercial_long == 99526

    def test_wheat_srw_noncommercial_short(self, fixture_records):
        rec = next(r for r in fixture_records if "WHEAT-SRW" in r.commodity_name)
        assert rec.noncommercial_short == 16459

    def test_wheat_srw_commercial_long(self, fixture_records):
        rec = next(r for r in fixture_records if "WHEAT-SRW" in r.commodity_name)
        assert rec.commercial_long == 234567

    def test_wheat_srw_commercial_short(self, fixture_records):
        rec = next(r for r in fixture_records if "WHEAT-SRW" in r.commodity_name)
        assert rec.commercial_short == 312456

    def test_wheat_srw_total_reportable_long(self, fixture_records):
        rec = next(r for r in fixture_records if "WHEAT-SRW" in r.commodity_name)
        assert rec.total_reportable_long == 339216

    def test_wheat_srw_total_reportable_short(self, fixture_records):
        rec = next(r for r in fixture_records if "WHEAT-SRW" in r.commodity_name)
        assert rec.total_reportable_short == 333038

    def test_wheat_srw_nonreportable_long(self, fixture_records):
        rec = next(r for r in fixture_records if "WHEAT-SRW" in r.commodity_name)
        assert rec.nonreportable_long == 16067

    def test_wheat_srw_nonreportable_short(self, fixture_records):
        rec = next(r for r in fixture_records if "WHEAT-SRW" in r.commodity_name)
        assert rec.nonreportable_short == 660

    def test_crude_oil_noncommercial_long(self, fixture_records):
        rec = next(r for r in fixture_records if "CRUDE OIL" in r.commodity_name)
        assert rec.noncommercial_long == 567890

    def test_crude_oil_commercial_short(self, fixture_records):
        rec = next(r for r in fixture_records if "CRUDE OIL" in r.commodity_name)
        assert rec.commercial_short == 678901

    def test_crude_oil_contract_code(self, fixture_records):
        rec = next(r for r in fixture_records if "CRUDE OIL" in r.commodity_name)
        assert rec.cftc_contract_market_code == "067651"

    def test_wheat_srw_contract_code(self, fixture_records):
        rec = next(r for r in fixture_records if "WHEAT-SRW" in r.commodity_name)
        assert rec.cftc_contract_market_code == "001602"

    def test_source_url_set_on_all_records(self, fixture_records):
        for rec in fixture_records:
            assert rec.source_url == SOURCE_URL

    def test_fetch_time_is_iso8601(self, fixture_records):
        for rec in fixture_records:
            datetime.fromisoformat(rec.fetch_time)

    def test_all_records_share_same_fetch_time(self, fixture_html):
        records = parse_html(fixture_html)
        times = {r.fetch_time for r in records}
        assert len(times) == 1

    def test_position_fields_are_integers(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec.noncommercial_long, int)
            assert isinstance(rec.noncommercial_short, int)
            assert isinstance(rec.commercial_long, int)
            assert isinstance(rec.commercial_short, int)
            assert isinstance(rec.total_reportable_long, int)
            assert isinstance(rec.total_reportable_short, int)
            assert isinstance(rec.nonreportable_long, int)
            assert isinstance(rec.nonreportable_short, int)

    def test_longs_are_positive(self, fixture_records):
        for rec in fixture_records:
            assert rec.noncommercial_long >= 0
            assert rec.commercial_long >= 0
            assert rec.total_reportable_long >= 0
            assert rec.nonreportable_long >= 0

    def test_record_is_cot_record(self, fixture_records):
        for rec in fixture_records:
            assert isinstance(rec, CotRecord)

    def test_all_six_commodities_named(self, fixture_records):
        expected_substrings = [
            "WHEAT-SRW",
            "WHEAT-HRW",
            "CORN",
            "CRUDE OIL",
            "GOLD",
            "SILVER",
        ]
        names = [r.commodity_name for r in fixture_records]
        for sub in expected_substrings:
            assert any(sub in name for name in names), f"Missing commodity: {sub}"


class TestExtractReportDate:
    def test_extracts_date_from_h2(self):
        html = "<html><body><h2>As of Tuesday, January 14, 2025</h2></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert _extract_report_date(soup) == "2025-01-14"

    def test_extracts_date_from_title(self):
        html = "<html><head><title>COT As of Monday, March 3, 2025</title></head></html>"
        soup = BeautifulSoup(html, "lxml")
        assert _extract_report_date(soup) == "2025-03-03"

    def test_returns_empty_string_when_no_date(self):
        html = "<html><body><h2>Commitments of Traders Report</h2></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert _extract_report_date(soup) == ""

    def test_case_insensitive_match(self):
        html = "<html><body><h2>as of Tuesday, February 11, 2025</h2></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert _extract_report_date(soup) == "2025-02-11"

    def test_fixture_date(self, fixture_html):
        soup = BeautifulSoup(fixture_html, "lxml")
        assert _extract_report_date(soup) == "2025-01-14"


class TestEdgeCases:
    def test_empty_html_returns_empty_list(self):
        assert parse_html("") == []

    def test_html_without_pre_tag_returns_empty_list(self):
        html = "<html><body><h2>As of Tuesday, January 14, 2025</h2><p>No data</p></body></html>"
        assert parse_html(html) == []

    def test_pre_with_no_commodity_lines_returns_empty_list(self):
        html = (
            "<html><body><h2>As of Tuesday, January 14, 2025</h2>"
            "<pre>Just some text\nwith no commodities</pre></body></html>"
        )
        assert parse_html(html) == []

    def test_commodity_header_without_all_line_skipped(self):
        html = (
            "<html><body><h2>As of Tuesday, January 14, 2025</h2><pre>"
            "WHEAT-SRW - CHICAGO BOARD OF TRADE                                        001602\n"
            "  This line does not start with ALL so it should be ignored\n"
            "CORN - CHICAGO BOARD OF TRADE                                             002602\n"
            "  ALL                                                                      456,789   234,567    45,678   789,012   901,234 1,291,479 1,181,479   89,012   15,678\n"
            "</pre></body></html>"
        )
        records = parse_html(html)
        assert len(records) == 1
        assert "CORN" in records[0].commodity_name

    def test_all_line_with_fewer_than_nine_numbers_skipped(self):
        html = (
            "<html><body><h2>As of Tuesday, January 14, 2025</h2><pre>"
            "WHEAT-SRW - CHICAGO BOARD OF TRADE                                        001602\n"
            "  ALL                                         100   200   300\n"
            "</pre></body></html>"
        )
        assert parse_html(html) == []

    def test_large_input_all_parsed(self):
        """50 identical commodity blocks all produce records."""
        block = (
            "CORN - CHICAGO BOARD OF TRADE                                             002602\n"
            "  ALL                                                                      456,789   234,567    45,678   789,012   901,234 1,291,479 1,181,479   89,012   15,678\n"
        )
        pre_content = "\n".join([block] * 50)
        html = (
            f"<html><body><h2>As of Tuesday, January 14, 2025</h2>"
            f"<pre>{pre_content}</pre></body></html>"
        )
        records = parse_html(html)
        assert len(records) == 50

    def test_commas_stripped_from_numbers(self, fixture_html):
        records = parse_html(fixture_html)
        corn = next(r for r in records if "CORN" in r.commodity_name)
        assert corn.noncommercial_long == 456789

    def test_no_date_yields_empty_report_date(self):
        html = (
            "<html><body><pre>"
            "GOLD - COMMODITY EXCHANGE INC.                                            088691\n"
            "  ALL                                                                      234,567    56,789    23,456   123,456   301,234   381,479   381,479   15,678    2,345\n"
            "</pre></body></html>"
        )
        records = parse_html(html)
        assert len(records) == 1
        assert records[0].report_date == ""


class TestDefaultRecord:
    def test_default_cot_record_fields(self):
        rec = CotRecord()
        assert rec.report_date == ""
        assert rec.commodity_name == ""
        assert rec.cftc_contract_market_code == ""
        assert rec.noncommercial_long == 0
        assert rec.noncommercial_short == 0
        assert rec.commercial_long == 0
        assert rec.commercial_short == 0
        assert rec.total_reportable_long == 0
        assert rec.total_reportable_short == 0
        assert rec.nonreportable_long == 0
        assert rec.nonreportable_short == 0
        assert rec.source_url == ""
        assert rec.fetch_time == ""


class TestScrapeFunction:
    def test_scrape_calls_fetch_with_source_url(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp) as mock_fetch:
            records = scrape()
        mock_fetch.assert_called_once_with(SOURCE_URL)
        assert len(records) == 6

    def test_scrape_returns_same_as_parse_html(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp):
            scraped = scrape()
        direct = parse_html(fixture_html)
        assert len(scraped) == len(direct)
        for s, d in zip(scraped, direct):
            assert s.commodity_name == d.commodity_name
            assert s.cftc_contract_market_code == d.cftc_contract_market_code
            assert s.noncommercial_long == d.noncommercial_long
            assert s.commercial_long == d.commercial_long

    def test_scrape_no_live_network(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp) as mock_fetch:
            scrape()
        assert mock_fetch.call_count == 1


MULTI_PRE_FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "cftc_cot_multi_pre.html"
)

CHANGE_LINES_FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "cftc_cot_change_lines.html"
)


@pytest.fixture
def multi_pre_html() -> str:
    with open(MULTI_PRE_FIXTURE_PATH, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture
def change_lines_html() -> str:
    with open(CHANGE_LINES_FIXTURE_PATH, encoding="utf-8") as fh:
        return fh.read()


class TestFindPreWithData:
    def test_returns_none_for_no_pre(self):
        soup = BeautifulSoup("<html><body><p>nothing</p></body></html>", "lxml")
        assert _find_pre_with_data(soup) is None

    def test_returns_none_when_pre_has_no_commodity_line(self):
        html = "<html><body><pre>Just a legend block with no commodity data.</pre></body></html>"
        soup = BeautifulSoup(html, "lxml")
        assert _find_pre_with_data(soup) is None

    def test_returns_first_pre_with_commodity_data(self):
        html = (
            "<html><body>"
            "<pre>Legend block: NON-COMMERCIAL LONG / SHORT / SPREADS</pre>"
            "<pre>CORN - CHICAGO BOARD OF TRADE                                             002602\n"
            "  ALL                                                                      456,789   234,567    45,678   789,012   901,234 1,291,479 1,181,479   89,012   15,678\n"
            "</pre></body></html>"
        )
        soup = BeautifulSoup(html, "lxml")
        pre = _find_pre_with_data(soup)
        assert pre is not None
        assert "CORN" in pre.get_text()

    def test_skips_legend_pre_and_finds_data_pre(self, multi_pre_html):
        soup = BeautifulSoup(multi_pre_html, "lxml")
        pre = _find_pre_with_data(soup)
        assert pre is not None
        assert "WHEAT-SRW" in pre.get_text()
        assert "CORN" in pre.get_text()


class TestMultiPreParsing:
    def test_multi_pre_parses_two_records(self, multi_pre_html):
        records = parse_html(multi_pre_html)
        assert len(records) == 2

    def test_multi_pre_wheat_srw_present(self, multi_pre_html):
        records = parse_html(multi_pre_html)
        names = [r.commodity_name for r in records]
        assert any("WHEAT-SRW" in n for n in names)

    def test_multi_pre_corn_present(self, multi_pre_html):
        records = parse_html(multi_pre_html)
        names = [r.commodity_name for r in records]
        assert any("CORN" in n for n in names)

    def test_multi_pre_report_date_extracted(self, multi_pre_html):
        records = parse_html(multi_pre_html)
        for rec in records:
            assert rec.report_date == "2025-01-14"

    def test_multi_pre_wheat_values(self, multi_pre_html):
        records = parse_html(multi_pre_html)
        wheat = next(r for r in records if "WHEAT-SRW" in r.commodity_name)
        assert wheat.noncommercial_long == 99526
        assert wheat.noncommercial_short == 16459
        assert wheat.commercial_long == 234567


class TestChangeLines:
    def test_change_lines_fixture_parses_two_records(self, change_lines_html):
        records = parse_html(change_lines_html)
        assert len(records) == 2

    def test_change_lines_ignored_wheat_values_correct(self, change_lines_html):
        records = parse_html(change_lines_html)
        wheat = next(r for r in records if "WHEAT-SRW" in r.commodity_name)
        assert wheat.noncommercial_long == 99526
        assert wheat.commercial_long == 234567

    def test_change_lines_ignored_corn_values_correct(self, change_lines_html):
        records = parse_html(change_lines_html)
        corn = next(r for r in records if "CORN" in r.commodity_name)
        assert corn.noncommercial_long == 456789

    def test_change_lines_do_not_produce_extra_records(self, change_lines_html):
        records = parse_html(change_lines_html)
        assert len(records) == 2


class TestCustomSourceUrl:
    def test_default_source_url_is_source_url_constant(self, fixture_html):
        records = parse_html(fixture_html)
        for rec in records:
            assert rec.source_url == SOURCE_URL

    def test_custom_source_url_embedded_in_records(self, fixture_html):
        custom = "https://www.cftc.gov/dea/futures/deacmesf2020.htm"
        records = parse_html(fixture_html, source_url=custom)
        for rec in records:
            assert rec.source_url == custom

    def test_custom_source_url_does_not_affect_field_values(self, fixture_html):
        custom = "https://www.cftc.gov/dea/futures/deacmesf2020.htm"
        records_default = parse_html(fixture_html)
        records_custom = parse_html(fixture_html, source_url=custom)
        assert len(records_default) == len(records_custom)
        for d, c in zip(records_default, records_custom):
            assert d.commodity_name == c.commodity_name
            assert d.noncommercial_long == c.noncommercial_long


class TestHistoricalUrlTemplate:
    def test_template_produces_expected_url(self):
        url = HISTORICAL_URL_TEMPLATE.format(year=2020)
        assert url == "https://www.cftc.gov/dea/futures/deacmesf2020.htm"

    def test_template_year_varies(self):
        url_2010 = HISTORICAL_URL_TEMPLATE.format(year=2010)
        url_2023 = HISTORICAL_URL_TEMPLATE.format(year=2023)
        assert "2010" in url_2010
        assert "2023" in url_2023
        assert url_2010 != url_2023

    def test_template_contains_base_domain(self):
        url = HISTORICAL_URL_TEMPLATE.format(year=2015)
        assert url.startswith("https://www.cftc.gov/")


class TestScrapeYear:
    def test_scrape_year_calls_fetch_with_year_url(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        expected_url = HISTORICAL_URL_TEMPLATE.format(year=2020)
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp) as mock_fetch:
            records = scrape_year(2020)
        mock_fetch.assert_called_once_with(expected_url)
        assert len(records) == 6

    def test_scrape_year_embeds_year_url_in_records(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        expected_url = HISTORICAL_URL_TEMPLATE.format(year=2018)
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp):
            records = scrape_year(2018)
        for rec in records:
            assert rec.source_url == expected_url

    def test_scrape_year_year_url_differs_from_source_url(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp):
            records = scrape_year(2019)
        for rec in records:
            assert rec.source_url != SOURCE_URL

    def test_scrape_year_no_live_network(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp) as mock_fetch:
            scrape_year(2021)
        assert mock_fetch.call_count == 1


class TestScrapeRange:
    def test_scrape_range_aggregates_all_years(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp):
            records = scrape_range(2020, 2022)
        assert len(records) == 6 * 3

    def test_scrape_range_calls_fetch_once_per_year(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp) as mock_fetch:
            scrape_range(2020, 2022)
        assert mock_fetch.call_count == 3

    def test_scrape_range_skips_failed_years(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html

        call_count = 0

        def mock_fetch(url):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise requests.HTTPError("404")
            return fake_resp

        with patch("src.scrapers.cftc_cot.fetch", side_effect=mock_fetch):
            records = scrape_range(2020, 2022)
        assert len(records) == 6 * 2

    def test_scrape_range_empty_when_start_exceeds_end(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp) as mock_fetch:
            records = scrape_range(2023, 2020)
        assert records == []
        mock_fetch.assert_not_called()

    def test_scrape_range_single_year_equals_scrape_year(self, fixture_html):
        fake_resp = MagicMock(spec=requests.Response)
        fake_resp.text = fixture_html
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp):
            range_records = scrape_range(2020, 2020)
        with patch("src.scrapers.cftc_cot.fetch", return_value=fake_resp):
            year_records = scrape_year(2020)
        assert len(range_records) == len(year_records)
        for r, y in zip(range_records, year_records):
            assert r.commodity_name == y.commodity_name
            assert r.noncommercial_long == y.noncommercial_long

    def test_scrape_range_all_years_skipped_returns_empty(self):
        with patch("src.scrapers.cftc_cot.fetch", side_effect=RuntimeError("down")):
            records = scrape_range(2020, 2022)
        assert records == []
