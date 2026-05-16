import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.health_check import HealthCheck, _is_missing, check, check_all

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "eia_petroleum_sample.html")


def _mod(records, required_fields=None, name="mock_scraper"):
    attrs = {"__name__": name, "run": lambda html: records}
    if required_fields is not None:
        attrs["REQUIRED_FIELDS"] = required_fields
    return SimpleNamespace(**attrs)


def _record(**overrides):
    base = {
        "source_url": "https://www.eia.gov/test",
        "period_date": "2025-01-06",
        "product": "petroleum",
        "region": "U.S.",
        "price_usd_per_gallon": 3.12,
        "grade": "Regular",
        "units": "USD/gallon",
    }
    base.update(overrides)
    return base


class TestIsMissing:
    def test_none_is_missing(self):
        assert _is_missing(None) is True

    def test_empty_string_is_missing(self):
        assert _is_missing("") is True

    def test_zero_float_is_missing(self):
        assert _is_missing(0.0) is True

    def test_zero_int_is_missing(self):
        assert _is_missing(0) is True

    def test_non_empty_string_not_missing(self):
        assert _is_missing("hello") is False

    def test_positive_float_not_missing(self):
        assert _is_missing(3.14) is False

    def test_negative_float_not_missing(self):
        assert _is_missing(-1.5) is False

    def test_false_bool_not_flagged(self):
        # bool is a subclass of int; we must NOT treat False as missing
        assert _is_missing(False) is False

    def test_true_bool_not_flagged(self):
        assert _is_missing(True) is False


class TestCheckOkWithEIAFixture:
    @pytest.fixture
    def sample_html(self):
        with open(FIXTURE_PATH, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_ok_result_for_valid_eia_fixture(self, sample_html):
        from src.scrapers import eia_petroleum

        result = check(eia_petroleum, sample_html)
        assert result.status == "ok"

    def test_record_count_positive_for_eia_fixture(self, sample_html):
        from src.scrapers import eia_petroleum

        result = check(eia_petroleum, sample_html)
        assert result.record_count > 0

    def test_no_missing_fields_for_eia_fixture(self, sample_html):
        from src.scrapers import eia_petroleum

        result = check(eia_petroleum, sample_html)
        assert result.missing_fields == []

    def test_scraper_name_matches_module_name(self, sample_html):
        from src.scrapers import eia_petroleum

        result = check(eia_petroleum, sample_html)
        assert result.scraper_name == eia_petroleum.__name__


class TestCheckFailOnEmptyList:
    def test_fail_when_run_returns_empty_list(self):
        mod = _mod([], required_fields=["source_url", "region"])
        result = check(mod, "<html/>")
        assert result.status == "fail"

    def test_record_count_is_zero_on_fail(self):
        result = check(_mod([]), "<html/>")
        assert result.record_count == 0

    def test_missing_fields_empty_on_fail(self):
        # No records to inspect, so missing_fields must not be populated
        result = check(_mod([], required_fields=["source_url"]), "<html/>")
        assert result.missing_fields == []

    def test_fail_with_no_required_fields_declared(self):
        result = check(_mod([]), "")
        assert result.status == "fail"


class TestCheckWarnOnMissingField:
    def test_warn_when_string_field_is_empty(self):
        mod = _mod([_record(source_url="")], required_fields=["source_url"])
        result = check(mod, "<html/>")
        assert result.status == "warn"
        assert "source_url" in result.missing_fields

    def test_warn_when_field_is_none(self):
        mod = _mod([_record(region=None)], required_fields=["region"])
        result = check(mod, "<html/>")
        assert result.status == "warn"
        assert "region" in result.missing_fields

    def test_warn_when_numeric_field_is_zero(self):
        mod = _mod([_record(price_usd_per_gallon=0.0)], required_fields=["price_usd_per_gallon"])
        result = check(mod, "<html/>")
        assert result.status == "warn"
        assert "price_usd_per_gallon" in result.missing_fields

    def test_non_zero_price_is_ok(self):
        mod = _mod([_record(price_usd_per_gallon=3.5)], required_fields=["price_usd_per_gallon"])
        result = check(mod, "<html/>")
        assert result.status == "ok"
        assert result.missing_fields == []

    def test_only_actually_missing_fields_reported(self):
        mod = _mod(
            [_record(grade="", region=None)],
            required_fields=["grade", "region", "units"],
        )
        result = check(mod, "<html/>")
        assert result.status == "warn"
        assert set(result.missing_fields) == {"grade", "region"}

    def test_warn_when_any_record_has_missing_field(self):
        records = [_record(), _record(region="")]
        mod = _mod(records, required_fields=["region"])
        result = check(mod, "<html/>")
        assert result.status == "warn"
        assert "region" in result.missing_fields

    def test_record_count_reflects_all_records_on_warn(self):
        records = [_record(), _record(), _record(grade="")]
        mod = _mod(records, required_fields=["grade"])
        result = check(mod, "<html/>")
        assert result.record_count == 3

    def test_no_required_fields_declared_gives_ok(self):
        mod = _mod([_record()], required_fields=[])
        result = check(mod, "<html/>")
        assert result.status == "ok"
        assert result.missing_fields == []

    def test_module_without_required_fields_attr_gives_ok(self):
        # getattr fallback: missing REQUIRED_FIELDS attribute → treat as []
        mod = _mod([_record()])
        result = check(mod, "<html/>")
        assert result.status == "ok"

    def test_missing_fields_list_is_sorted(self):
        mod = _mod(
            [_record(units="", region=None, grade="")],
            required_fields=["units", "region", "grade"],
        )
        result = check(mod, "<html/>")
        assert result.missing_fields == sorted(result.missing_fields)


class TestCheckAll:
    def test_returns_one_result_per_scraper(self):
        scrapers = {
            "a": (_mod([_record()], required_fields=["region"]), "<html/>"),
            "b": (_mod([]), "<html/>"),
        }
        results = check_all(scrapers)
        assert len(results) == 2

    def test_uses_caller_supplied_names(self):
        scrapers = {"my_scraper": (_mod([_record()]), "<html/>")}
        results = check_all(scrapers)
        assert results[0].scraper_name == "my_scraper"

    def test_aggregates_ok_and_fail(self):
        scrapers = {
            "good": (_mod([_record()], required_fields=["region"]), "<html/>"),
            "bad": (_mod([]), "<html/>"),
        }
        results = check_all(scrapers)
        by_name = {r.scraper_name: r.status for r in results}
        assert by_name["good"] == "ok"
        assert by_name["bad"] == "fail"

    def test_aggregates_ok_warn_fail(self):
        scrapers = {
            "ok_scraper": (_mod([_record()], required_fields=["region"]), "<html/>"),
            "warn_scraper": (_mod([_record(region="")], required_fields=["region"]), "<html/>"),
            "fail_scraper": (_mod([]), "<html/>"),
        }
        results = check_all(scrapers)
        by_name = {r.scraper_name: r.status for r in results}
        assert by_name["ok_scraper"] == "ok"
        assert by_name["warn_scraper"] == "warn"
        assert by_name["fail_scraper"] == "fail"

    def test_empty_dict_returns_empty_list(self):
        assert check_all({}) == []

    def test_preserves_insertion_order(self):
        names = [f"s{i}" for i in range(5)]
        scrapers = {n: (_mod([_record()]), "<html/>") for n in names}
        results = check_all(scrapers)
        assert [r.scraper_name for r in results] == names


class TestMain:
    def test_main_exits_zero_on_all_ok(self, monkeypatch):
        def fake_check_all(scrapers):
            return [
                HealthCheck(
                    scraper_name="eia_petroleum",
                    record_count=5,
                    missing_fields=[],
                    status="ok",
                )
            ]

        monkeypatch.setattr("src.health_check.check_all", fake_check_all)

        import src.health_check as hc_mod
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                hc_mod.main()
            except SystemExit as exc:
                pytest.fail(f"main() called sys.exit({exc.code}) but expected no exit")

    def test_main_exits_one_on_fail(self, monkeypatch):
        import src.health_check as hc_mod

        def fake_check_all(scrapers):
            return [
                HealthCheck(
                    scraper_name="eia_petroleum",
                    record_count=0,
                    missing_fields=[],
                    status="fail",
                )
            ]

        monkeypatch.setattr("src.health_check.check_all", fake_check_all)

        with pytest.raises(SystemExit) as exc_info:
            hc_mod.main()

        assert exc_info.value.code == 1
