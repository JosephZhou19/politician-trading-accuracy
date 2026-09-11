"""Parses a House Clerk Periodic Transaction Report PDF into structured trade records.

Column x-coordinates are derived per-document from the header row's own label words (see
_derive_columns), not hardcoded, since the PDF template's column positions and label casing
have shifted across template eras and even between filers in the same year.
"""

import re

import pdfplumber

DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
TYPE_TOKENS = {"P", "S", "E"}
STATUS_RE = re.compile(r"Status:\s*(\S+)", re.IGNORECASE)
SIGNED_DATE_RE = re.compile(r"Digitally Signed:.*?,\s*(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)

# Metadata line prefixes across template eras: modern abbreviated codes ("F S:", "S O:",
# "C:") and older spelled-out labels with erratic small-caps casing ("FIlINg sTaTus:",
# "DEsCRIPTIoN:"). Matched by content (whitespace/case stripped), not exact casing.
_METADATA_PREFIXES = {"FS", "SO", "C", "D", "FILINGSTATUS", "SUBHOLDINGOF", "DESCRIPTION", "COMMENT"}
# Comment-worthy prefixes specifically (vs. e.g. "Filing Status: New", which isn't).
_COMMENT_PREFIXES = {"C", "DESCRIPTION", "D"}

# Ticker + modern bracket-style asset-type code, e.g. "(ADBE) [ST]". The optional ".X" is the
# share-class suffix (BRK.B, BF.B, ...).
TICKER_WITH_TYPE_RE = re.compile(r"\(([A-Za-z]{1,6}(?:\.[A-Za-z])?)\)\s*\[([A-Za-z]{2})\]\s*$")
# Older filings have no bracket suffix at all, just "(aaPl)".
TICKER_ONLY_RE = re.compile(r"\(([A-Za-z]{1,6}(?:\.[A-Za-z])?)\)\s*$")
# Non-equity assets (LLCs, funds, LP units) have no ticker, just a bare type code, e.g.
# "REOF XX, LLC [AB]".
ASSET_TYPE_ONLY_RE = re.compile(r"\[([A-Za-z]{2})\]\s*$")

# A filer occasionally writes "TICKER (EXCHANGE)" instead of the usual "Name (TICKER)" - real
# example: "FIG (NYSE) [OT]", disclosed right after Figma's 2025 IPO. None of these are real,
# current ticker symbols, so treating the parenthetical as an exchange name is safe.
_EXCHANGE_NAMES = {"NYSE", "NASDAQ", "OTC", "AMEX"}


class UnparseableFormError(Exception):
    """Raised when no column header can be located in the PDF - either a genuine
    scanned/legacy paper form with no text layer, or some other layout this parser
    doesn't yet recognize. See _derive_columns."""


def _clean(text):
    # small-caps glyphs in section labels (Filing Status, Description, ...) decode to
    # NUL bytes for every letter but the first
    return text.replace("\x00", "")


def _bucket_for(x0, columns):
    for name, lo, hi in columns:
        if lo <= x0 < hi:
            return name
    return None


def _derive_columns(pdf):
    """Locate the header row's label words (case-insensitive) on an early page and return
    column (name, lo, hi) ranges built from their x0 positions. Returns None if no page's
    header can be located (a genuine scan, or an unrecognized layout)."""
    for page in pdf.pages[:3]:
        words = page.extract_words()
        found = {}
        date_candidates = []
        for w in words:
            if DATE_RE.match(w["text"]) or w["text"].startswith("$"):
                break  # reached real data; the header (if any) is behind us
            label = w["text"].strip().rstrip(".:").lower()
            if label == "date":
                date_candidates.append(w["x0"])
            elif label in ("owner", "asset", "type", "notification", "amount", "cap") and label not in found:
                found[label] = w["x0"]

        if not all(k in found for k in ("owner", "asset", "type", "notification", "amount")):
            continue

        # The real "Cap. Gains" column always sits right of "amount" - a match left of it is
        # "cap" appearing inside a data row's own asset name (e.g. a municipal bond), not a header.
        if "cap" in found and found["cap"] <= found["amount"]:
            del found["cap"]

        # "Date" appears twice - once for the transaction-date column (between "type"
        # and "notification"), once redundantly stacked under "notification" itself
        # for "Notification Date". Only the first one is a new column boundary.
        txn_dates = [d for d in date_candidates if found["type"] < d < found["notification"]]
        if not txn_dates:
            continue
        found["date"] = txn_dates[0]

        ordered = sorted(
            ((name, found[name]) for name in ("owner", "asset", "type", "date", "notification", "amount")),
            key=lambda kv: kv[1],
        )
        # A column's data lines up with its header's x0 almost exactly (~0.1pt drift), so
        # each boundary is the *next* column's header x0 minus a small epsilon - not the
        # midpoint, which sits too far left and clips long asset-name text.
        EPSILON = 2
        xs = [x0 for _, x0 in ordered]
        boundaries = [xs[0] - EPSILON - 10]
        boundaries += [x - EPSILON for x in xs[1:]]
        # The modern layout has a further "Cap. Gains > $200?" checkbox column past amount;
        # without a real right edge, amount's catch-all range swallows its stray fragments.
        # Bound amount at "Cap."'s own x0 when present; older layouts keep the wide fallback.
        boundaries.append(found["cap"] - EPSILON if "cap" in found else xs[-1] + 300)
        columns = []
        for i, (name, _) in enumerate(ordered):
            label = "notif_date" if name == "notification" else name
            columns.append((label, boundaries[i], boundaries[i + 1]))
        return columns
    return None


def _group_lines(page):
    words = page.extract_words()
    lines = {}
    for w in words:
        top = round(w["top"])
        key = next((k for k in lines if abs(k - top) <= 2), top)
        lines.setdefault(key, []).append(w)
    return [lines[k] for k in sorted(lines)]


def _line_to_buckets(line_words, columns):
    buckets = {name: [] for name, _, _ in columns}
    for w in sorted(line_words, key=lambda w: w["x0"]):
        b = _bucket_for(w["x0"], columns)
        if b:
            buckets[b].append(_clean(w["text"]))
    return {k: " ".join(v).strip() for k, v in buckets.items()}


def _is_row_start(buckets):
    type_tok = buckets["type"].split()[0].upper() if buckets["type"] else ""
    return (
        DATE_RE.match(buckets["date"] or "")
        and DATE_RE.match(buckets["notif_date"] or "")
        and type_tok in TYPE_TOKENS
    )


def _looks_like_header(buckets):
    """A repeated header row (e.g. on a later page) buckets its own label words into
    the columns they define - checked by content, not exact phrasing, since the
    surrounding text varies by template era."""
    return (
        buckets.get("asset", "").strip().upper() == "ASSET"
        or buckets.get("type", "").strip().upper() in ("TYPE", "TRANSACTION")
        or buckets.get("notif_date", "").strip().upper() == "NOTIFICATION"
        or buckets.get("amount", "").strip().upper() == "AMOUNT"
    )


def _metadata_prefix(text):
    prefix, sep, _rest = text.partition(":")
    if not sep:
        return None
    normalized = re.sub(r"\s+", "", prefix).upper()
    return normalized if normalized in _METADATA_PREFIXES else None


def _extract_raw_rows(pdf, columns):
    all_lines = []
    for page in pdf.pages:
        stop = False
        for line_words in _group_lines(page):
            buckets = _line_to_buckets(line_words, columns)
            if _looks_like_header(buckets):
                continue
            text = " ".join(w["text"] for w in line_words)
            if text.upper().startswith("* FOR THE COMPLETE"):
                stop = True
                break
            all_lines.append(buckets)
        if stop:
            break

    rows = []
    current = None
    for buckets in all_lines:
        if _is_row_start(buckets):
            if current:
                rows.append(current)
            current = {
                "owner_raw": buckets["owner"],
                "asset_raw": buckets["asset"],
                "type_raw": buckets["type"],
                "date": buckets["date"],
                "notif_date": buckets["notif_date"],
                "amount_raw": buckets["amount"],
                "meta_lines": [],
                "seen_metadata": False,
            }
        elif current is not None:
            asset_text = buckets["asset"]
            if asset_text and _metadata_prefix(asset_text):
                current["seen_metadata"] = True
                current["meta_lines"].append(asset_text)
            elif asset_text and not current["seen_metadata"]:
                current["asset_raw"] += " " + asset_text
                if buckets["amount"]:
                    current["amount_raw"] += " " + buckets["amount"]
            elif asset_text:
                current["meta_lines"][-1] += " " + asset_text
            elif buckets["amount"]:
                current["amount_raw"] += " " + buckets["amount"]
    if current:
        rows.append(current)
    return rows


def _canonicalize_transaction_type(type_raw):
    normalized = type_raw.upper()
    if normalized.startswith("P"):
        return "purchase"
    if normalized.startswith("S (PARTIAL)"):
        return "sale_partial"
    if normalized.startswith("S"):
        return "sale_full"
    if normalized.startswith("E"):
        return "exchange"
    return type_raw


def _canonicalize_owner(owner_raw):
    return {"SP": "spouse", "JT": "joint", "DC": "dependent_child", "": "self"}.get(
        owner_raw.upper(), owner_raw
    )


def canonicalize_filer_status(status_raw):
    s = (status_raw or "").strip().lower()
    if "former" in s:
        return "former_member"
    return s


def _real_ticker(asset_raw, match_start, candidate):
    if candidate not in _EXCHANGE_NAMES:
        return candidate
    preceding = asset_raw[:match_start].split()
    if preceding and re.fullmatch(r"[A-Za-z]{1,6}", preceding[-1]):
        return preceding[-1].upper()
    return candidate


def _extract_ticker_and_asset_type(asset_raw):
    m = TICKER_WITH_TYPE_RE.search(asset_raw)
    if m:
        return _real_ticker(asset_raw, m.start(), m.group(1).upper()), m.group(2).upper()
    m = ASSET_TYPE_ONLY_RE.search(asset_raw)
    if m:
        return None, m.group(1).upper()
    m = TICKER_ONLY_RE.search(asset_raw)
    if m:
        return _real_ticker(asset_raw, m.start(), m.group(1).upper()), None
    return None, None


def _parse_amount(amount_raw):
    # Also matches a bare-cents value with no leading zero, e.g. "$.01".
    parts = re.findall(r"\$(?:[\d,]+(?:\.\d+)?|\.\d+)", amount_raw)
    if not parts:
        return None, None
    low = int(float(parts[0].replace("$", "").replace(",", "")))
    high = int(float(parts[1].replace("$", "").replace(",", ""))) if len(parts) > 1 else low
    return low, high


def _extract_comment(meta_lines):
    for line in meta_lines:
        prefix = _metadata_prefix(line)
        if prefix in _COMMENT_PREFIXES:
            return line.partition(":")[2].strip()
    return None


def _extract_filing_status(meta_lines):
    """"New" or "Amended", read from the row's own "Filing Status" field. Tracked per
    transaction, not per document, so a single PTR can mix corrected rows with new ones
    (see reconcile_amendments.py)."""
    for line in meta_lines:
        if _metadata_prefix(line) in ("FILINGSTATUS", "FS"):
            value = line.partition(":")[2].strip()
            return value.split()[0].lower() if value else None
    return None


def _to_iso_date(mmddyyyy):
    # Real filings write single-digit month/day without a leading zero (e.g. "11/5/2014")
    month, day, year = mmddyyyy.split("/")
    return f"{year}-{int(month):02d}-{int(day):02d}"


def parse_filing(pdf_path):
    """Parse a House PTR PDF into filer status, filing date, and canonicalized trades.
    Raises UnparseableFormError if no column header can be located (see
    _derive_columns) - a genuine scanned/legacy paper form, or an unrecognized layout."""
    with pdfplumber.open(pdf_path) as pdf:
        columns = _derive_columns(pdf)
        if columns is None:
            raise UnparseableFormError(f"could not locate a column header in {pdf_path}")
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        raw_rows = _extract_raw_rows(pdf, columns)

    status_match = STATUS_RE.search(full_text)
    date_match = SIGNED_DATE_RE.search(full_text)

    trades = []
    for i, row in enumerate(raw_rows, start=1):
        ticker, asset_type = _extract_ticker_and_asset_type(row["asset_raw"])
        amount_low, amount_high = _parse_amount(row["amount_raw"])
        trades.append({
            # House's PDF "ID" column is always blank in practice, unlike Senate's - parse
            # order is the closest thing to a row identifier this source gives us
            "source_row_number": i,
            "ticker": ticker,
            "asset_name": row["asset_raw"],
            "asset_type": asset_type,
            "transaction_type": _canonicalize_transaction_type(row["type_raw"]),
            "transaction_date": _to_iso_date(row["date"]),
            "notification_date": _to_iso_date(row["notif_date"]),
            "amount_low": amount_low,
            "amount_high": amount_high,
            "owner": _canonicalize_owner(row["owner_raw"]),
            "comment": _extract_comment(row["meta_lines"]),
            "raw_row_text": " | ".join(row["meta_lines"]) or None,
            "filing_status": _extract_filing_status(row["meta_lines"]),
        })

    return {
        "filer_status_raw": status_match.group(1) if status_match else None,
        "filing_date": _to_iso_date(date_match.group(1)) if date_match else None,
        "trades": trades,
    }
