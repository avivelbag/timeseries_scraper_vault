"""Tests for src/drift_detector.py.

Uses monkeypatching to inject fixture content without making network requests.
All tests are deterministic: no real I/O, no time.time() flakiness.
"""

import json
import os
import sys
import types
from datetime import datetime


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.drift_detector import DriftReport, run_all_checks, run_drift_check


def _make_scraper(required_fields: list[str], records: list[dict]):
    """Return a minimal fake scraper namespace for testing."""
    m = types.SimpleNamespace()
    m.__name__ = "src.scrapers.fake"
    m.REQUIRED_FIELDS = required_fields
    m.run = lambda html: records
    return m


class TestDriftReport:
    def test_dataclass_fields_accessible(self):
        """DriftReport exposes all six required fields."""
        report = DriftReport(
            scraper_name="bls_cpi",
            url="https://example.com",
            status="ok",
            missing_fields=[],
            sample_record_count=10,
            checked_at="2026-05-15T00:00:00+00:00",
        )
        assert report.scraper_name == "bls_cpi"
        assert report.url == "https://example.com"
        assert report.status == "ok"
        assert report.missing_fields == []
        assert report.sample_record_count == 10
        assert report.checked_at == "2026-05-15T00:00:00+00:00"

    def test_broken_status_stored(self):
        report = DriftReport(
            scraper_name="x",
            url="u",
            status="broken",
            missing_fields=["f1", "f2"],
            sample_record_count=0,
            checked_at="2026-01-01T00:00:00+00:00",
        )
        assert report.status == "broken"
        assert report.missing_fields == ["f1", "f2"]


class TestRunDriftCheck:
    def test_all_fields_populated_status_ok(self, monkeypatch):
        """All required fields non-default in a record → status='ok'."""
        scraper = _make_scraper(
            ["field_str", "field_num"],
            [{"field_str": "hello", "field_num": 42.0}],
        )
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://example.com", None)

        assert report.status == "ok"
        assert report.missing_fields == []
        assert report.sample_record_count == 1

    def test_empty_string_field_marked_missing(self, monkeypatch):
        """A required str field with empty string value → appears in missing_fields."""
        scraper = _make_scraper(
            ["field_str", "field_num"],
            [{"field_str": "", "field_num": 42.0}],
        )
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://example.com", None)

        assert report.status == "broken"
        assert "field_str" in report.missing_fields
        assert "field_num" not in report.missing_fields

    def test_zero_float_field_marked_missing(self, monkeypatch):
        """A required float field with value 0.0 → appears in missing_fields."""
        scraper = _make_scraper(["price"], [{"price": 0.0}])
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://example.com", None)

        assert report.status == "broken"
        assert "price" in report.missing_fields

    def test_zero_int_field_marked_missing(self, monkeypatch):
        """A required int field with value 0 → appears in missing_fields."""
        scraper = _make_scraper(["year"], [{"year": 0}])
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://example.com", None)

        assert report.status == "broken"
        assert "year" in report.missing_fields

    def test_all_fields_default_broken_with_all_missing(self, monkeypatch):
        """All required fields default → status='broken', all in missing_fields."""
        scraper = _make_scraper(
            ["field_a", "field_b"],
            [{"field_a": "", "field_b": 0}],
        )
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://example.com", None)

        assert report.status == "broken"
        assert set(report.missing_fields) == {"field_a", "field_b"}

    def test_fetch_failure_broken_with_fetch_failed_sentinel(self, monkeypatch):
        """_fetch_content returning None → status='broken', missing=['fetch_failed']."""
        scraper = _make_scraper(["f"], [])
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: None)

        report = run_drift_check(scraper, "https://example.com", None)

        assert report.status == "broken"
        assert "fetch_failed" in report.missing_fields
        assert report.sample_record_count == 0

    def test_empty_records_list_broken_with_no_records_sentinel(self, monkeypatch):
        """run() returning [] → status='broken', missing=['no_records']."""
        scraper = _make_scraper(["f"], [])
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://example.com", None)

        assert report.status == "broken"
        assert "no_records" in report.missing_fields
        assert report.sample_record_count == 0

    def test_absent_required_key_marked_missing(self, monkeypatch):
        """A record that lacks a required key → that key appears in missing_fields."""
        scraper = _make_scraper(
            ["field_a", "field_b"],
            [{"field_a": "present"}],
        )
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://example.com", None)

        assert report.status == "broken"
        assert "field_b" in report.missing_fields
        assert "field_a" not in report.missing_fields

    def test_mutually_exclusive_fields_across_records_ok(self, monkeypatch):
        """Fields that alternate across records are each considered present.

        Mirrors usda_crop_progress where progress records have stage/pct_complete
        and condition records have condition_category/pct_condition — no single
        record carries all four, but the dataset as a whole does.
        """
        scraper = _make_scraper(
            ["stage", "pct_complete", "condition_category", "pct_condition"],
            [
                {"stage": "Planted", "pct_complete": 65.0, "condition_category": "", "pct_condition": 0.0},
                {"stage": "", "pct_complete": 0.0, "condition_category": "Good", "pct_condition": 40.0},
            ],
        )
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://example.com", None)

        assert report.status == "ok"
        assert report.missing_fields == []

    def test_scraper_name_extracted_from_module_dunder_name(self, monkeypatch):
        """scraper_name is the last dotted component of module.__name__."""
        m = types.SimpleNamespace()
        m.__name__ = "src.scrapers.bls_cpi"
        m.REQUIRED_FIELDS = []
        m.run = lambda html: [{}]
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(m, "https://example.com", None)

        assert report.scraper_name == "bls_cpi"

    def test_checked_at_is_valid_iso8601(self, monkeypatch):
        """checked_at field parses as a valid ISO-8601 datetime."""
        scraper = _make_scraper([], [{}])
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://example.com", None)

        datetime.fromisoformat(report.checked_at)

    def test_parse_exception_produces_broken(self, monkeypatch):
        """A run() that raises an exception → status='broken', missing=['parse_failed']."""
        m = types.SimpleNamespace()
        m.__name__ = "src.scrapers.broken"
        m.REQUIRED_FIELDS = ["f"]
        m.run = lambda html: (_ for _ in ()).throw(ValueError("site changed"))
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(m, "https://example.com", None)

        assert report.status == "broken"
        assert "parse_failed" in report.missing_fields

    def test_url_stored_in_report(self, monkeypatch):
        """The live_url argument is preserved in the returned DriftReport."""
        scraper = _make_scraper(["f"], [{"f": "v"}])
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://data.example.org/path", None)

        assert report.url == "https://data.example.org/path"

    def test_sample_record_count_matches_run_output(self, monkeypatch):
        """sample_record_count equals the length of the list returned by run()."""
        scraper = _make_scraper(
            ["f"],
            [{"f": "a"}, {"f": "b"}, {"f": "c"}],
        )
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://example.com", None)

        assert report.sample_record_count == 3

    def test_no_required_fields_always_ok(self, monkeypatch):
        """A scraper with no REQUIRED_FIELDS always produces status='ok'."""
        scraper = _make_scraper([], [{"anything": "value"}])
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")

        report = run_drift_check(scraper, "https://example.com", None)

        assert report.status == "ok"


class TestRunAllChecks:
    def test_returns_one_report_per_entry(self, monkeypatch, tmp_path):
        """len(reports) == len(registry)."""
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")
        monkeypatch.setattr("src.drift_detector.time.sleep", lambda s: None)

        registry = [
            {"scraper_module": _make_scraper(["f"], [{"f": "v"}]), "url": "u", "proto_class": None},
            {"scraper_module": _make_scraper(["f"], [{"f": "v"}]), "url": "u", "proto_class": None},
            {"scraper_module": _make_scraper(["f"], [{"f": "v"}]), "url": "u", "proto_class": None},
        ]

        reports = run_all_checks(registry, report_path=tmp_path / "r.json")

        assert len(reports) == 3

    def test_sleep_called_between_scrapers_not_before_first(self, monkeypatch, tmp_path):
        """Exactly N-1 sleeps for N scrapers; no sleep before the first entry."""
        sleep_calls: list[float] = []
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")
        monkeypatch.setattr("src.drift_detector.time.sleep", lambda s: sleep_calls.append(s))

        registry = [
            {"scraper_module": _make_scraper(["f"], [{"f": "v"}]), "url": "u", "proto_class": None},
            {"scraper_module": _make_scraper(["f"], [{"f": "v"}]), "url": "u", "proto_class": None},
            {"scraper_module": _make_scraper(["f"], [{"f": "v"}]), "url": "u", "proto_class": None},
        ]

        run_all_checks(registry, report_path=tmp_path / "r.json")

        assert len(sleep_calls) == 2
        assert all(s >= 3 for s in sleep_calls)

    def test_writes_valid_json_report(self, monkeypatch, tmp_path):
        """The JSON report file is written and parseable."""
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")
        monkeypatch.setattr("src.drift_detector.time.sleep", lambda s: None)

        scraper = _make_scraper(["field"], [{"field": "value"}])
        registry = [{"scraper_module": scraper, "url": "https://example.com", "proto_class": None}]
        report_path = tmp_path / "drift_report.json"

        run_all_checks(registry, report_path=report_path)

        assert report_path.exists()
        data = json.loads(report_path.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["status"] == "ok"
        assert data[0]["url"] == "https://example.com"

    def test_json_report_contains_all_required_keys(self, monkeypatch, tmp_path):
        """Each JSON entry has all six DriftReport field names."""
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")
        monkeypatch.setattr("src.drift_detector.time.sleep", lambda s: None)

        scraper = _make_scraper(["f"], [{"f": "v"}])
        registry = [{"scraper_module": scraper, "url": "u", "proto_class": None}]

        run_all_checks(registry, report_path=tmp_path / "r.json")

        data = json.loads((tmp_path / "r.json").read_text())
        expected_keys = {"scraper_name", "url", "status", "missing_fields", "sample_record_count", "checked_at"}
        assert expected_keys.issubset(data[0].keys())

    def test_returns_list_of_drift_report_instances(self, monkeypatch, tmp_path):
        """Return value contains DriftReport instances, not raw dicts."""
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")
        monkeypatch.setattr("src.drift_detector.time.sleep", lambda s: None)

        scraper = _make_scraper(["f"], [{"f": "v"}])
        registry = [{"scraper_module": scraper, "url": "u", "proto_class": None}]

        reports = run_all_checks(registry, report_path=tmp_path / "r.json")

        assert all(isinstance(r, DriftReport) for r in reports)

    def test_broken_scraper_included_without_aborting(self, monkeypatch, tmp_path):
        """A broken scraper does not abort run_all_checks; subsequent scrapers still run."""
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: None)
        monkeypatch.setattr("src.drift_detector.time.sleep", lambda s: None)

        registry = [
            {"scraper_module": _make_scraper(["f"], []), "url": "u1", "proto_class": None},
            {"scraper_module": _make_scraper(["f"], []), "url": "u2", "proto_class": None},
        ]

        reports = run_all_checks(registry, report_path=tmp_path / "r.json")

        assert len(reports) == 2
        assert all(r.status == "broken" for r in reports)

    def test_report_path_parent_created_if_missing(self, monkeypatch, tmp_path):
        """The output directory is created automatically if it does not exist."""
        monkeypatch.setattr("src.drift_detector._fetch_content", lambda url: "<html/>")
        monkeypatch.setattr("src.drift_detector.time.sleep", lambda s: None)

        scraper = _make_scraper([], [{}])
        registry = [{"scraper_module": scraper, "url": "u", "proto_class": None}]
        nested_path = tmp_path / "a" / "b" / "report.json"

        run_all_checks(registry, report_path=nested_path)

        assert nested_path.exists()

    def test_empty_registry_writes_empty_json_array(self, monkeypatch, tmp_path):
        """An empty registry produces an empty list in the JSON report."""
        monkeypatch.setattr("src.drift_detector.time.sleep", lambda s: None)
        report_path = tmp_path / "r.json"

        reports = run_all_checks([], report_path=report_path)

        assert reports == []
        assert json.loads(report_path.read_text()) == []


class TestDriftRegistry:
    def test_registry_covers_all_nine_scrapers(self):
        """REGISTRY contains exactly one entry per documented scraper."""
        from src.drift_registry import REGISTRY

        scraper_names = {entry["scraper_module"].__name__.split(".")[-1] for entry in REGISTRY}
        expected = {
            "bls_cpi",
            "eia_electricity",
            "eia_natural_gas",
            "eia_petroleum",
            "fao_food_price_index",
            "fed_h15_rates",
            "treasury_yield_curve",
            "usda_crop_progress",
            "usgs_streamflow",
        }
        assert scraper_names == expected

    def test_registry_entries_have_required_keys(self):
        """Each registry entry has scraper_module, url, and proto_class keys."""
        from src.drift_registry import REGISTRY

        for entry in REGISTRY:
            assert "scraper_module" in entry
            assert "url" in entry
            assert "proto_class" in entry

    def test_registry_urls_are_non_empty_strings(self):
        """All registry URLs are non-empty strings."""
        from src.drift_registry import REGISTRY

        for entry in REGISTRY:
            assert isinstance(entry["url"], str)
            assert entry["url"] != ""

    def test_registry_scraper_modules_have_run_and_required_fields(self):
        """Each registry scraper module exposes run() and REQUIRED_FIELDS."""
        from src.drift_registry import REGISTRY

        for entry in REGISTRY:
            mod = entry["scraper_module"]
            assert callable(mod.run)
            assert hasattr(mod, "REQUIRED_FIELDS")
            assert isinstance(mod.REQUIRED_FIELDS, list)
