"""Background orchestrator that runs scrapers on a due-check schedule.

Each scraper under ``src/scrapers`` is a standalone module exposing
``main()`` and runnable as ``python -m src.scrapers.<name>``.  This
orchestrator wraps all of them so the whole vault can be run as a single
long-lived background process:

    nohup python3 -m src.orchestrator >/dev/null 2>&1 &

On each pass it computes which scrapers are *due* (based on each source's
natural release cadence and the timestamp of its last successful run,
persisted in a JSON state file), runs only those, records successes, and
then sleeps until the next pass (default ~24 h).  A failed scraper does
not update its last-success timestamp, so it is retried on the next pass.

The default loop mode is for the current "start it with nohup" workflow;
``--once`` performs a single due-check pass and exits, which is what a
future cron entry or systemd timer should call.
"""

import argparse
import ast
import glob
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("orchestrator")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRAPERS_DIR = REPO_ROOT / "src" / "scrapers"
DEFAULT_STATE_PATH = REPO_ROOT / "var" / "orchestrator_state.json"
DEFAULT_LOG_PATH = REPO_ROOT / "var" / "orchestrator.log"

# Run scrapers under the project venv if present, so they get the
# installed dependencies (google-cloud-bigquery, etc.) regardless of which
# interpreter launched the orchestrator. Falls back to the current one.
_VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
SCRAPER_PYTHON = str(_VENV_PYTHON) if os.access(_VENV_PYTHON, os.X_OK) else sys.executable

# Named cadences -> minimum days between runs.  A scraper is "due" once
# this much time has elapsed since its last successful run.  Monthly uses
# 28 days so a monthly release is never missed by calendar drift; running
# slightly early is harmless because BigQuery upload dedups by date.
CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 28}

# Per-scraper cadence, derived from each source's documented release
# schedule.  Scrapers not listed here default to "weekly" (see
# DEFAULT_CADENCE) with a warning, so a newly added scraper still runs.
SCRAPER_CADENCE = {
    "bls_cpi": "monthly",
    "bls_ppi": "monthly",
    "cdc_fluview": "weekly",
    "cftc_cot": "weekly",
    "eia_electricity": "weekly",
    "eia_natural_gas": "monthly",
    "eia_petroleum": "weekly",
    "fao_food_price_index": "monthly",
    "fed_consumer_credit": "monthly",
    "fed_h15_rates": "daily",
    "nasa_giss_temp": "monthly",
    "noaa_co2": "monthly",
    "treasury_yield_curve": "daily",
    "usda_crop_progress": "weekly",
    "us_drought_monitor": "weekly",
    "usgs_streamflow": "daily",
}
DEFAULT_CADENCE = "weekly"

# Slack subtracted from the cadence interval when testing due-ness, so a
# pass that runs a little later each day does not skip a scheduled run.
DUE_SLACK = timedelta(hours=12)


def _defines_main(path: str) -> bool:
    """True if the module defines a top-level ``def main``.

    Parsed statically (no import/exec) so shared helpers placed under
    src/scrapers (e.g. http_client.py, which has no main()) are excluded
    while any real scraper is picked up automatically.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError) as exc:
        logger.warning("skipping %s: cannot parse (%s)", path, exc)
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "main"
        for node in tree.body
    )


def discover_scrapers() -> list[str]:
    """Return scraper module names found under src/scrapers (sorted).

    Only modules that define a top-level ``main()`` are returned, so
    helper modules in the same package are not treated as scrapers.
    """
    names = []
    for path in sorted(glob.glob(str(SCRAPERS_DIR / "*.py"))):
        stem = Path(path).stem
        if stem.startswith("_"):
            continue
        if not _defines_main(path):
            continue
        names.append(stem)
    return names


def cadence_of(name: str) -> str:
    """Return the cadence label for a scraper, defaulting to weekly."""
    cadence = SCRAPER_CADENCE.get(name)
    if cadence is None:
        logger.warning(
            "no cadence configured for %r; defaulting to %s", name, DEFAULT_CADENCE
        )
        return DEFAULT_CADENCE
    return cadence


def load_state(state_path: Path) -> dict[str, str]:
    """Load the {scraper: last_success_iso} map; empty if missing/corrupt."""
    try:
        with open(state_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        logger.warning("state file %s is not an object; ignoring", state_path)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("could not read state file %s: %s", state_path, exc)
    return {}


def save_state(state_path: Path, state: dict[str, str]) -> None:
    """Atomically write the last-success map to disk."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, state_path)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_due(name: str, state: dict[str, str], now: datetime) -> bool:
    """True if the scraper has never succeeded or its cadence has elapsed."""
    last = _parse_ts(state.get(name))
    if last is None:
        return True
    interval = timedelta(days=CADENCE_DAYS[cadence_of(name)])
    return (now - last) >= (interval - DUE_SLACK)


def run_scraper(name: str, timeout: int) -> bool:
    """Run one scraper as a subprocess. Return True on exit code 0."""
    logger.info("running %s ...", name)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [SCRAPER_PYTHON, "-m", f"src.scrapers.{name}"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("%s timed out after %ss", name, timeout)
        return False
    elapsed = time.monotonic() - started
    if proc.returncode == 0:
        logger.info("%s OK (%.0fs)", name, elapsed)
        return True
    logger.error(
        "%s FAILED rc=%s (%.0fs)\n--- stderr tail ---\n%s",
        name,
        proc.returncode,
        elapsed,
        "\n".join(proc.stderr.strip().splitlines()[-20:]) or "(empty)",
    )
    return False


def run_pass(
    state_path: Path,
    only: list[str] | None,
    timeout: int,
    dry_run: bool,
) -> tuple[int, int]:
    """Run one due-check pass. Return (succeeded, failed) counts."""
    now = datetime.now(timezone.utc)
    state = load_state(state_path)
    scrapers = discover_scrapers()

    if only:
        unknown = sorted(set(only) - set(scrapers))
        if unknown:
            logger.error("unknown scraper(s): %s", ", ".join(unknown))
        due = [n for n in scrapers if n in only]
    else:
        due = [n for n in scrapers if is_due(n, state, now)]

    if not due:
        logger.info("nothing due (%d scrapers checked)", len(scrapers))
        return (0, 0)

    logger.info("due this pass: %s", ", ".join(due))
    if dry_run:
        logger.info("dry-run: not executing")
        return (0, 0)

    succeeded = failed = 0
    for name in due:
        if run_scraper(name, timeout):
            succeeded += 1
            state[name] = now.isoformat()
            save_state(state_path, state)
        else:
            failed += 1
    logger.info("pass complete: %d succeeded, %d failed", succeeded, failed)
    return (succeeded, failed)


def print_status(state_path: Path) -> None:
    """Print each scraper's cadence, last run, and due status."""
    now = datetime.now(timezone.utc)
    state = load_state(state_path)
    print(f"{'scraper':<26}{'cadence':<10}{'last success (UTC)':<28}due")
    print("-" * 70)
    for name in discover_scrapers():
        last = state.get(name, "")
        due = "YES" if is_due(name, state, now) else "no"
        print(f"{name:<26}{cadence_of(name):<10}{last or '(never)':<28}{due}")


def configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger("orchestrator")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(stream)
    root.addHandler(file_handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.orchestrator",
        description="Run scrapers on a due-check schedule.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single due-check pass and exit (use this from cron/systemd)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report which scrapers are due without running them",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print cadence/last-run/due status for every scraper and exit",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="NAME",
        help="run only these scrapers (forces them, ignoring due-check)",
    )
    parser.add_argument(
        "--interval-hours",
        type=float,
        default=24.0,
        help="hours to sleep between passes in loop mode (default: 24)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="per-scraper subprocess timeout in seconds (default: 1800)",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"state file path (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG_PATH,
        help=f"log file path (default: {DEFAULT_LOG_PATH})",
    )
    args = parser.parse_args(argv)

    configure_logging(args.log)

    if args.list:
        print_status(args.state)
        return 0

    if args.once or args.dry_run or args.only:
        _, failed = run_pass(args.state, args.only, args.timeout, args.dry_run)
        return 1 if failed else 0

    logger.info(
        "orchestrator started (loop mode, every %.1f h); state=%s log=%s",
        args.interval_hours,
        args.state,
        args.log,
    )
    sleep_seconds = max(60.0, args.interval_hours * 3600.0)
    try:
        while True:
            run_pass(args.state, None, args.timeout, dry_run=False)
            logger.info("sleeping %.1f h until next pass", args.interval_hours)
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        logger.info("interrupted; exiting")
        return 0


if __name__ == "__main__":
    sys.exit(main())
