"""Parses a House Clerk Periodic Transaction Report PDF into structured trade records.

Column positions below are derived from the standard House PTR form layout and are
fragile to a template change on the House Clerk's end - if House redesigns the form,
these x-coordinates and the small-caps null-byte quirk below would need re-verifying.
"""

import re

import pdfplumber

COLUMNS = [
    ("owner", 55, 100),
    ("asset", 100, 245),
    ("type", 245, 322),
    ("date", 322, 377),
    ("notif_date", 377, 442),
    ("amount", 442, 522),
]

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
TYPE_TOKENS = {"P", "S", "E"}
METADATA_LINE_RE = re.compile(r"^[A-Z](\s*[A-Z])?\s*:")
COMMENT_LINE_RE = re.compile(r"^C\s*:\s*(.+)$")
TICKER_RE = re.compile(r"\(([A-Z]{1,6})\)\s*\[[A-Z]{2}\]\s*$")
ASSET_TYPE_RE = re.compile(r"\[([A-Z]{2})\]\s*$")
STATUS_RE = re.compile(r"Status:\s*(\S+)")
SIGNED_DATE_RE = re.compile(r"Digitally Signed:.*?,\s*(\d{2}/\d{2}/\d{4})")


def _clean(text):
    # small-caps glyphs in section labels (Filing Status, Description, ...) decode to
    # NUL bytes for every letter but the first
    return text.replace("\x00", "")


def _bucket_for(x0):
    for name, lo, hi in COLUMNS:
        if lo <= x0 < hi:
            return name
    return None


def _group_lines(page):
    words = page.extract_words()
    lines = {}
    for w in words:
        top = round(w["top"])
        key = next((k for k in lines if abs(k - top) <= 2), top)
        lines.setdefault(key, []).append(w)
    return [lines[k] for k in sorted(lines)]


def _line_to_buckets(line_words):
    buckets = {name: [] for name, _, _ in COLUMNS}
    for w in sorted(line_words, key=lambda w: w["x0"]):
        b = _bucket_for(w["x0"])
        if b:
            buckets[b].append(_clean(w["text"]))
    return {k: " ".join(v).strip() for k, v in buckets.items()}


def _is_row_start(buckets):
    type_tok = buckets["type"].split()[0] if buckets["type"] else ""
    return (
        DATE_RE.match(buckets["date"] or "")
        and DATE_RE.match(buckets["notif_date"] or "")
        and type_tok in TYPE_TOKENS
    )


def _extract_raw_rows(pdf):
    all_lines = []
    for page in pdf.pages:
        stop = False
        for line_words in _group_lines(page):
            text = " ".join(w["text"] for w in line_words)
            if text.startswith("* For the complete"):
                stop = True
                break
            if any(text.startswith(h) for h in ("ID Owner", "ID", "Cap.", "Type Date")):
                continue
            all_lines.append(_line_to_buckets(line_words))
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
            if asset_text and METADATA_LINE_RE.match(asset_text):
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
    if type_raw.startswith("P"):
        return "purchase"
    if type_raw.startswith("S (partial)"):
        return "sale_partial"
    if type_raw.startswith("S"):
        return "sale_full"
    if type_raw.startswith("E"):
        return "exchange"
    return type_raw


def _canonicalize_owner(owner_raw):
    return {"SP": "spouse", "JT": "joint", "DC": "dependent_child", "": "self"}.get(
        owner_raw, owner_raw
    )


def canonicalize_filer_status(status_raw):
    s = (status_raw or "").strip().lower()
    if "former" in s:
        return "former_member"
    return s


def _extract_ticker_and_asset_type(asset_raw):
    ticker_match = TICKER_RE.search(asset_raw)
    type_match = ASSET_TYPE_RE.search(asset_raw)
    return (
        ticker_match.group(1) if ticker_match else None,
        type_match.group(1) if type_match else None,
    )


def _parse_amount(amount_raw):
    parts = re.findall(r"\$[\d,]+(?:\.\d+)?", amount_raw)
    if not parts:
        return None, None
    low = int(float(parts[0].replace("$", "").replace(",", "")))
    high = int(float(parts[1].replace("$", "").replace(",", ""))) if len(parts) > 1 else low
    return low, high


def _extract_comment(meta_lines):
    for line in meta_lines:
        m = COMMENT_LINE_RE.match(line)
        if m:
            return m.group(1).strip()
    return None


def _to_iso_date(mmddyyyy):
    month, day, year = mmddyyyy.split("/")
    return f"{year}-{month}-{day}"


def parse_filing(pdf_path):
    """Parse a House PTR PDF into filer status, filing date, and canonicalized trades."""
    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        raw_rows = _extract_raw_rows(pdf)

    status_match = STATUS_RE.search(full_text)
    date_match = SIGNED_DATE_RE.search(full_text)

    trades = []
    for row in raw_rows:
        ticker, asset_type = _extract_ticker_and_asset_type(row["asset_raw"])
        amount_low, amount_high = _parse_amount(row["amount_raw"])
        trades.append({
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
        })

    return {
        "filer_status_raw": status_match.group(1) if status_match else None,
        "filing_date": _to_iso_date(date_match.group(1)) if date_match else None,
        "trades": trades,
    }
