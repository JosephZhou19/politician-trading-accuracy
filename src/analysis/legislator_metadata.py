"""Matches our `legislators` table against the `unitedstates/congress-legislators` project
(GitHub, public domain, bioguide-ID indexed) to get party and state - two fields our own
ingest pipeline never captures, since House/Senate PTR filings don't carry them directly.

Confirmed during an earlier feasibility check this session: 418/637 (66%) of our legislators
match automatically by (first_name, last_name) once trailing suffixes ("Jr.", ", III", etc.)
are stripped from our own `last_name` field - a pre-existing quirk in our name parsing, not
in the external data. Most of the remainder are legal-name-vs-commonly-used-name mismatches
(our source data uses the legal name from the PTR filing; the external roster - and common
knowledge - uses the name the person actually goes by) or a handful of other known specific
cases, handled by NAME_ALIASES below. Unmatched legislators simply get party=None, state=None
- there's no silent wrong guess here, just missing data the site displays as "—".
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Optional

import requests
import yaml

LEGISLATORS_CURRENT_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/legislators-current.yaml"
)
LEGISLATORS_HISTORICAL_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/legislators-historical.yaml"
)
DEFAULT_CACHE_DIR = Path(".cache/congress-legislators")
CACHE_MAX_AGE_DAYS = 30

_SUFFIX_RE = re.compile(r",?\s*(JR\.?|SR\.?|II|III|IV)\.?\s*$", re.IGNORECASE)

# Confirmed live (see module docstring): our source data's first_name is the PTR filing's
# legal name, which for these specific people isn't the name they're otherwise known/indexed
# by. Keyed by (our first_name, our cleaned last_name) exactly as stored in our DB.
NAME_ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    ("Ladda", "Duckworth"): ("Tammy", "Duckworth"),
    ("Rafael", "Cruz"): ("Ted", "Cruz"),
    ("Rohit", "Khanna"): ("Ro", "Khanna"),
    ("JACK", "REED"): ("John", "Reed"),
    ("Jacklyn", "Rosen"): ("Jacky", "Rosen"),
    ("Cathy", "Rodgers"): ("Cathy", "McMorris Rodgers"),
    # Confirmed live: our top-traded legislators include several "formal first name in our
    # source data, nickname in the external roster" mismatches - checked directly against
    # the external data rather than guessed, since e.g. "Thomas" Carper actually IS indexed
    # under his formal name, so this isn't a blanket first-name transform.
    ("Thomas", "MacArthur"): ("Tom", "MacArthur"),
    ("Thomas", "Tuberville"): ("Tommy", "Tuberville"),
    ("Daniel", "Goldman"): ("Dan", "Goldman"),
    ("David", "Trott"): ("Dave", "Trott"),
    ("David", "McCormick"): ("Dave", "McCormick"),
    # The mismatch direction flips here - our own source data has the nickname, the external
    # roster has the formal name. Confirmed directly, not assumed from the other direction.
    ("Rob", "Bresnahan"): ("Robert", "Bresnahan"),
}


def _clean_last_name(name: str) -> str:
    name = name.strip().rstrip(",").strip()
    return _SUFFIX_RE.sub("", name).strip()


def fetch_external_legislators(cache_dir: Path = DEFAULT_CACHE_DIR) -> list[dict]:
    """Downloads (or reuses a <=30-day-old local cache of) the external roster - historical
    members first, then current ones appended last. Order matters here: build_match_index
    keeps the *last* entry it sees for a given name, and someone no longer in office (Green,
    Perdue, etc.) still needs to match, but a currently-serving member sharing a name with
    a 19th-century predecessor must resolve to the current one, not the other way around -
    confirmed live, 14 current legislators (including Senator Jack Reed, matched via this
    module's own Reed alias) share a (first, last) name with some unrelated historical
    figure; with current loaded first, the old pre-fix logic would silently take whichever
    historical entry happened to be read later in the file. This is the one external
    network call in the whole website export pipeline, made once per run against GitHub,
    not Turso - entirely unrelated to the production database's quota."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    legislators = []
    for name, url in [("legislators-historical.yaml", LEGISLATORS_HISTORICAL_URL),
                       ("legislators-current.yaml", LEGISLATORS_CURRENT_URL)]:
        cache_path = cache_dir / name
        if cache_path.exists():
            age_days = (datetime.datetime.now().timestamp() - cache_path.stat().st_mtime) / 86400
        else:
            age_days = None
        if age_days is None or age_days > CACHE_MAX_AGE_DAYS:
            response = requests.get(url, timeout=60)
            response.raise_for_status()
            cache_path.write_text(response.text, encoding="utf-8")
        legislators.extend(yaml.safe_load(cache_path.read_text(encoding="utf-8")))
    return legislators


def build_match_index(external_legislators: list[dict]) -> dict[tuple[str, str], dict]:
    """Keyed by (FIRST, LAST) uppercased, using the external roster's own name spelling -
    the lookup side always normalizes our DB's name the same way before checking this index.
    On a name collision, whichever entry comes LAST in `external_legislators` wins - callers
    must order historical before current (see fetch_external_legislators) so a currently-
    serving member always takes priority over a same-named predecessor."""
    index = {}
    for leg in external_legislators:
        key = (leg["name"]["first"].upper(), leg["name"]["last"].upper())
        index[key] = leg
    return index


def match_legislator(first_name: str, last_name: str, match_index: dict[tuple[str, str], dict]) -> Optional[dict]:
    cleaned_last = _clean_last_name(last_name)
    alias = NAME_ALIASES.get((first_name, cleaned_last))
    if alias:
        first_name, cleaned_last = alias
    return match_index.get((first_name.upper(), cleaned_last.upper()))


def extract_party_state(external_legislator: dict) -> tuple[Optional[str], Optional[str]]:
    """Party and state can change across a career (redistricting, a party switch) - the most
    recent term is "current" for an active member and "as of leaving office" for a former
    one, which is the only sensible single answer to show on a directory page."""
    terms = external_legislator.get("terms") or []
    if not terms:
        return None, None
    last_term = terms[-1]
    return last_term.get("party"), last_term.get("state")


def get_legislator_metadata(
    our_legislators: list[tuple[int, str, str]], cache_dir: Path = DEFAULT_CACHE_DIR,
) -> dict[int, dict]:
    """`our_legislators` is a list of (id, first_name, last_name) tuples. Returns
    {id: {"party": ..., "state": ...}} for every legislator that matched - omitted entirely,
    not a None-filled entry, for anyone unmatched so a caller's .get(id, {}) reads cleanly."""
    external = fetch_external_legislators(cache_dir)
    match_index = build_match_index(external)

    metadata = {}
    for leg_id, first_name, last_name in our_legislators:
        matched = match_legislator(first_name, last_name, match_index)
        if matched is None:
            continue
        party, state = extract_party_state(matched)
        metadata[leg_id] = {"party": party, "state": state}
    return metadata
