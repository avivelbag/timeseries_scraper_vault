"""Tests for src/scrapers/mba_mortgage_applications.py.

All tests use static HTML fixtures or inline HTML strings — zero live network
calls. The listing fixture has 3 release links spanning 2023-12 through
2024-01. The release fixture has 6 rows (Market Composite SA/NSA, Purchase
SA/NSA, Refinance SA/NSA) for the week ending 2024-01-12.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from protos.mba_mortgage_applications_pb2 import MortgageApplicationsRecord  # type: ignore[attr-defined]
from src.scrapers.mba_mortgage_applications import (
    SOURCE_URL,
    _extract_release_links,
    _find_column_indices,
    _find_data_table,
    _normalize_index_name,
    _parse_pct,
    _parse_week_ending_date,
    _record_to_proto,
    backfill,
    run,
    scrape,
)
from bs4 import BeautifulSoup

LISTING_FIXTURE = Path(__file__).parent / "fixtures" / "mba_was_listing_sample.html"
RELEASE_FIXTURE = Path(__file__).parent / "fixtures" / "mba_was_release_sample.html"


@pytest.fixture
def listing_html() -> str:
    return LISTING_FIXTURE.read_text()


@pytest.fixture
def release_html() -> str:
    return RELEASE_FIXTURE.read_text()


# ---------------------------------------------------------------------------
# _parse_pct unit tests
# ---------------------------------------------------------------------------


class TestParsePct:
    def test_negative_percentage(self):
        assert _parse_pct("-2.3%") == pytest.approx(-2.3)

    def test_positive_percentage(self):
        assert _parse_pct("5.1%") == pytest.approx(5.1)

    def test_na_returns_none(self):
        assert _parse_pct("n.a.") is None

    def test_double_dash_returns_none(self):
        assert _parse_pct("--") is None

    def test_empty_string_returns_none(self):
        assert _parse_pct("") is None

    def test_n_slash_a_returns_none(self):
        assert _parse_pct("N/A") is None

    def test_na_lowercase_returns_none(self):
        assert _parse_pct("na") is None

    def test_percentage_without_sign(self):
        assert _parse_pct("10.7%") == pytest.approx(10.7)

    def test_whitespace_stripped(self):
        assert _parse_pct("  -4.9%  ") == pytest.approx(-4.9)

    def test_integer_percentage(self):
        assert _parse_pct("3%") == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# _normalize_index_name unit tests
# ---------------------------------------------------------------------------


class TestNormalizeIndexName:
    def test_market_composite_sa(self):
        name, sa = _normalize_index_name("Market Composite Index (SA):")
        assert name == "Market_Composite"
        assert sa is True

    def test_market_composite_nsa(self):
        name, sa = _normalize_index_name("Market Composite Index (NSA):")
        assert name == "Market_Composite"
        assert sa is False

    def test_purchase_sa(self):
        name, sa = _normalize_index_name("Purchase Index (SA):")
        assert name == "Purchase"
        assert sa is True

    def test_purchase_nsa(self):
        name, sa = _normalize_index_name("Purchase Index (NSA):")
        assert name == "Purchase"
        assert sa is False

    def test_refinance_sa(self):
        name, sa = _normalize_index_name("Refinance Index (SA):")
        assert name == "Refinance"
        assert sa is True

    def test_refinance_nsa(self):
        name, sa = _normalize_index_name("Refinance Index (NSA):")
        assert name == "Refinance"
        assert sa is False

    def test_no_sa_marker_defaults_false(self):
        _, sa = _normalize_index_name("Market Composite Index:")
        assert sa is False

    def test_unknown_name_underscored(self):
        name, _ = _normalize_index_name("Some Unknown Index (SA):")
        assert "_" in name or name == "Some_Unknown"

    def test_trailing_colon_stripped(self):
        name, _ = _normalize_index_name("Purchase Index (SA):")
        assert not name.endswith(":")


# ---------------------------------------------------------------------------
# _parse_week_ending_date unit tests
# ---------------------------------------------------------------------------


class TestParseWeekEndingDate:
    def test_date_from_h1(self):
        html = "<html><body><h1>Weekly Survey for the Week Ending January 12, 2024</h1></body></html>"
        assert _parse_week_ending_date(html) == "2024-01-12"

    def test_date_from_title(self):
        html = "<html><head><title>MBA Survey Week Ending March 15, 2024</title></head><body></body></html>"
        assert _parse_week_ending_date(html) == "2024-03-15"

    def test_date_from_h2(self):
        html = "<html><body><h2>For the Week Ending December 22, 2023</h2></body></html>"
        assert _parse_week_ending_date(html) == "2023-12-22"

    def test_missing_date_returns_none(self):
        html = "<html><body><h1>No date here</h1></body></html>"
        assert _parse_week_ending_date(html) is None

    def test_single_digit_day(self):
        html = "<html><body><h1>Survey for the Week Ending January 5, 2024</h1></body></html>"
        assert _parse_week_ending_date(html) == "2024-01-05"

    def test_december_date(self):
        html = "<html><body><h1>Survey for the Week Ending December 22, 2023</h1></body></html>"
        assert _parse_week_ending_date(html) == "2023-12-22"


# ---------------------------------------------------------------------------
# _extract_release_links unit tests
# ---------------------------------------------------------------------------


class TestExtractReleaseLinks:
    def test_returns_three_links(self, listing_html):
        links = _extract_release_links(listing_html)
        assert len(links) == 3

    def test_sorted_newest_first(self, listing_html):
        links = _extract_release_links(listing_html)
        dates = [d for d, _ in links]
        assert dates == sorted(dates, reverse=True)

    def test_most_recent_first(self, listing_html):
        links = _extract_release_links(listing_html)
        assert links[0][0] == "2024-01-12"

    def test_second_link_date(self, listing_html):
        links = _extract_release_links(listing_html)
        assert links[1][0] == "2024-01-05"

    def test_oldest_link_date(self, listing_html):
        links = _extract_release_links(listing_html)
        assert links[2][0] == "2023-12-22"

    def test_absolute_urls(self, listing_html):
        links = _extract_release_links(listing_html)
        for _, url in links:
            assert url.startswith("https://www.mba.org")

    def test_week_ending_dates_from_link_text(self, listing_html):
        links = _extract_release_links(listing_html)
        dates = {d for d, _ in links}
        assert {"2024-01-12", "2024-01-05", "2023-12-22"} == dates

    def test_empty_page_returns_empty_list(self):
        links = _extract_release_links("<html><body><p>No links</p></body></html>")
        assert links == []

    def test_page_without_matching_hrefs_returns_empty(self):
        html = '<html><body><a href="/about">About</a></body></html>'
        links = _extract_release_links(html)
        assert links == []

    def test_duplicate_urls_deduplicated(self):
        html = """<html><body>
        <a href="/weekly-applications-survey/2024/01/17/foo">Weekly Mortgage Applications Survey for the Week Ending January 12, 2024</a>
        <a href="/weekly-applications-survey/2024/01/17/foo">Weekly Mortgage Applications Survey for the Week Ending January 12, 2024</a>
        </body></html>"""
        links = _extract_release_links(html)
        assert len(links) == 1

    def test_custom_base_url(self, listing_html):
        links = _extract_release_links(listing_html, base_url="https://custom.example.com")
        for _, url in links:
            assert url.startswith("https://custom.example.com")


# ---------------------------------------------------------------------------
# run() happy-path tests (release fixture)
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    def test_returns_six_records(self, release_html):
        records = run(release_html)
        assert len(records) == 6

    def test_week_ending_date_parsed(self, release_html):
        records = run(release_html)
        for r in records:
            assert r["week_ending_date"] == "2024-01-12"

    def test_all_index_names_present(self, release_html):
        records = run(release_html)
        names = {r["index_name"] for r in records}
        assert "Market_Composite" in names
        assert "Purchase" in names
        assert "Refinance" in names

    def test_market_composite_sa_flag_true(self, release_html):
        records = run(release_html)
        mc_sa = next(
            r for r in records
            if r["index_name"] == "Market_Composite" and r["seasonally_adjusted"]
        )
        assert mc_sa["seasonally_adjusted"] is True

    def test_market_composite_nsa_flag_false(self, release_html):
        records = run(release_html)
        mc_nsa = next(
            r for r in records
            if r["index_name"] == "Market_Composite" and not r["seasonally_adjusted"]
        )
        assert mc_nsa["seasonally_adjusted"] is False

    def test_purchase_nsa_flag_false(self, release_html):
        records = run(release_html)
        pur_nsa = next(
            r for r in records
            if r["index_name"] == "Purchase" and not r["seasonally_adjusted"]
        )
        assert pur_nsa["seasonally_adjusted"] is False

    def test_market_composite_sa_index_value(self, release_html):
        records = run(release_html)
        mc_sa = next(
            r for r in records
            if r["index_name"] == "Market_Composite" and r["seasonally_adjusted"]
        )
        assert mc_sa["index_value"] == pytest.approx(221.3)

    def test_purchase_sa_index_value(self, release_html):
        records = run(release_html)
        pur_sa = next(
            r for r in records if r["index_name"] == "Purchase" and r["seasonally_adjusted"]
        )
        assert pur_sa["index_value"] == pytest.approx(147.2)

    def test_refinance_sa_index_value(self, release_html):
        records = run(release_html)
        ref_sa = next(
            r for r in records if r["index_name"] == "Refinance" and r["seasonally_adjusted"]
        )
        assert ref_sa["index_value"] == pytest.approx(612.4)

    def test_pct_week_parsed_for_market_composite_sa(self, release_html):
        records = run(release_html)
        mc_sa = next(
            r for r in records
            if r["index_name"] == "Market_Composite" and r["seasonally_adjusted"]
        )
        assert mc_sa["change_pct_week"] == pytest.approx(-2.3)

    def test_pct_year_parsed_for_market_composite_sa(self, release_html):
        records = run(release_html)
        mc_sa = next(
            r for r in records
            if r["index_name"] == "Market_Composite" and r["seasonally_adjusted"]
        )
        assert mc_sa["change_pct_year"] == pytest.approx(5.1)

    def test_pct_year_null_for_market_composite_nsa(self, release_html):
        """n.a. in year-ago column maps to None."""
        records = run(release_html)
        mc_nsa = next(
            r for r in records
            if r["index_name"] == "Market_Composite" and not r["seasonally_adjusted"]
        )
        assert mc_nsa["change_pct_year"] is None

    def test_pct_week_null_for_purchase_nsa(self, release_html):
        """-- in week-change column maps to None."""
        records = run(release_html)
        pur_nsa = next(
            r for r in records if r["index_name"] == "Purchase" and not r["seasonally_adjusted"]
        )
        assert pur_nsa["change_pct_week"] is None

    def test_pct_year_null_for_refinance_nsa(self, release_html):
        records = run(release_html)
        ref_nsa = next(
            r for r in records if r["index_name"] == "Refinance" and not r["seasonally_adjusted"]
        )
        assert ref_nsa["change_pct_year"] is None

    def test_source_url_default(self, release_html):
        records = run(release_html)
        for r in records:
            assert r["source_url"] == SOURCE_URL

    def test_source_url_override(self, release_html):
        url = "https://example.com/release"
        records = run(release_html, source_url=url)
        for r in records:
            assert r["source_url"] == url

    def test_fetch_time_is_iso(self, release_html):
        records = run(release_html)
        for r in records:
            assert "T" in r["fetch_time"]

    def test_six_sa_nsa_pairs(self, release_html):
        """Each index family appears as exactly one SA and one NSA record."""
        records = run(release_html)
        for name in ("Market_Composite", "Purchase", "Refinance"):
            sa_count = sum(1 for r in records if r["index_name"] == name and r["seasonally_adjusted"])
            nsa_count = sum(1 for r in records if r["index_name"] == name and not r["seasonally_adjusted"])
            assert sa_count == 1, f"{name} SA count wrong"
            assert nsa_count == 1, f"{name} NSA count wrong"


# ---------------------------------------------------------------------------
# run() edge-case tests (inline HTML)
# ---------------------------------------------------------------------------


class TestRunEdgeCases:
    def _minimal_table(self, rows: list[str], headers: str | None = None) -> str:
        if headers is None:
            headers = (
                "<tr>"
                "<th>Survey Component</th>"
                "<th>Index</th>"
                "<th>Pct. Change from Previous Week</th>"
                "<th>Pct. Change from One Year Ago</th>"
                "</tr>"
            )
        body = "".join(rows)
        return (
            "<html><body>"
            "<h1>Survey for the Week Ending February 2, 2024</h1>"
            "<table>"
            f"<thead>{headers}</thead>"
            f"<tbody>{body}</tbody>"
            "</table></body></html>"
        )

    def test_large_table_parses_all_rows(self):
        """Table with 20 SA rows all produce valid records."""
        row_template = "<tr><td>Market Composite Index (SA):</td><td>{v}</td><td>1.0%</td><td>2.0%</td></tr>"
        rows = [row_template.format(v=200.0 + i) for i in range(20)]
        html = self._minimal_table(rows)
        records = run(html)
        assert len(records) == 20

    def test_row_with_all_null_pct_still_emits_record(self):
        """A row where both pct columns are 'n.a.' still yields one record."""
        html = self._minimal_table(
            ["<tr><td>Refinance Index (SA):</td><td>612.4</td><td>n.a.</td><td>n.a.</td></tr>"]
        )
        records = run(html)
        assert len(records) == 1
        assert records[0]["change_pct_week"] is None
        assert records[0]["change_pct_year"] is None

    def test_row_missing_pct_columns_still_emits_record(self):
        """A row shorter than the column count yields a record with None pct fields."""
        html = (
            "<html><body>"
            "<h1>Survey for the Week Ending February 2, 2024</h1>"
            "<table>"
            "<tr><th>Survey Component</th><th>Index</th></tr>"
            "<tr><td>Purchase Index (SA):</td><td>147.2</td></tr>"
            "</table></body></html>"
        )
        records = run(html)
        assert len(records) == 1
        assert records[0]["change_pct_week"] is None
        assert records[0]["change_pct_year"] is None

    def test_missing_week_date_stored_as_empty(self):
        """When no 'Week Ending' text is found, week_ending_date is stored as ''."""
        html = self._minimal_table(
            ["<tr><td>Market Composite Index (SA):</td><td>221.3</td><td>-2.3%</td><td>5.1%</td></tr>"],
            headers=(
                "<tr>"
                "<th>Survey Component</th><th>Index</th>"
                "<th>Pct. Change from Previous Week</th>"
                "<th>Pct. Change from One Year Ago</th>"
                "</tr>"
            ),
        ).replace("Week Ending February 2, 2024", "No date here")
        html = (
            "<html><body>"
            "<h1>No date here</h1>"
            "<table>"
            "<tr><th>Survey Component</th><th>Index</th>"
            "<th>Pct. Change from Previous Week</th>"
            "<th>Pct. Change from One Year Ago</th></tr>"
            "<tr><td>Market Composite Index (SA):</td><td>221.3</td><td>-2.3%</td><td>5.1%</td></tr>"
            "</table></body></html>"
        )
        records = run(html)
        assert records[0]["week_ending_date"] == ""

    def test_comma_in_value_stripped(self):
        """Values like '1,221.3' are parsed after stripping commas."""
        html = self._minimal_table(
            ["<tr><td>Market Composite Index (SA):</td><td>1,221.3</td><td>-2.3%</td><td>5.1%</td></tr>"]
        )
        records = run(html)
        assert records[0]["index_value"] == pytest.approx(1221.3)

    def test_header_row_skipped(self):
        """Header row with 'Survey Component' label is not emitted as a record."""
        html = self._minimal_table(
            ["<tr><td>Market Composite Index (SA):</td><td>221.3</td><td>-2.3%</td><td>5.1%</td></tr>"]
        )
        records = run(html)
        assert all(r["index_name"] != "Survey_Component" for r in records)


# ---------------------------------------------------------------------------
# run() failure-mode tests
# ---------------------------------------------------------------------------


class TestRunFailureModes:
    def test_empty_html_raises(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("")

    def test_whitespace_only_html_raises(self):
        with pytest.raises(ValueError, match="Empty HTML"):
            run("   \n  ")

    def test_no_table_raises(self):
        with pytest.raises(ValueError, match="No mortgage applications table"):
            run("<html><body><p>No table here</p></body></html>")

    def test_table_without_index_keywords_raises(self):
        """A table without 'composite', 'purchase', or 'refinance' is not recognised."""
        html = """<html><body>
        <table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
        </body></html>"""
        with pytest.raises(ValueError, match="No mortgage applications table"):
            run(html)

    def test_header_only_table_returns_empty_list(self):
        """A table with only headers and no parseable data rows yields [].

        The table title row includes "Market Composite Index" so _find_data_table
        can locate it; the data section is intentionally empty.
        """
        html = """<html><body>
        <table>
          <tr><th colspan="4">Market Composite Index Survey</th></tr>
          <tr><th>Survey Component</th><th>Index</th>
              <th>Pct. Change from Previous Week</th>
              <th>Pct. Change from One Year Ago</th></tr>
        </table>
        </body></html>"""
        records = run(html)
        assert records == []

    def test_all_value_cells_non_numeric_returns_empty(self):
        """Rows whose value cell is non-numeric are all skipped, yielding []."""
        html = """<html><body>
        <table>
          <tr><th>Survey Component</th><th>Index</th>
              <th>Pct. Change from Previous Week</th>
              <th>Pct. Change from One Year Ago</th></tr>
          <tr><td>Market Composite Index (SA):</td><td>n.a.</td><td>-2.3%</td><td>5.1%</td></tr>
        </table>
        </body></html>"""
        records = run(html)
        assert records == []


# ---------------------------------------------------------------------------
# _record_to_proto tests
# ---------------------------------------------------------------------------


class TestRecordToProto:
    def test_proto_type(self, release_html):
        records = run(release_html)
        msg = _record_to_proto(records[0])
        assert isinstance(msg, MortgageApplicationsRecord)

    def test_proto_week_ending_date(self, release_html):
        records = run(release_html)
        msg = _record_to_proto(records[0])
        assert msg.week_ending_date == records[0]["week_ending_date"]

    def test_proto_index_name(self, release_html):
        records = run(release_html)
        msg = _record_to_proto(records[0])
        assert msg.index_name == records[0]["index_name"]

    def test_proto_index_value(self, release_html):
        records = run(release_html)
        msg = _record_to_proto(records[0])
        assert msg.index_value == pytest.approx(records[0]["index_value"])

    def test_proto_nullable_pct_week(self, release_html):
        records = run(release_html)
        mc_nsa = next(
            r for r in records
            if r["index_name"] == "Market_Composite" and not r["seasonally_adjusted"]
        )
        msg = _record_to_proto(mc_nsa)
        assert msg.change_pct_week == pytest.approx(-10.7)

    def test_proto_nullable_pct_year_is_none(self, release_html):
        records = run(release_html)
        mc_nsa = next(
            r for r in records
            if r["index_name"] == "Market_Composite" and not r["seasonally_adjusted"]
        )
        msg = _record_to_proto(mc_nsa)
        assert msg.change_pct_year is None

    def test_proto_seasonally_adjusted(self, release_html):
        records = run(release_html)
        mc_sa = next(
            r for r in records
            if r["index_name"] == "Market_Composite" and r["seasonally_adjusted"]
        )
        msg = _record_to_proto(mc_sa)
        assert msg.seasonally_adjusted is True

    def test_proto_source_url(self, release_html):
        records = run(release_html)
        msg = _record_to_proto(records[0])
        assert msg.source_url != ""

    def test_proto_fetch_time_is_iso(self, release_html):
        records = run(release_html)
        msg = _record_to_proto(records[0])
        assert "T" in msg.fetch_time
        assert msg.fetch_time != ""


# ---------------------------------------------------------------------------
# scrape() integration tests (no live network)
# ---------------------------------------------------------------------------


class TestScrapeFunction:
    def _make_fake_responses(self, listing_html, release_html):
        listing_resp = MagicMock(spec=requests.Response)
        listing_resp.text = listing_html
        release_resp = MagicMock(spec=requests.Response)
        release_resp.text = release_html
        return listing_resp, release_resp

    def test_scrape_fetches_listing_url_first(self, listing_html, release_html):
        listing_resp, release_resp = self._make_fake_responses(listing_html, release_html)
        with patch(
            "src.scrapers.mba_mortgage_applications.fetch",
            side_effect=[listing_resp, release_resp],
        ) as mock_fetch, patch("src.scrapers.mba_mortgage_applications.time.sleep"):
            scrape()
        assert mock_fetch.call_args_list[0][0][0] == SOURCE_URL

    def test_scrape_fetches_most_recent_release(self, listing_html, release_html):
        listing_resp, release_resp = self._make_fake_responses(listing_html, release_html)
        with patch(
            "src.scrapers.mba_mortgage_applications.fetch",
            side_effect=[listing_resp, release_resp],
        ) as mock_fetch, patch("src.scrapers.mba_mortgage_applications.time.sleep"):
            scrape()
        second_call_url = mock_fetch.call_args_list[1][0][0]
        assert "2024/01/17" in second_call_url

    def test_scrape_returns_six_records(self, listing_html, release_html):
        listing_resp, release_resp = self._make_fake_responses(listing_html, release_html)
        with patch(
            "src.scrapers.mba_mortgage_applications.fetch",
            side_effect=[listing_resp, release_resp],
        ), patch("src.scrapers.mba_mortgage_applications.time.sleep"):
            records = scrape()
        assert len(records) == 6

    def test_scrape_sleeps_at_least_3s_after_each_fetch(self, listing_html, release_html):
        listing_resp, release_resp = self._make_fake_responses(listing_html, release_html)
        sleep_calls: list[float] = []
        with patch(
            "src.scrapers.mba_mortgage_applications.fetch",
            side_effect=[listing_resp, release_resp],
        ), patch(
            "src.scrapers.mba_mortgage_applications.time.sleep",
            side_effect=lambda s: sleep_calls.append(s),
        ):
            scrape()
        assert len(sleep_calls) == 2
        assert all(s >= 3 for s in sleep_calls), f"Sleep < 3s found: {sleep_calls}"

    def test_scrape_raises_when_no_links(self):
        empty_resp = MagicMock(spec=requests.Response)
        empty_resp.text = "<html><body><p>No links</p></body></html>"
        with patch(
            "src.scrapers.mba_mortgage_applications.fetch",
            return_value=empty_resp,
        ), patch("src.scrapers.mba_mortgage_applications.time.sleep"):
            with pytest.raises(ValueError, match="No release links"):
                scrape()

    def test_scrape_propagates_parse_error(self, listing_html):
        listing_resp = MagicMock(spec=requests.Response)
        listing_resp.text = listing_html
        bad_resp = MagicMock(spec=requests.Response)
        bad_resp.text = "<html><body><p>No table</p></body></html>"
        with patch(
            "src.scrapers.mba_mortgage_applications.fetch",
            side_effect=[listing_resp, bad_resp],
        ), patch("src.scrapers.mba_mortgage_applications.time.sleep"):
            with pytest.raises(ValueError, match="No mortgage applications table"):
                scrape()


# ---------------------------------------------------------------------------
# backfill() tests
# ---------------------------------------------------------------------------


class TestBackfillFunction:
    """Tests for backfill(start_date, end_date).

    The listing fixture has 3 release links for dates 2023-12-22, 2024-01-05,
    and 2024-01-12. The release fixture is reused for all releases in these
    tests.
    """

    @pytest.fixture
    def _patch_fetch(self, listing_html, release_html):
        listing_resp = MagicMock(spec=requests.Response)
        listing_resp.text = listing_html
        release_resp = MagicMock(spec=requests.Response)
        release_resp.text = release_html
        with patch(
            "src.scrapers.mba_mortgage_applications.fetch",
            side_effect=[listing_resp] + [release_resp] * 10,
        ), patch("src.scrapers.mba_mortgage_applications.time.sleep"):
            yield

    def test_full_range_returns_all_releases(self, _patch_fetch):
        """Date range spanning all 3 fixture releases yields 18 records (6 per release)."""
        records = backfill("2023-12-01", "2024-01-31")
        assert len(records) == 18

    def test_single_week_filter(self, _patch_fetch):
        """Date range covering only the most recent week yields 6 records."""
        records = backfill("2024-01-12", "2024-01-12")
        assert len(records) == 6

    def test_two_week_filter(self, _patch_fetch):
        records = backfill("2024-01-05", "2024-01-12")
        assert len(records) == 12

    def test_boundary_dates_inclusive(self, _patch_fetch):
        records = backfill("2023-12-22", "2024-01-12")
        assert len(records) == 18

    def test_start_after_end_raises(self, _patch_fetch):
        with pytest.raises(ValueError, match="must be <="):
            backfill("2024-01-12", "2024-01-05")

    def test_no_matching_dates_raises(self, _patch_fetch):
        with pytest.raises(ValueError, match="No MBA WAS releases found"):
            backfill("2020-01-01", "2020-12-31")

    def test_sleeps_between_pages(self, listing_html, release_html):
        listing_resp = MagicMock(spec=requests.Response)
        listing_resp.text = listing_html
        release_resp = MagicMock(spec=requests.Response)
        release_resp.text = release_html
        sleep_calls: list[float] = []
        with patch(
            "src.scrapers.mba_mortgage_applications.fetch",
            side_effect=[listing_resp, release_resp],
        ), patch(
            "src.scrapers.mba_mortgage_applications.time.sleep",
            side_effect=lambda s: sleep_calls.append(s),
        ):
            backfill("2024-01-12", "2024-01-12")
        assert all(s >= 3 for s in sleep_calls), f"Sleep < 3s found: {sleep_calls}"

    def test_returned_records_have_required_fields(self, _patch_fetch):
        required = (
            "week_ending_date",
            "index_name",
            "index_value",
            "seasonally_adjusted",
            "source_url",
            "fetch_time",
        )
        for r in backfill("2024-01-12", "2024-01-12"):
            for key in required:
                assert key in r
                assert r[key] is not None or key in ("change_pct_week", "change_pct_year")

    def test_equal_start_end_outside_data_raises(self, _patch_fetch):
        with pytest.raises(ValueError, match="No MBA WAS releases found"):
            backfill("2024-02-01", "2024-02-01")


# ---------------------------------------------------------------------------
# _find_data_table tests
# ---------------------------------------------------------------------------


class TestFindDataTable:
    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    def test_finds_table_with_composite_and_index(self):
        html = (
            "<table><tr><td>Market Composite Index (SA):</td>"
            "<td>221.3</td></tr></table>"
        )
        soup = self._soup(html)
        assert _find_data_table(soup) is not None

    def test_finds_table_with_purchase(self):
        html = (
            "<table><tr><td>Purchase Index (SA):</td>"
            "<td>147.2</td></tr></table>"
        )
        soup = self._soup(html)
        assert _find_data_table(soup) is not None

    def test_returns_none_for_unrelated_table(self):
        html = "<table><tr><td>Navigation</td><td>Home</td></tr></table>"
        soup = self._soup(html)
        assert _find_data_table(soup) is None

    def test_returns_none_for_no_tables(self):
        soup = self._soup("<html><body><p>Text only</p></body></html>")
        assert _find_data_table(soup) is None


# ---------------------------------------------------------------------------
# _find_column_indices tests
# ---------------------------------------------------------------------------


class TestFindColumnIndices:
    def _make_table(self, header_row: str) -> BeautifulSoup:
        html = f"<table><tr>{header_row}</tr></table>"
        soup = BeautifulSoup(html, "lxml")
        return soup.find("table")

    def test_standard_headers(self):
        table = self._make_table(
            "<th>Survey Component</th>"
            "<th>Index</th>"
            "<th>Pct. Change from Previous Week</th>"
            "<th>Pct. Change from One Year Ago</th>"
        )
        val, pct_w, pct_y = _find_column_indices(table)
        assert val == 1
        assert pct_w == 2
        assert pct_y == 3

    def test_this_week_header(self):
        table = self._make_table(
            "<th>Component</th><th>This Week</th><th>Last Week</th><th>1 Year Ago</th>"
        )
        val, pct_w, pct_y = _find_column_indices(table)
        assert val == 1

    def test_fallback_defaults_when_no_header_matches(self):
        table = self._make_table("<td>a</td><td>b</td><td>c</td>")
        val, pct_w, pct_y = _find_column_indices(table)
        assert (val, pct_w, pct_y) == (1, 2, 3)
