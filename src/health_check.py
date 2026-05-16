"""Scraper health-check module — validates output before upload.

Provides check() and check_all() to validate scraper output against
required-field completeness thresholds.  Designed to run entirely offline
(no network, no BigQuery) as a fast pre-upload gate.
"""

import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Literal


@dataclass
class HealthCheck:
    """Result of validating a single scraper's output against its schema.

    Attributes:
        scraper_name: Identifying name of the scraper that was checked.
        record_count: Number of records returned by the scraper's run().
        missing_fields: Sorted list of REQUIRED_FIELDS that were absent,
            empty-string, or zero in at least one record.
        status: "ok" — all records complete; "warn" — records exist but
            some required fields are missing/default; "fail" — no records.
    """

    scraper_name: str
    record_count: int
    missing_fields: list[str]
    status: Literal["ok", "warn", "fail"]


def _is_missing(value: object) -> bool:
    """Return True when value matches a proto-default sentinel.

    Uses type-aware checks rather than plain truthiness so that a
    legitimately False bool is not flagged as missing.  The three
    sentinels are None (field absent), "" (empty string field), and
    0.0 / 0 (numeric proto default).

    Args:
        value: Field value extracted from a scraper output record.

    Returns:
        True if the value should be considered absent or default.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0.0
    return False


def check(scraper_module: ModuleType, html: str) -> HealthCheck:
    """Run a scraper against HTML and return a HealthCheck for its output.

    Calls scraper_module.run(html), then validates:
      1. record_count > 0 (otherwise status is "fail").
      2. Every field in scraper_module.REQUIRED_FIELDS (defaults to []) is
         present and non-default in every record (otherwise status is "warn").

    Args:
        scraper_module: A module with run(html: str) -> list[dict] and an
            optional REQUIRED_FIELDS: list[str] constant.
        html: Raw HTML string to pass to run().

    Returns:
        HealthCheck describing the validation outcome.
    """
    scraper_name: str = getattr(scraper_module, "__name__", str(scraper_module))
    records: list[dict] = scraper_module.run(html)
    record_count = len(records)

    if record_count == 0:
        return HealthCheck(
            scraper_name=scraper_name,
            record_count=0,
            missing_fields=[],
            status="fail",
        )

    required_fields: list[str] = getattr(scraper_module, "REQUIRED_FIELDS", [])
    missing: set[str] = set()

    for record in records:
        for field_name in required_fields:
            if _is_missing(record.get(field_name)):
                missing.add(field_name)

    status: Literal["ok", "warn", "fail"] = "warn" if missing else "ok"
    return HealthCheck(
        scraper_name=scraper_name,
        record_count=record_count,
        missing_fields=sorted(missing),
        status=status,
    )


def check_all(scrapers: dict[str, tuple[ModuleType, str]]) -> list[HealthCheck]:
    """Run health checks for multiple scrapers and return aggregated results.

    The caller controls both the scraper name (used in the returned
    HealthCheck) and the HTML fixture, keeping validation fully offline.

    Args:
        scrapers: Maps a display name to (scraper_module, fixture_html).
            The display name overrides the module's __name__ in results.

    Returns:
        List of HealthCheck results in iteration order of scrapers.
    """
    results: list[HealthCheck] = []
    for name, (module, html) in scrapers.items():
        hc = check(module, html)
        hc.scraper_name = name
        results.append(hc)
    return results


def main() -> None:
    """Print a human-readable health-check summary and exit 1 on failure.

    Runs check_all against the EIA petroleum scraper using the bundled
    test fixture so the gate runs entirely offline.  Exits with code 1
    if any scraper's status is "fail".
    """
    import os
    from src.scrapers import eia_petroleum

    fixture_path = os.path.join(
        os.path.dirname(__file__), "..", "tests", "fixtures", "eia_petroleum_sample.html"
    )

    scrapers: dict[str, tuple[ModuleType, str]] = {}
    if os.path.exists(fixture_path):
        with open(fixture_path, "r", encoding="utf-8") as fh:
            scrapers["eia_petroleum"] = (eia_petroleum, fh.read())
    else:
        print("WARN: EIA fixture not found; no scrapers checked.")

    results = check_all(scrapers)
    any_fail = False

    for hc in results:
        label = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}[hc.status]
        line = f"[{label}] {hc.scraper_name}: {hc.record_count} record(s)"
        if hc.missing_fields:
            line += f" | missing fields: {', '.join(hc.missing_fields)}"
        print(line)
        if hc.status == "fail":
            any_fail = True

    if any_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
