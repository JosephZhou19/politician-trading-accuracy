"""Maps a GICS sector to its SPDR Select Sector ETF - the benchmark used for alpha vs. that
sector, the same way benchmark_prices uses SPY for alpha vs. the whole market.

Keys are yfinance's own info['sector'] strings, confirmed live against representative
tickers rather than assumed (yfinance's taxonomy differs slightly from GICS naming, e.g.
"Consumer Cyclical"/"Consumer Defensive" instead of "Consumer Discretionary"/"Consumer
Staples", "Financial Services" and "Basic Materials" instead of "Financials"/"Materials") -
whatever eventually maps a ticker to a sector can join straight into
sector_benchmark_prices.sector with no translation, using this same string. NOTE: nothing
populates a per-ticker sector anywhere in this codebase yet (see sector_benchmark_prices'
own comment in schema.sql) - this mapping and that table both currently have no consumer.
"""

SECTOR_ETFS = {
    "Energy": "XLE",
    "Financial Services": "XLF",
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Industrials": "XLI",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Utilities": "XLU",
    "Basic Materials": "XLB",
    "Communication Services": "XLC",
    "Real Estate": "XLRE",
}
