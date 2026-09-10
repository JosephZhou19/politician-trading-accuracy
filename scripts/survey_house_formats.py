"""Read-only survey: sample a handful of PTR PDFs from many different filers across
several years, to map how many distinct House PTR template "eras" actually exist
(casing, ticker-bracket presence, column x-coordinates) before designing a fix for
src/parse/house_ptr_parser.py. Downloads PDFs to a scratch dir; does not touch the DB.

Usage: python -m scripts.survey_house_formats
"""
import re
import sys
import time
from pathlib import Path

from src.ingest import house_clerk
from src.parse import house_ptr_parser as hp

SCRATCH_DIR = Path("data/raw/house")  # reuse the normal cache; these are real filings
YEARS = [2014, 2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026]
SAMPLES_PER_YEAR = 4


def classify(pdf_path):
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        if len(full_text) < 50:
            return "genuine_scan", None, None

        has_filing_id_exact = "Filing ID" in full_text
        has_bracket = bool(re.search(r"\[[A-Z]{2}\]", full_text))
        # crude case-corruption signal: the literal word "TRANSACTIONS" (all caps,
        # as the modern template renders it) vs a mixed-case corrupted rendering
        clean_case = "TRANSACTIONS" in full_text or "Transaction" in full_text.replace(
            "transaction", "Transaction"
        )

        try:
            columns = hp._derive_columns(pdf)
            if columns is None:
                return "no_header_found", has_filing_id_exact, has_bracket
            raw_rows = hp._extract_raw_rows(pdf, columns)
        except Exception as e:
            return f"extract_error: {e!r}", has_filing_id_exact, has_bracket

    if not raw_rows:
        tag = "zero_rows_extracted"
    else:
        bad = any(
            hp._canonicalize_owner(r["owner_raw"]) not in
            {"self", "spouse", "joint", "dependent_child"}
            for r in raw_rows
        )
        tag = "rows_extracted_but_bad_values" if bad else "rows_extracted_clean"

    return tag, has_filing_id_exact, has_bracket


def main():
    seen_filers = set()
    results = []
    for year in YEARS:
        try:
            rows = house_clerk.search_filings(year)
        except Exception as e:
            print(f"{year}: search failed: {e!r}")
            continue
        ptr_rows = [r for r in rows if house_clerk.is_ptr(r["filing_type_raw"])]
        sampled = 0
        for row in ptr_rows:
            if sampled >= SAMPLES_PER_YEAR:
                break
            name = row["name_raw"]
            if name in seen_filers:
                continue
            seen_filers.add(name)
            try:
                ext_id = house_clerk.external_filing_id(row["pdf_path"])
                pdf_file = SCRATCH_DIR / f"{ext_id}.pdf"
                if not pdf_file.exists():
                    pdf_bytes = house_clerk.download_pdf(row["pdf_path"])
                    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
                    pdf_file.write_bytes(pdf_bytes)
                tag, has_filing_id, has_bracket = classify(pdf_file)
                print(f"{year} | {name[:30]:30s} | {tag:30s} | Filing_ID={has_filing_id} | bracket={has_bracket}")
                results.append((year, name, tag, has_filing_id, has_bracket))
                sampled += 1
                time.sleep(0.5)
            except Exception as e:
                print(f"{year} | {name[:30]:30s} | ERROR: {e!r}")

    print("\n=== summary by year ===")
    by_year = {}
    for year, name, tag, has_id, has_bracket in results:
        by_year.setdefault(year, []).append(tag)
    for year in sorted(by_year):
        tags = by_year[year]
        print(year, {t: tags.count(t) for t in set(tags)})


if __name__ == "__main__":
    main()
