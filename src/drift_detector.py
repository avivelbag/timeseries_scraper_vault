"""Schema drift detector for catching site layout changes early.

Fetches each scraper's live URL, calls the scraper's run() function, and
verifies that all REQUIRED_FIELDS are present with non-default values in at
least one returned record.  Never calls bq_uploader; read-only validation only.
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.http_client import fetch

_DEFAULT_REPORT_PATH = Path("workspace/scraper/drift_report.json")


@dataclass
class DriftReport:
    """Result of a single scraper drift check.

    status values:
        "ok"      — all required fields are present and non-default in the data.
        "degraded" — reserved for partial coverage (not currently emitted).
        "broken"  — fetch failed, no records returned, or required fields absent/default.
    """

    scraper_name: str
    url: str
    status: str
    missing_fields: list[str]
    sample_record_count: int
    checked_at: str


def _is_default(value: Any) -> bool:
    """Return True when value matches a proto default (empty string or numeric zero)."""
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (int, float)):
        return value == 0
    return False


def _fetch_content(url: str) -> str | None:
    """Return page text for url using the shared polite-fetch client, or None on error.

    Wraps src.http_client.fetch, which already enforces a User-Agent header,
    a 2–5 s random sleep before the request, and exponential backoff on
    429/5xx responses.  Any exception (robots disallowed, connection error,
    non-2xx after retries) collapses to None so callers can report "broken".
    """
    try:
        resp = fetch(url)
        return resp.text
    except Exception:
        return None


def run_drift_check(scraper_module: Any, live_url: str, proto_class: Any) -> DriftReport:
    """Check a single scraper for schema drift against its live URL.

    Fetches live_url, calls scraper_module.run() on the response text, then
    checks that every field listed in scraper_module.REQUIRED_FIELDS appears
    with a non-default value in at least one returned record.

    Checking across all records (rather than only the first) handles scrapers
    like usda_crop_progress where progress and condition records carry mutually
    exclusive non-zero fields — both field groups appear in at least one record
    of a healthy response.

    A field value is considered "default" (missing) when it is an empty string
    for str fields or zero for int/float fields, matching protobuf defaults.

    Args:
        scraper_module: Module exposing run(html: str) -> list[dict] and
            REQUIRED_FIELDS: list[str].
        live_url: URL to fetch for the check.
        proto_class: Proto/dataclass class for the scraper's records.  Accepted
            for interface compatibility; field validation uses REQUIRED_FIELDS.

    Returns:
        DriftReport with status "ok" when all required fields are found
        non-default in at least one record, "broken" otherwise.
    """
    scraper_name = scraper_module.__name__.split(".")[-1]
    checked_at = datetime.now(timezone.utc).isoformat()

    content = _fetch_content(live_url)
    if content is None:
        return DriftReport(
            scraper_name=scraper_name,
            url=live_url,
            status="broken",
            missing_fields=["fetch_failed"],
            sample_record_count=0,
            checked_at=checked_at,
        )

    try:
        records = scraper_module.run(content)
    except Exception:
        return DriftReport(
            scraper_name=scraper_name,
            url=live_url,
            status="broken",
            missing_fields=["parse_failed"],
            sample_record_count=0,
            checked_at=checked_at,
        )

    if not records:
        return DriftReport(
            scraper_name=scraper_name,
            url=live_url,
            status="broken",
            missing_fields=["no_records"],
            sample_record_count=0,
            checked_at=checked_at,
        )

    required_fields: list[str] = getattr(scraper_module, "REQUIRED_FIELDS", [])
    missing = [
        f
        for f in required_fields
        if not any(f in r and not _is_default(r[f]) for r in records)
    ]

    return DriftReport(
        scraper_name=scraper_name,
        url=live_url,
        status="broken" if missing else "ok",
        missing_fields=missing,
        sample_record_count=len(records),
        checked_at=checked_at,
    )


def run_all_checks(
    registry: list[dict],
    report_path: Path | None = None,
) -> list[DriftReport]:
    """Run drift checks for all scrapers in the registry sequentially.

    Iterates registry entries in order, calling run_drift_check() for each,
    with at least 3 seconds of sleep between consecutive scrapers to avoid
    triggering rate limits across different data sources.

    Writes the combined results to report_path as a JSON array so the
    orchestrator or a human can inspect them without running the full pipeline.

    Args:
        registry: List of dicts, each with keys "scraper_module", "url",
            "proto_class".
        report_path: Path to write the JSON report.  Defaults to
            workspace/scraper/drift_report.json (relative to cwd).

    Returns:
        List of DriftReport instances in registry order, one per entry.
    """
    if report_path is None:
        report_path = _DEFAULT_REPORT_PATH

    reports: list[DriftReport] = []
    for i, entry in enumerate(registry):
        if i > 0:
            time.sleep(3)
        report = run_drift_check(
            entry["scraper_module"],
            entry["url"],
            entry["proto_class"],
        )
        reports.append(report)

    out = Path(report_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(
            [
                {
                    "scraper_name": r.scraper_name,
                    "url": r.url,
                    "status": r.status,
                    "missing_fields": r.missing_fields,
                    "sample_record_count": r.sample_record_count,
                    "checked_at": r.checked_at,
                }
                for r in reports
            ],
            fh,
            indent=2,
        )

    return reports
