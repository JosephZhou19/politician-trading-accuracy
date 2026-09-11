"""Sanity checks run against every filing's trades right after parsing, to catch parser bugs
(bad column boundaries, misread dates/amounts) rather than genuine content ambiguity.

A filing that trips a check is not blocked - its trades are inserted as parsed, and the
filing is flagged via reconciliation_note, the same channel amendment reconciliation uses.
`SELECT * FROM filings WHERE reconciliation_note IS NOT NULL` is one review queue for both.
"""
from datetime import date

MIN_PLAUSIBLE_YEAR = 2000
# STOCK Act requires disclosure within 45 days; a gap this large is more likely a misread
# date than a genuinely multi-year-late disclosure.
MAX_NOTIFICATION_LAG_DAYS = 730
# 2-3 identical rows are common and legitimate (e.g. same stock bought same day for two
# dependent children). Confirmed against the real backfill: of 2,395 repeated-key groups,
# 2,137 occur exactly twice - only flag counts clearly beyond that.
MAX_PLAUSIBLE_REPEATS = 4


def _parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def validate_trades(trades):
    """Returns a list of human-readable issue strings describing anything that looks like a
    parsing error; empty if nothing looks wrong. Never raises - a broken check shouldn't be
    able to take down an ingest run that would otherwise succeed."""
    issues = []
    key_counts = {}

    for i, trade in enumerate(trades, start=1):
        label = f"row {i}"

        amount_low = trade.get("amount_low")
        amount_high = trade.get("amount_high")
        if amount_low is not None and amount_high is not None and amount_low > amount_high:
            issues.append(f"{label}: amount_low ({amount_low}) > amount_high ({amount_high})")
        if amount_low is not None and amount_low < 0:
            issues.append(f"{label}: negative amount_low ({amount_low})")

        asset_name = (trade.get("asset_name") or "").strip()
        if len(asset_name) < 2 or asset_name.isdigit():
            issues.append(f"{label}: suspicious asset_name {asset_name!r}")

        txn_date = _parse_date(trade.get("transaction_date"))
        notif_date = _parse_date(trade.get("notification_date"))
        today_plus_one_year = date.today().year + 1
        for field_name, parsed in (("transaction_date", txn_date), ("notification_date", notif_date)):
            if parsed is not None and not (MIN_PLAUSIBLE_YEAR <= parsed.year <= today_plus_one_year):
                issues.append(f"{label}: {field_name} {parsed.isoformat()} outside plausible range")

        if txn_date is not None and notif_date is not None:
            lag = (notif_date - txn_date).days
            if lag < 0:
                issues.append(f"{label}: notification_date ({notif_date}) predates transaction_date ({txn_date})")
            elif lag > MAX_NOTIFICATION_LAG_DAYS:
                issues.append(f"{label}: notification_date is {lag} days after transaction_date")

        dup_key = (
            trade.get("transaction_date"), asset_name.lower(), trade.get("transaction_type"),
            amount_low, amount_high, trade.get("owner"),
        )
        key_counts[dup_key] = key_counts.get(dup_key, 0) + 1

    for dup_key, count in key_counts.items():
        if count > MAX_PLAUSIBLE_REPEATS:
            _, name, *_ = dup_key
            issues.append(f"{count} identical rows (same date/asset/type/amount/owner) for {name!r}")

    return issues
