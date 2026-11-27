"""Persistent DuckDB schema and static baseline CSV loader for JEVDB stock research."""

from __future__ import annotations

import csv
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import duckdb
import jevdb

from stock_research.config import get_settings

_CONNECT_LOCK = threading.RLock()
_SCHEMA_LOCK = threading.RLock()
_REQUIRED_TABLES = frozenset(
    {
        "assets",
        "market_bars",
        "sector_snapshots",
        "sector_constituents",
        "fundamentals",
        "edgar_filings",
        "news_events",
        "earnings_calendar",
        "ingestion_runs",
        "static_trading_rules",
        "semantic_decisions_cache",
    }
)

SCHEMA_DDL = [
    """
    CREATE TABLE IF NOT EXISTS assets (
        symbol VARCHAR,
        market VARCHAR,
        name VARCHAR,
        sector VARCHAR,
        industry VARCHAR,
        updated_at TIMESTAMP,
        PRIMARY KEY (symbol, market)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_bars (
        symbol VARCHAR,
        market VARCHAR,
        trade_date DATE,
        open DOUBLE,
        high DOUBLE,
        low DOUBLE,
        close DOUBLE,
        volume DOUBLE,
        amount DOUBLE,
        source VARCHAR,
        PRIMARY KEY (symbol, market, trade_date, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sector_snapshots (
        market VARCHAR,
        sector_name VARCHAR,
        sector_code VARCHAR,
        trade_date DATE,
        change_pct DOUBLE,
        volume DOUBLE,
        turnover DOUBLE,
        leader_symbol VARCHAR,
        payload_json VARCHAR,
        source VARCHAR,
        PRIMARY KEY (market, sector_name, trade_date, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sector_constituents (
        sector_name VARCHAR,
        symbol VARCHAR,
        name VARCHAR,
        price DOUBLE,
        change_pct DOUBLE,
        pe DOUBLE,
        pb DOUBLE,
        market_cap DOUBLE,
        updated_at TIMESTAMP,
        source VARCHAR,
        PRIMARY KEY (sector_name, symbol, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fundamentals (
        symbol VARCHAR,
        market VARCHAR,
        report_date DATE,
        pe_ttm DOUBLE,
        pb DOUBLE,
        roe DOUBLE,
        revenue DOUBLE,
        net_income DOUBLE,
        debt_ratio DOUBLE,
        metrics_json VARCHAR,
        source VARCHAR,
        updated_at TIMESTAMP,
        PRIMARY KEY (symbol, market, report_date, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edgar_filings (
        cik VARCHAR,
        symbol VARCHAR,
        accession_no VARCHAR PRIMARY KEY,
        form VARCHAR,
        filing_date DATE,
        report_date DATE,
        primary_doc VARCHAR,
        title VARCHAR,
        url VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news_events (
        event_id VARCHAR PRIMARY KEY,
        symbol VARCHAR,
        market VARCHAR,
        sector VARCHAR,
        published_at TIMESTAMP,
        title VARCHAR,
        summary VARCHAR,
        url VARCHAR,
        source VARCHAR,
        sentiment_label VARCHAR,
        keywords_json VARCHAR,
        keys VARCHAR[]
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS earnings_calendar (
        symbol VARCHAR,
        report_date DATE,
        eps_estimate DOUBLE,
        eps_actual DOUBLE,
        revenue_estimate DOUBLE,
        revenue_actual DOUBLE,
        source VARCHAR,
        fetched_at TIMESTAMP,
        PRIMARY KEY (symbol, report_date, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingestion_runs (
        run_id VARCHAR PRIMARY KEY,
        source VARCHAR,
        target VARCHAR,
        status VARCHAR,
        rows_written BIGINT,
        started_at TIMESTAMP,
        finished_at TIMESTAMP,
        error_msg VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS static_trading_rules (
        symbol VARCHAR PRIMARY KEY,
        buy_lot INTEGER,
        sell_lot INTEGER,
        tick_size DOUBLE,
        commission_rate DOUBLE,
        min_commission DOUBLE,
        transfer_fee_rate DOUBLE,
        stamp_duty_rate DOUBLE,
        slippage_rate DOUBLE,
        price_limit_rate DOUBLE,
        max_participation_rate DOUBLE,
        source VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS semantic_decisions_cache (
        cache_key VARCHAR PRIMARY KEY,
        backend VARCHAR,
        model VARCHAR,
        op VARCHAR,
        condition VARCHAR,
        input_json VARCHAR,
        criteria_json VARCHAR,
        result_json VARCHAR,
        created_at TIMESTAMP
    )
    """,
]


def _resolve_db_path(db_path: str | Path | None) -> str:
    if db_path is None:
        return str(get_settings().db_path)
    text = str(db_path)
    if text != ":memory:":
        Path(text).parent.mkdir(parents=True, exist_ok=True)
    return text


def connect_db(
    db_path: str | Path | None = None,
    *,
    retries: int = 12,
    initial_delay: float = 0.05,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB+JEVDB connection with lock-conflict retry backoff."""
    resolved = _resolve_db_path(db_path)
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with _CONNECT_LOCK:
                con = jevdb.connect(resolved)
            try:
                con.execute("PRAGMA disable_progress_bar")
            except Exception:
                con.close()
                raise
            return con
        except duckdb.IOException as exc:
            last_exc = exc
            msg = str(exc).lower()
            if ("lock" in msg or "busy" in msg) and attempt + 1 < retries:
                time.sleep(min(0.5, initial_delay * (1.6 ** attempt)))
                continue
            raise
    assert last_exc is not None
    raise last_exc


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Idempotently create all peripheral tables on an open connection."""
    with _SCHEMA_LOCK:
        existing = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        if _REQUIRED_TABLES.issubset(existing):
            return
        for ddl in SCHEMA_DDL:
            for attempt in range(8):
                try:
                    con.execute(ddl)
                    break
                except duckdb.TransactionException as exc:
                    if "conflict" in str(exc).lower() and attempt < 7:
                        time.sleep(0.02 * (attempt + 1))
                        continue
                    raise


def init_db(db_path: str | Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open a connection via JEVDB and ensure all peripheral tables exist."""
    con = connect_db(db_path)
    try:
        ensure_schema(con)
        return con
    except Exception:
        con.close()
        raise


def ensure_baseline_seeded(
    con: duckdb.DuckDBPyConnection,
    *,
    seed_strategies: bool = False,
) -> None:
    """Idempotently seed static_baseline rows (even when dynamic rows already exist) and optional strategies."""
    ensure_schema(con)
    settings = get_settings()
    bars_cnt = con.execute(
        "SELECT COUNT(*) FROM market_bars WHERE source = 'static_baseline'"
    ).fetchone()[0]
    news_cnt = con.execute(
        "SELECT COUNT(*) FROM news_events WHERE source = 'static_baseline'"
    ).fetchone()[0]
    if (bars_cnt == 0 or news_cnt == 0) and settings.static_examples_dir.exists():
        load_static_baseline(con, settings.static_examples_dir)
    if seed_strategies:
        from stock_research.quant.strategies import ensure_builtin_strategies

        ensure_builtin_strategies(con=con)


@contextmanager
def db_session(
    db_path: str | Path | None = None,
    *,
    init: bool = True,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Short-lived connection context manager to avoid cross-process DuckDB file locks."""
    con = init_db(db_path) if init else connect_db(db_path)
    try:
        yield con
    finally:
        con.close()


def _read_commented_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        lines = [line for line in stream if line.strip() and not line.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def load_static_baseline(
    con: duckdb.DuckDBPyConnection,
    examples_dir: str | Path | None = None,
) -> dict[str, int]:
    """Idempotently load examples/stock_trading/{prices,news,trading_rules}.csv into DuckDB."""
    ensure_schema(con)
    base_dir = Path(examples_dir) if examples_dir is not None else get_settings().static_examples_dir
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    prices_rows = _read_commented_csv(base_dir / "prices.csv")
    news_rows = _read_commented_csv(base_dir / "news.csv")
    rules_rows = _read_commented_csv(base_dir / "trading_rules.csv")

    assets_map: dict[str, str] = {}
    bars_params = []
    for row in prices_rows:
        symbol = row["symbol"].strip().upper()
        name = row.get("name", "").strip()
        if name:
            assets_map[symbol] = name
        volume = float(row["volume_lots"]) * 100.0
        close_px = float(row["close"])
        bars_params.append(
            (
                symbol,
                "CN",
                row["date"].strip(),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                close_px,
                volume,
                round(close_px * volume, 2),
                "static_baseline",
            )
        )

    asset_params = [
        (symbol, "CN", name, "", "", now) for symbol, name in sorted(assets_map.items())
    ]
    if asset_params:
        con.executemany(
            """
            INSERT INTO assets (symbol, market, name, sector, industry, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol, market) DO UPDATE SET
                name = COALESCE(NULLIF(excluded.name, ''), assets.name),
                updated_at = excluded.updated_at
            """,
            asset_params,
        )

    if bars_params:
        con.executemany(
            """
            INSERT INTO market_bars (
                symbol, market, trade_date, open, high, low, close, volume, amount, source
            )
            VALUES (?, ?, CAST(? AS DATE), ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol, market, trade_date, source) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                amount = excluded.amount
            """,
            bars_params,
        )

    news_params = []
    for row in news_rows:
        symbol = row["symbol"].strip().upper()
        name = assets_map.get(symbol, "")
        keys = []
        for token in ("CN", symbol, name, "static_baseline"):
            if token and token not in keys:
                keys.append(token)
        news_params.append(
            (
                row["news_id"].strip(),
                symbol,
                "CN",
                "",
                row["published_at"].strip(),
                row["title"].strip(),
                row["body"].strip(),
                row.get("source_url", "").strip(),
                "static_baseline",
                None,
                json.dumps(keys, ensure_ascii=False),
                keys,
            )
        )

    if news_params:
        con.executemany(
            """
            INSERT INTO news_events (
                event_id, symbol, market, sector, published_at, title, summary,
                url, source, sentiment_label, keywords_json, keys
            )
            VALUES (?, ?, ?, ?, CAST(? AS TIMESTAMP), ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (event_id) DO UPDATE SET
                symbol = excluded.symbol,
                market = excluded.market,
                sector = excluded.sector,
                published_at = excluded.published_at,
                title = excluded.title,
                summary = excluded.summary,
                url = excluded.url,
                source = excluded.source,
                keywords_json = excluded.keywords_json,
                keys = excluded.keys
            """,
            news_params,
        )

    rules_params = [
        (
            row["symbol"].strip().upper(),
            int(row["buy_lot"]),
            int(row["sell_lot"]),
            float(row["tick_size"]),
            float(row["commission_rate"]),
            float(row["min_commission"]),
            float(row["transfer_fee_rate"]),
            float(row["stamp_duty_rate"]),
            float(row["slippage_rate"]),
            float(row["price_limit_rate"]),
            float(row["max_participation_rate"]),
            "static_baseline",
        )
        for row in rules_rows
    ]
    if rules_params:
        con.executemany(
            """
            INSERT INTO static_trading_rules (
                symbol, buy_lot, sell_lot, tick_size, commission_rate, min_commission,
                transfer_fee_rate, stamp_duty_rate, slippage_rate, price_limit_rate,
                max_participation_rate, source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol) DO UPDATE SET
                buy_lot = excluded.buy_lot,
                sell_lot = excluded.sell_lot,
                tick_size = excluded.tick_size,
                commission_rate = excluded.commission_rate,
                min_commission = excluded.min_commission,
                transfer_fee_rate = excluded.transfer_fee_rate,
                stamp_duty_rate = excluded.stamp_duty_rate,
                slippage_rate = excluded.slippage_rate,
                price_limit_rate = excluded.price_limit_rate,
                max_participation_rate = excluded.max_participation_rate,
                source = excluded.source
            """,
            rules_params,
        )

    return {
        "assets": len(asset_params),
        "market_bars": len(bars_params),
        "news_events": len(news_params),
        "static_trading_rules": len(rules_params),
    }
