"""Schema drift detector — fetches live URLs and validates field coverage against REQUIRED_FIELDS."""

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
    scraper_name: str
    url: str
    status: str
    missing_fields: list[str]
    sample_record_count: int
    checked_at: str


def _is_default(value: Any) -> bool:
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (int, float)):
        return value == 0
    return False


def _fetch_content(url: str) -> str | None:
    try:
        resp = fetch(url)
        return resp.text
    except Exception:
        return None


def run_drift_check(scraper_module: Any, live_url: str, proto_class: Any) -> DriftReport:
    """Fetch live_url, call scraper_module.run(), and check REQUIRED_FIELDS coverage."""
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
    """Run drift checks sequentially (≥3 s between scrapers) and write JSON report."""
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
