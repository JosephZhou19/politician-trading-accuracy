"""Full-history ingestion pipeline for both chambers.

Safe to run repeatedly (e.g. on a daily schedule) - both scrapers skip anything already
successfully parsed, so a re-run only fetches genuinely new filings. See PLAN.md for the
recommended way to schedule this (Windows Task Scheduler) - this script itself has no
scheduling logic, it's a single pass.

Usage:
    python -m src.ingest.run_all
    python -m src.ingest.run_all --db data/congress_trades.db --house-start-year 2012
"""

import argparse
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from src.db import models
from src.ingest import house_clerk, senate_efd

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/congress_trades.db"
DEFAULT_HOUSE_START_YEAR = 2012


def _configure_logging(data_dir):
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "ingest.log"),
            logging.StreamHandler(),
        ],
    )


def run_house(conn, data_dir, start_year, end_year):
    run_id = models.start_ingestion_run(conn, "house", datetime.now(timezone.utc).isoformat())
    totals = {"ptrs_found": 0, "ptrs_new": 0, "ptrs_skipped": 0, "ptrs_needs_ocr": 0, "ptrs_failed": 0}
    error_message = None
    try:
        for year in range(start_year, end_year + 1):
            logger.info("House: searching filing year %d", year)
            summary = house_clerk.ingest_ptrs(conn, data_dir, year)
            logger.info("House %d: %s", year, {k: v for k, v in summary.items() if k != "failures"})
            for key in totals:
                totals[key] += summary[key]
        status = "completed"
    except Exception as e:
        logger.exception("House ingestion aborted")
        status = "failed"
        error_message = repr(e)
    models.finish_ingestion_run(
        conn,
        run_id,
        finished_at=datetime.now(timezone.utc).isoformat(),
        filings_found=totals["ptrs_found"],
        filings_new=totals["ptrs_new"],
        filings_failed=totals["ptrs_failed"],
        status=status,
        error_message=error_message,
    )
    return totals


def run_senate(conn, data_dir):
    run_id = models.start_ingestion_run(conn, "senate", datetime.now(timezone.utc).isoformat())
    totals = {"found": 0, "new": 0, "skipped": 0, "paper": 0, "failed": 0}
    error_message = None
    try:
        filer_types = (
            senate_efd.FILER_TYPE_SENATOR,
            senate_efd.FILER_TYPE_CANDIDATE,
            senate_efd.FILER_TYPE_FORMER_SENATOR,
        )
        summary = senate_efd.ingest_ptrs(conn, data_dir, filer_types=filer_types)
        logger.info("Senate: %s", {k: v for k, v in summary.items() if k != "failures"})
        for key in totals:
            totals[key] += summary[key]
        status = "completed"
    except Exception as e:
        logger.exception("Senate ingestion aborted")
        status = "failed"
        error_message = repr(e)
    models.finish_ingestion_run(
        conn,
        run_id,
        finished_at=datetime.now(timezone.utc).isoformat(),
        filings_found=totals["found"],
        filings_new=totals["new"],
        filings_failed=totals["failed"],
        status=status,
        error_message=error_message,
    )
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to the SQLite database")
    parser.add_argument("--data-dir", default="data", help="Directory for raw filing cache and logs")
    parser.add_argument("--house-start-year", type=int, default=DEFAULT_HOUSE_START_YEAR)
    parser.add_argument("--house-end-year", type=int, default=date.today().year)
    parser.add_argument("--chamber", choices=["house", "senate", "both"], default="both")
    args = parser.parse_args()

    _configure_logging(args.data_dir)
    conn = models.connect(args.db)

    if args.chamber in ("house", "both"):
        house_totals = run_house(conn, args.data_dir, args.house_start_year, args.house_end_year)
        logger.info("House ingestion complete: %s", house_totals)

    if args.chamber in ("senate", "both"):
        senate_totals = run_senate(conn, args.data_dir)
        logger.info("Senate ingestion complete: %s", senate_totals)


if __name__ == "__main__":
    main()
