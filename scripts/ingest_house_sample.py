"""One-off script: ingest a handful of specific, known-active House traders across
recent years, to get real House data into the sample DB for spot-checking against
Quiver without running a full unrestricted multi-year backfill.
"""
import sys

from src.db import models
from src.ingest import house_clerk

# Start year per person is their actual first year in office (per their Quiver profile's
# "Years Active"), so the ingested total should match Quiver's full-history count exactly.
NAME_START_YEARS = {
    "Gottheimer": 2017,
    "Pelosi": 2014,
    "Greene": 2021,
}
END_YEAR = 2026


def main(db_path="data/sample.db"):
    conn = models.connect(db_path)
    for last_name, start_year in NAME_START_YEARS.items():
        for year in range(start_year, END_YEAR + 1):
            summary = house_clerk.ingest_ptrs(conn, "data", year, last_name=last_name)
            print(last_name, year, {k: v for k, v in summary.items() if k != "failures"})
            for ext_id, err in summary["failures"]:
                print("  FAILED", ext_id, err)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/sample.db")
