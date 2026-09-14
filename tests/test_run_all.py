from unittest.mock import patch

from src.db import models
from src.ingest import house_clerk, run_all, senate_efd

_EMPTY_HOUSE_SUMMARY = {
    "ptrs_found": 0, "ptrs_new": 0, "ptrs_skipped": 0,
    "ptrs_needs_ocr": 0, "ptrs_failed": 0, "failures": [],
}
_EMPTY_SENATE_SUMMARY = {
    "found": 0, "new": 0, "skipped": 0, "paper": 0, "failed": 0, "failures": [],
}


def test_run_house_builds_dedup_dict_once_and_reuses_it_across_years(conn, tmp_path):
    """Regression for the ingest N+1 latency bug (see PLAN.md): the dedup dict must be
    built once per run and threaded through every filing_year, not rebuilt (or worse,
    replaced by per-row DB calls) inside the loop."""
    seen_dicts = []

    def fake_ingest_ptrs(conn, data_dir, year, last_name="", existing_by_ext_id=None):
        seen_dicts.append(existing_by_ext_id)
        return dict(_EMPTY_HOUSE_SUMMARY)

    with patch.object(
        models, "get_filing_statuses_by_chamber", wraps=models.get_filing_statuses_by_chamber
    ) as mock_bulk, patch.object(house_clerk, "ingest_ptrs", side_effect=fake_ingest_ptrs):
        run_all.run_house(conn, tmp_path, 2024, 2026)

    mock_bulk.assert_called_once_with(conn, "house")
    assert len(seen_dicts) == 3  # one call per year, 2024-2026 inclusive
    assert all(d is seen_dicts[0] for d in seen_dicts), "same dict object must be reused across years"


def test_run_senate_builds_dedup_dict_once_and_passes_it_through(conn, tmp_path):
    with patch.object(
        models, "get_filing_statuses_by_chamber", wraps=models.get_filing_statuses_by_chamber
    ) as mock_bulk, patch.object(
        senate_efd, "ingest_ptrs", return_value=dict(_EMPTY_SENATE_SUMMARY)
    ) as mock_ingest:
        run_all.run_senate(conn, tmp_path)

    mock_bulk.assert_called_once_with(conn, "senate")
    assert isinstance(mock_ingest.call_args.kwargs["existing_by_ext_id"], dict)
