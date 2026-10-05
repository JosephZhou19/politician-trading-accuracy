import datetime
import os
from unittest.mock import Mock, patch

import pytest

from src.analysis.legislator_metadata import (
    build_match_index,
    extract_party_state,
    fetch_external_legislators,
    get_legislator_metadata,
    match_legislator,
    _clean_last_name,
)


def _external(first, last, terms):
    return {"name": {"first": first, "last": last}, "terms": terms}


def _term(party, state):
    return {"party": party, "state": state}


@pytest.mark.parametrize("raw,expected", [
    ("McConnell, Jr.", "McConnell"),
    ("King, Jr.", "King"),
    ("Justice, II", "Justice"),
    ("Manchin, III", "Manchin"),
    ("Hagerty, IV", "Hagerty"),
    ("Moran,", "Moran"),
    ("Pelosi", "Pelosi"),
])
def test_clean_last_name_strips_known_suffix_shapes(raw, expected):
    assert _clean_last_name(raw) == expected


def test_build_match_index_keys_by_uppercased_first_last():
    external = [_external("Nancy", "Pelosi", [_term("Democrat", "CA")])]
    index = build_match_index(external)
    assert ("NANCY", "PELOSI") in index
    assert index[("NANCY", "PELOSI")]["name"]["last"] == "Pelosi"


def test_match_legislator_direct_match_after_suffix_cleaning():
    index = build_match_index([_external("Angus", "King", [_term("Independent", "ME")])])
    assert match_legislator("Angus", "King, Jr.", index) is not None


def test_match_legislator_uses_alias_table_for_legal_vs_common_name():
    # Our source data stores Tammy Duckworth's legal first name, "Ladda" - the external
    # roster (and everyone else) indexes her as "Tammy".
    index = build_match_index([_external("Tammy", "Duckworth", [_term("Democrat", "IL")])])
    assert match_legislator("Ladda", "Duckworth", index) is not None


def test_match_legislator_uses_alias_table_for_formal_name_vs_nickname():
    # Confirmed live: our #1 most-active trader by volume, Thomas MacArthur, is indexed in
    # the external roster under his nickname "Tom" - checked directly, not a blanket rule
    # (some "Thomas"es, like Thomas Carper, are indexed under their formal name instead).
    index = build_match_index([_external("Tom", "MacArthur", [_term("Republican", "NJ")])])
    assert match_legislator("Thomas", "MacArthur", index) is not None


def test_match_legislator_returns_none_when_genuinely_unmatched():
    index = build_match_index([_external("Nancy", "Pelosi", [_term("Democrat", "CA")])])
    assert match_legislator("Someone", "Unknown", index) is None


def test_extract_party_state_uses_the_most_recent_term():
    leg = _external("Nancy", "Pelosi", [_term("Democrat", "CA"), _term("Democrat", "CA")])
    assert extract_party_state(leg) == ("Democrat", "CA")


def test_extract_party_state_handles_no_terms():
    assert extract_party_state({"name": {"first": "X", "last": "Y"}, "terms": []}) == (None, None)


def test_get_legislator_metadata_matches_and_skips_unmatched(monkeypatch):
    external = [_external("Nancy", "Pelosi", [_term("Democrat", "CA")])]
    monkeypatch.setattr(
        "src.analysis.legislator_metadata.fetch_external_legislators", lambda cache_dir=None: external,
    )

    metadata = get_legislator_metadata([(1, "Nancy", "Pelosi"), (2, "Unknown", "Person")])

    assert metadata[1] == {"party": "Democrat", "state": "CA"}
    assert 2 not in metadata


def test_fetch_external_legislators_uses_fresh_cache_without_a_network_call(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "legislators-current.yaml").write_text(
        "- name: {first: Nancy, last: Pelosi}\n  terms: [{party: Democrat, state: CA}]\n",
        encoding="utf-8",
    )
    (cache_dir / "legislators-historical.yaml").write_text("[]\n", encoding="utf-8")

    with patch("src.analysis.legislator_metadata.requests.get") as mock_get:
        result = fetch_external_legislators(cache_dir)

    mock_get.assert_not_called()
    assert result == [{"name": {"first": "Nancy", "last": "Pelosi"}, "terms": [{"party": "Democrat", "state": "CA"}]}]


def test_fetch_external_legislators_refetches_a_stale_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    stale_current = cache_dir / "legislators-current.yaml"
    stale_current.write_text("[]\n", encoding="utf-8")
    stale_historical = cache_dir / "legislators-historical.yaml"
    stale_historical.write_text("[]\n", encoding="utf-8")
    old_time = (datetime.datetime.now() - datetime.timedelta(days=31)).timestamp()
    os.utime(stale_current, (old_time, old_time))
    os.utime(stale_historical, (old_time, old_time))

    mock_response = Mock(text="- name: {first: Nancy, last: Pelosi}\n  terms: []\n")
    mock_response.raise_for_status = Mock()
    with patch("src.analysis.legislator_metadata.requests.get", return_value=mock_response) as mock_get:
        result = fetch_external_legislators(cache_dir)

    assert mock_get.call_count == 2  # both current and historical refetched
    assert result == [{"name": {"first": "Nancy", "last": "Pelosi"}, "terms": []}] * 2


def test_fetch_external_legislators_orders_historical_before_current(tmp_path):
    """Regression: build_match_index keeps whichever entry comes LAST for a given name.
    Confirmed live, 14 current legislators - including Senator Jack Reed, matched through
    this module's own Reed alias - share a (first, last) name with an unrelated historical
    figure; with current loaded first, match_index silently resolved Reed to a 19th-century
    Massachusetts Whig instead of the sitting RI Senator. historical must load first so
    current (loaded second) wins the collision."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "legislators-current.yaml").write_text(
        "- name: {first: John, last: Reed}\n  terms: [{party: Democrat, state: RI}]\n",
        encoding="utf-8",
    )
    (cache_dir / "legislators-historical.yaml").write_text(
        "- name: {first: John, last: Reed}\n  terms: [{party: Whig, state: MA}]\n",
        encoding="utf-8",
    )

    external = fetch_external_legislators(cache_dir)
    index = build_match_index(external)

    assert index[("JOHN", "REED")]["terms"][0]["party"] == "Democrat"
