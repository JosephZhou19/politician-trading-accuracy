"""Tests for src/parse/senate_ptr_parser.py.

Real bug found via external validation against Quiver Quantitative: bond/note rows
carry a nested <div class="text-muted"> with Rate/Coupon and Maturity info inside the
same cell as the asset name, with no separator in the markup - plain get_text() on that
cell was gluing it onto the name (e.g. "JPMorgan Chase & Co. (NYSE)Rate/Coupon:6.75Matures:02/01/24").
Separately, the source sometimes links a bond's ticker cell to its issuer's own
common-stock ticker (a JPMorgan Chase bond linked to "JPM"), which would corrupt
Phase 2/3 price matching if treated as the equity - so ticker is only trusted for
actual stock trades.
"""
from src.parse.senate_ptr_parser import parse_report_html

TABLE_TEMPLATE = """
<table><tbody>
{rows}
</tbody></table>
"""

BOND_ROW = """
<tr>
    <td>18</td>
    <td>03/13/2014</td>
    <td>Joint</td>
    <td><a href="https://finance.yahoo.com/quote/JPM" target="_blank">JPM</a></td>
    <td>JPMorgan Chase &amp; Co. (NYSE)
        <div class="text-muted"><em>Rate/Coupon:</em> 6.75<br/><em>Matures:</em> 02/01/24</div>
    </td>
    <td>Corporate Bond</td>
    <td>Purchase</td>
    <td>$15,001 - $50,000</td>
    <td>--</td>
</tr>
"""

MUNI_ROW_NO_TICKER = """
<tr>
    <td>1</td>
    <td>09/04/2025</td>
    <td>Self</td>
    <td></td>
    <td>RI INFRASTRUCTURE BK SAFE DRINKIN
        <div class="text-muted"><em>Rate/Coupon:</em> 3.0%<br/><em>Matures:</em> 10/01/2032</div>
    </td>
    <td>Municipal Security</td>
    <td>Sale (Full)</td>
    <td>$15,001 - $50,000</td>
    <td>--</td>
</tr>
"""

GLUED_TICKER_ROW = """
<tr>
    <td>5</td>
    <td>04/12/2016</td>
    <td>Child</td>
    <td>--</td>
    <td>STT-State Street Corporation</td>
    <td>Stock</td>
    <td>Sale (Partial)</td>
    <td>$15,001 - $50,000</td>
    <td>--</td>
</tr>
"""

GLUED_TICKER_WITH_CLASS_SUFFIX_ROW = """
<tr>
    <td>1</td>
    <td>07/24/2025</td>
    <td>Joint</td>
    <td>--</td>
    <td>BRK-B - Berkshire Hathaway Inc Class B</td>
    <td>Stock</td>
    <td>Purchase</td>
    <td>$1,001 - $15,000</td>
    <td>--</td>
</tr>
"""

BARE_TICKER_ROW = """
<tr>
    <td>11</td>
    <td>10/23/2015</td>
    <td>Child</td>
    <td>--</td>
    <td>DIS</td>
    <td>Stock</td>
    <td>Purchase</td>
    <td>$1,001 - $15,000</td>
    <td>--</td>
</tr>
"""

STOCK_ROW = """
<tr>
    <td>19</td>
    <td>09/06/2016</td>
    <td>Self</td>
    <td><a href="https://finance.yahoo.com/quote/GILD" target="_blank">GILD</a></td>
    <td>Gilead Sciences, Inc.</td>
    <td>Stock</td>
    <td>Sale (Partial)</td>
    <td>$1,001 - $15,000</td>
    <td>--</td>
</tr>
"""


def test_bond_ticker_not_conflated_with_issuer_stock():
    trades = parse_report_html(TABLE_TEMPLATE.format(rows=BOND_ROW))
    assert len(trades) == 1
    trade = trades[0]
    assert trade["asset_type"] == "Corporate Bond"
    assert trade["ticker"] is None
    assert trade["asset_name"] == "JPMorgan Chase & Co. (NYSE)"
    assert "Rate/Coupon: 6.75" in trade["raw_row_text"]
    assert "Matures: 02/01/24" in trade["raw_row_text"]
    assert "source ticker link: JPM" in trade["raw_row_text"]


def test_bond_detail_never_leaks_into_asset_name():
    trades = parse_report_html(TABLE_TEMPLATE.format(rows=MUNI_ROW_NO_TICKER))
    trade = trades[0]
    assert trade["asset_name"] == "RI INFRASTRUCTURE BK SAFE DRINKIN"
    assert trade["ticker"] is None
    assert "Rate/Coupon: 3.0%" in trade["raw_row_text"]
    assert "source ticker link" not in trade["raw_row_text"]


def test_stock_ticker_still_parsed_normally():
    trades = parse_report_html(TABLE_TEMPLATE.format(rows=STOCK_ROW))
    trade = trades[0]
    assert trade["asset_type"] == "Stock"
    assert trade["ticker"] == "GILD"
    assert trade["asset_name"] == "Gilead Sciences, Inc."
    assert trade["raw_row_text"] is None


def test_glued_ticker_recovered_when_ticker_link_missing():
    """Regression: found via Quiver comparison - the ticker-link cell is sometimes empty
    ("--") for a real stock trade, with the ticker glued onto the asset name instead."""
    trades = parse_report_html(TABLE_TEMPLATE.format(rows=GLUED_TICKER_ROW))
    trade = trades[0]
    assert trade["ticker"] == "STT"
    assert trade["asset_name"] == "State Street Corporation"


def test_glued_ticker_with_share_class_suffix_recovered():
    trades = parse_report_html(TABLE_TEMPLATE.format(rows=GLUED_TICKER_WITH_CLASS_SUFFIX_ROW))
    trade = trades[0]
    assert trade["ticker"] == "BRK-B"
    assert trade["asset_name"] == "Berkshire Hathaway Inc Class B"


def test_bare_ticker_with_no_company_name_recovered():
    """Regression: some filers give no company name at all, just the bare ticker as the
    whole asset name ("DIS"), with no ticker link either - confirmed against the raw
    source, not a truncated name."""
    trades = parse_report_html(TABLE_TEMPLATE.format(rows=BARE_TICKER_ROW))
    trade = trades[0]
    assert trade["ticker"] == "DIS"
    assert trade["asset_name"] == "DIS"


def test_mixed_bond_and_stock_rows_in_same_filing():
    trades = parse_report_html(TABLE_TEMPLATE.format(rows=BOND_ROW + STOCK_ROW))
    assert len(trades) == 2
    bond, stock = trades
    assert bond["ticker"] is None
    assert stock["ticker"] == "GILD"
