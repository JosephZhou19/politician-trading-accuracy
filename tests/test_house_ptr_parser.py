"""Tests for src/parse/house_ptr_parser.py's per-document column derivation.

Real bug found via a broad survey of cached filings across 2014-2026: the House
Clerk's PDF template has shifted column x-coordinates and label casing multiple
times, with different real filers using different layouts even within the same
calendar year - a fixed pixel table only ever matched the current layout and
silently returned zero rows for every other one. Fixed by deriving column
boundaries per-document from the header row's own label words, which line up with
their data column almost exactly (confirmed within ~0.1pt - a font-metric
artifact, not noise).

Word coordinates below are taken directly from real cached filings (not invented),
covering the three layouts confirmed to exist: current-modern (2022+), an earlier
layout with shifted columns but otherwise-modern casing/ticker-bracket (Gottheimer,
~2018-2021), and the oldest legacy layout with case-corrupted labels and no ticker
bracket at all (Pelosi, pre-2019ish).
"""
import pytest

from src.parse.house_ptr_parser import (
    UnparseableFormError,
    _derive_columns,
    _extract_filing_status,
    _extract_raw_rows,
    _extract_ticker_and_asset_type,
    _to_iso_date,
    parse_filing,
)


def _word(text, x0, x1, top):
    return {"text": text, "x0": x0, "x1": x1, "top": top}


class FakePage:
    def __init__(self, words, text=""):
        self._words = words
        self._text = text

    def extract_words(self):
        return self._words

    def extract_text(self):
        return self._text


class FakePDF:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


# Real coordinates from a 2024 Gottheimer filing (current-modern layout): header on
# one line plus a "Type"/"Date" sub-header line, data lines up ~0.12pt left of header.
MODERN_HEADER_WORDS = [
    _word("ID", 25.5, 38.0, 295.4),
    _word("Owner", 65.07, 97.82, 281.4),
    _word("Asset", 104.07, 130.09, 281.4),
    _word("Transaction", 245.2, 305.7, 281.4),
    _word("Date", 326.82, 349.9, 281.4),
    _word("Notification", 383.07, 443.1, 281.4),
    _word("Amount", 447.57, 488.2, 281.4),
    _word("Type", 262.32, 286.5, 292.6),
    _word("Date", 383.07, 406.5, 292.6),
]
MODERN_DATA_ROW = [
    _word("JT", 64.95, 75.17, 326.0),
    _word("Adobe", 103.95, 129.39, 326.0),
    _word("Inc.", 131.56, 146.90, 326.0),
    _word("Stock", 193.44, 215.35, 326.0),
    _word("(ADBE)", 217.52, 248.82, 326.0),
    _word("S", 262.20, 267.24, 326.0),
    _word("06/13/2024", 326.70, 375.25, 326.0),
    _word("07/08/2024", 382.95, 432.98, 326.0),
    _word("$1,001", 447.45, 474.14, 326.0),
    _word("-", 476.31, 479.68, 326.0),
    _word("$15,000", 481.85, 514.96, 326.0),
]

# Real coordinates from a ~2018-2021 Gottheimer-era filing: same casing/bracket as
# modern, but every column shifted left relative to the current layout.
SHIFTED_HEADER_WORDS = [
    _word("Owner", 61.5, 71.7, 281.4),
    _word("Asset", 100.5, 126.7, 281.4),
    _word("Transaction", 213.0, 270.0, 281.4),
    _word("Date", 313.5, 336.9, 281.4),
    _word("Notification", 366.0, 415.1, 281.4),
    _word("Amount", 431.2, 458.0, 281.4),
    _word("Type", 248.2, 272.4, 292.6),
    _word("Date", 366.0, 389.4, 292.6),
]
SHIFTED_DATA_ROW = [
    _word("JT", 61.5, 71.7, 340.2),
    _word("Affirm", 100.5, 126.7, 340.2),
    _word("Holdings,", 128.0, 160.0, 340.2),
    _word("Inc.", 162.0, 172.0, 340.2),
    _word("(AFRM)", 174.0, 199.3, 340.2),
    _word("S", 248.2, 253.3, 340.2),
    _word("12/28/2021", 313.5, 360.7, 340.2),
    _word("01/04/2022", 366.0, 415.1, 340.2),
    _word("$1,001", 431.2, 458.0, 340.2),
    _word("-", 460.0, 463.0, 340.2),
    _word("$15,000", 465.0, 495.0, 340.2),
]

# Real coordinates from a 2018 Pelosi filing (oldest legacy layout): small-caps
# case-corrupted labels ("sP" not "SP", lowercase "s" transaction type), no
# ticker-bracket suffix, and no "Cap. Gains" column at all.
LEGACY_HEADER_WORDS = [
    _word("owner", 63.0, 96.7, 295.4),
    _word("asset", 102.0, 128.8, 295.4),
    _word("transaction", 267.8, 328.2, 295.4),
    _word("Date", 333.0, 356.4, 295.4),
    _word("notification", 385.5, 445.6, 295.4),
    _word("amount", 450.8, 491.4, 295.4),
    _word("type", 267.8, 291.9, 306.6),
    _word("Date", 385.5, 408.9, 306.6),
]
LEGACY_DATA_ROW = [
    _word("sP", 63.0, 73.5, 329.0),
    _word("apple", 102.0, 125.2, 329.0),
    _word("Inc.", 127.4, 142.8, 329.0),
    _word("(aaPl)", 144.9, 174.7, 329.0),
    _word("s", 267.8, 272.8, 329.0),
    _word("12/21/2017", 333.0, 378.2, 329.0),
    _word("12/21/2017", 385.5, 430.7, 329.0),
    _word("$100,001", 450.8, 488.6, 329.0),
    _word("-", 490.8, 494.1, 329.0),
    _word("$250,000", 496.3, 536.2, 329.0),
]


# Real coordinates from a 2014 Pelosi filing (20002236.pdf): same legacy layout as
# above, but the transaction/notification dates have a single-digit day with no
# leading zero ("11/5/2014" not "11/05/2014").
UNPADDED_DATE_HEADER_WORDS = [
    _word("owner", 63.0, 96.7, 295.4),
    _word("asset", 102.0, 128.8, 295.4),
    _word("transaction", 269.2, 329.7, 295.4),
    _word("Date", 334.5, 357.9, 295.4),
    _word("notification", 387.0, 447.1, 295.4),
    _word("amount", 452.2, 492.9, 295.4),
    _word("type", 269.2, 293.4, 306.6),
    _word("Date", 387.0, 410.4, 306.6),
]
UNPADDED_DATE_DATA_ROW = [
    _word("Hertz", 102.0, 124.5, 329.0),
    _word("global", 126.6, 152.7, 329.0),
    _word("Holdings,", 154.9, 193.7, 329.0),
    _word("Inc", 195.9, 208.8, 329.0),
    _word("(HTZ)", 210.9, 236.1, 329.0),
    _word("P", 269.2, 274.7, 329.0),
    _word("11/5/2014", 334.5, 374.9, 329.0),
    _word("11/5/2014", 387.0, 427.4, 329.0),
    _word("$15,001", 452.2, 483.7, 329.0),
    _word("-", 485.9, 489.2, 329.0),
    _word("$50,000", 491.4, 526.2, 329.0),
]


def test_extract_rows_unpadded_single_digit_day_not_dropped():
    """Regression: DATE_RE required a zero-padded day/month, so "11/5/2014" (no leading
    zero) silently failed _is_row_start and the row vanished with no trace."""
    pdf = FakePDF([FakePage(UNPADDED_DATE_HEADER_WORDS + UNPADDED_DATE_DATA_ROW)])
    columns = _derive_columns(pdf)
    rows = _extract_raw_rows(pdf, columns)
    assert len(rows) == 1
    assert rows[0]["date"] == "11/5/2014"
    assert rows[0]["notif_date"] == "11/5/2014"


def test_derive_columns_from_modern_header():
    pdf = FakePDF([FakePage(MODERN_HEADER_WORDS + MODERN_DATA_ROW)])
    columns = _derive_columns(pdf)
    assert columns is not None
    names = [c[0] for c in columns]
    assert names == ["owner", "asset", "type", "date", "notif_date", "amount"]


def test_extract_rows_modern_layout_long_asset_name_not_clipped():
    """Regression: a midpoint-based boundary previously clipped this exact row's
    long asset name ("Adobe Inc. - Common Stock (ADBE)") into the type bucket,
    producing zero rows for a filing independently verified against the raw PDF."""
    pdf = FakePDF([FakePage(MODERN_HEADER_WORDS + MODERN_DATA_ROW)])
    columns = _derive_columns(pdf)
    rows = _extract_raw_rows(pdf, columns)
    assert len(rows) == 1
    row = rows[0]
    assert row["owner_raw"] == "JT"
    assert "Adobe" in row["asset_raw"] and "(ADBE)" in row["asset_raw"]
    assert row["type_raw"] == "S"
    assert row["date"] == "06/13/2024"
    assert row["notif_date"] == "07/08/2024"


def test_extract_rows_shifted_columns_layout():
    pdf = FakePDF([FakePage(SHIFTED_HEADER_WORDS + SHIFTED_DATA_ROW)])
    columns = _derive_columns(pdf)
    assert columns is not None
    rows = _extract_raw_rows(pdf, columns)
    assert len(rows) == 1
    assert rows[0]["type_raw"] == "S"
    assert rows[0]["date"] == "12/28/2021"
    assert rows[0]["notif_date"] == "01/04/2022"


def test_extract_rows_legacy_case_corrupted_layout():
    pdf = FakePDF([FakePage(LEGACY_HEADER_WORDS + LEGACY_DATA_ROW)])
    columns = _derive_columns(pdf)
    assert columns is not None
    rows = _extract_raw_rows(pdf, columns)
    assert len(rows) == 1
    row = rows[0]
    assert row["owner_raw"] == "sP"
    assert row["type_raw"] == "s"
    assert row["date"] == "12/21/2017"


def test_ticker_and_asset_type_with_bracket():
    assert _extract_ticker_and_asset_type("Adobe Inc. - Common Stock (ADBE) [ST]") == ("ADBE", "ST")


def test_ticker_only_no_bracket_legacy_format():
    assert _extract_ticker_and_asset_type("apple Inc. (aaPl)") == ("AAPL", None)


def test_ticker_with_share_class_suffix_and_bracket():
    """Regression: the ticker regexes only accepted bare letters, so a share-class suffix
    like "BRK.B" fell through to the no-ticker asset-type-only pattern."""
    assert _extract_ticker_and_asset_type("berkshire Hathaway Inc. New (bRK.b) [ST]") == ("BRK.B", "ST")


def test_ticker_with_share_class_suffix_no_bracket():
    assert _extract_ticker_and_asset_type("Berkshire Hathaway Inc. New (BRK.B)") == ("BRK.B", None)


def test_asset_type_only_no_ticker():
    """Regression: real Pelosi filings disclose LLC/fund holdings with no ticker at
    all, just a bare bracket type code (e.g. "REOF XX, LLC [AB]") - previously
    neither pattern matched, so asset_type was silently left None for these."""
    assert _extract_ticker_and_asset_type("REOF XX, LLC [AB]") == (None, "AB")
    assert _extract_ticker_and_asset_type("Roblox Corporation [OT]") == (None, "OT")


def test_ticker_before_exchange_name_in_parens():
    """Regression: a Greene filing wrote "FIG (NYSE) [OT]" (ticker before, exchange name in
    parens) instead of the usual "Name (TICKER)" - the exchange name was extracted as the
    ticker instead of the real one."""
    assert _extract_ticker_and_asset_type("FIG (NYSE) [OT]") == ("FIG", "OT")
    assert _extract_ticker_and_asset_type("FIG (NYSE)") == ("FIG", None)


def test_extract_filing_status_new_and_amended():
    assert _extract_filing_status(["FIlINg sTaTus: New", "DEsCRIPTION: Purchase of 50 Options"]) == "new"
    assert _extract_filing_status(["FILINg STATUS: Amended", "DESCRIPTION: Sold 10 shares"]) == "amended"


def test_extract_filing_status_missing_returns_none():
    assert _extract_filing_status(["DESCRIPTION: Purchased 10,000 shares."]) is None


def test_extract_filing_status_abbreviated_modern_format():
    """Regression: modern filings abbreviate "Filing Status" to "F S:", which wasn't
    recognized - only the spelled-out label was."""
    assert _extract_filing_status(["F S: New", "S O: Morgan Stanley - Select"]) == "new"


def test_to_iso_date_zero_pads_unpadded_month_and_day():
    assert _to_iso_date("11/5/2014") == "2014-11-05"
    assert _to_iso_date("1/25/2014") == "2014-01-25"


def test_derive_columns_returns_none_for_genuine_scan():
    pdf = FakePDF([FakePage([])])
    assert _derive_columns(pdf) is None


def test_parse_filing_raises_when_no_header_found(monkeypatch):
    import src.parse.house_ptr_parser as module

    monkeypatch.setattr(module.pdfplumber, "open", lambda path: FakePDF([FakePage([])]))
    with pytest.raises(UnparseableFormError):
        parse_filing("does-not-matter.pdf")
