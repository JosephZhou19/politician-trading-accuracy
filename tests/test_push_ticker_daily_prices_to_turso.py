"""Tests for scripts/push_ticker_daily_prices_to_turso.py."""
from scripts.push_ticker_daily_prices_to_turso import push_ticker_daily_prices
from src.db import models


def test_push_copies_every_row_across_tickers(conn, tmp_path, monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    local = models.connect_local(tmp_path / "local.db")
    models.set_ticker_daily_prices(local, "AAPL", [("2020-01-02", 100.0), ("2020-01-03", 101.0)])
    models.set_ticker_daily_prices(local, "MSFT", [("2020-01-02", 200.0)])

    count = push_ticker_daily_prices(local, conn, batch_size=2)

    assert count == 3
    rows = conn.execute("SELECT ticker, date, price FROM ticker_daily_prices ORDER BY ticker, date").fetchall()
    assert [(r["ticker"], r["date"], r["price"]) for r in rows] == [
        ("AAPL", "2020-01-02", 100.0), ("AAPL", "2020-01-03", 101.0), ("MSFT", "2020-01-02", 200.0),
    ]


def test_push_is_safe_to_rerun(conn, tmp_path, monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    local = models.connect_local(tmp_path / "local.db")
    models.set_ticker_daily_prices(local, "AAPL", [("2020-01-02", 100.0)])

    push_ticker_daily_prices(local, conn)
    push_ticker_daily_prices(local, conn)  # re-run must not duplicate or error

    rows = conn.execute("SELECT date, price FROM ticker_daily_prices WHERE ticker = 'AAPL'").fetchall()
    assert [(r["date"], r["price"]) for r in rows] == [("2020-01-02", 100.0)]


def test_push_of_empty_local_table_is_a_no_op(conn, tmp_path, monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    local = models.connect_local(tmp_path / "local.db")

    count = push_ticker_daily_prices(local, conn)

    assert count == 0
    assert conn.execute("SELECT COUNT(*) FROM ticker_daily_prices").fetchone()[0] == 0
