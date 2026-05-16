import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Literal


@dataclass
class HealthCheck:
    scraper_name: str
    record_count: int
    missing_fields: list[str]
    status: Literal["ok", "warn", "fail"]


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    # bool is a subclass of int; exclude it so False is not flagged as missing
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0.0
    return False


def check(scraper_module: ModuleType, html: str) -> HealthCheck:
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
    results: list[HealthCheck] = []
    for name, (module, html) in scrapers.items():
        hc = check(module, html)
        hc.scraper_name = name
        results.append(hc)
    return results


def main() -> None:
    import os
    from src.scrapers import eia_petroleum, treasury_yield_curve

    fixtures_dir = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures")

    scrapers: dict[str, tuple[ModuleType, str]] = {}

    eia_fixture = os.path.join(fixtures_dir, "eia_petroleum_sample.html")
    if os.path.exists(eia_fixture):
        with open(eia_fixture, "r", encoding="utf-8") as fh:
            scrapers["eia_petroleum"] = (eia_petroleum, fh.read())

    treasury_fixture = os.path.join(fixtures_dir, "treasury_yield_curve_sample.html")
    if os.path.exists(treasury_fixture):
        with open(treasury_fixture, "r", encoding="utf-8") as fh:
            scrapers["treasury_yield_curve"] = (treasury_yield_curve, fh.read())

    if not scrapers:
        print("WARN: no fixtures found; no scrapers checked.")

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
