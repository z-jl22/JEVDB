"""Deterministic historical backtester and multi-strategy comparator.

Implements causal end-of-day signal evaluation (day t close) with next-session
execution (day t+1 open), CN A-share rules (T+1, 100-share buy lot, 1-share sell lot,
+/-10% price limit, exact Decimal ROUND_HALF_UP fees) and US T+0 rules.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
from typing import Any, Literal

from stock_research.config import get_settings

from .risk import compute_risk_metrics
from .strategies import (
    StrategySpec,
    evaluate_strategy_signals,
    get_strategy,
)

_POSITIVE_KEYWORDS = (
    "增长",
    "大增",
    "盈利",
    "突破",
    "中标",
    "回购",
    "增持",
    "分红",
    "利好",
    "上调",
    "超预期",
    "beat",
    "growth",
    "surge",
    "profit",
    "upgrade",
    "record",
    "buyback",
    "dividend",
)
_NEGATIVE_KEYWORDS = (
    "亏损",
    "下滑",
    "暴跌",
    "减持",
    "处罚",
    "立案",
    "违约",
    "利空",
    "下调",
    "低于预期",
    "诉讼",
    "loss",
    "decline",
    "drop",
    "downgrade",
    "lawsuit",
    "default",
    "miss",
    "warning",
)


@dataclass(frozen=True)
class MarketRule:
    market: str = "CN"
    buy_lot: int = 100
    sell_lot: int = 1
    t_plus: int = 1
    tick_size: float = 0.01
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    transfer_fee_rate: float = 0.00001
    stamp_duty_rate: float = 0.0005
    slippage_rate: float = 0.0005
    price_limit_rate: float | None = 0.10
    max_participation_rate: float = 0.01
    max_weight: float = 0.20

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_CN_RULE = MarketRule(market="CN")
DEFAULT_US_RULE = MarketRule(
    market="US",
    buy_lot=1,
    sell_lot=1,
    t_plus=0,
    tick_size=0.01,
    commission_rate=0.0002,
    min_commission=1.0,
    transfer_fee_rate=0.0,
    stamp_duty_rate=0.0,
    slippage_rate=0.0005,
    price_limit_rate=None,
    max_participation_rate=0.01,
    max_weight=0.25,
)


@dataclass(frozen=True)
class _Bar:
    day: date
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int | None
    name: str = ""


@dataclass
class BacktestResult:
    strategy_id: str
    strategy_name: str
    style: str
    market: str
    symbols: list[str]
    start_date: str
    end_date: str
    initial_capital: float
    final_equity: float
    seed: int
    equity_curve: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    metrics: dict[str, Any]
    benchmark_metrics: dict[str, Any]
    assumptions: dict[str, Any]
    reproducibility_hash: str
    benchmark_aligned_metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "style": self.style,
            "market": self.market,
            "symbols": list(self.symbols),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "final_equity": self.final_equity,
            "seed": self.seed,
            "equity_curve": list(self.equity_curve),
            "trades": list(self.trades),
            "metrics": dict(self.metrics),
            "benchmark_metrics": dict(self.benchmark_metrics),
            "benchmark_aligned_metrics": dict(self.benchmark_aligned_metrics),
            "assumptions": dict(self.assumptions),
            "reproducibility_hash": self.reproducibility_hash,
        }


@dataclass
class ComparisonResult:
    strategy_ids: list[str]
    symbols: list[str]
    start_date: str
    end_date: str
    initial_capital: float
    market: str
    results: dict[str, BacktestResult]
    ranking: list[dict[str, Any]]
    aligned_equity_curves: list[dict[str, Any]]
    aligned_drawdown_curves: list[dict[str, Any]]
    benchmark_curve: list[dict[str, Any]]
    trade_logs: dict[str, list[dict[str, Any]]]
    reproducibility_hash: str
    benchmark_aligned_curve: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_ids": list(self.strategy_ids),
            "symbols": list(self.symbols),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "market": self.market,
            "results": {sid: res.to_dict() for sid, res in self.results.items()},
            "ranking": list(self.ranking),
            "aligned_equity_curves": list(self.aligned_equity_curves),
            "aligned_drawdown_curves": list(self.aligned_drawdown_curves),
            "benchmark_curve": list(self.benchmark_curve),
            "benchmark_aligned_curve": list(self.benchmark_aligned_curve),
            "trade_logs": dict(self.trade_logs),
            "reproducibility_hash": self.reproducibility_hash,
        }


def _slipped_price(ref_price: float, slippage_rate: float, side: str, tick_size: float) -> float:
    mult = Decimal("1") + Decimal(str(slippage_rate)) if side == "BUY" else Decimal("1") - Decimal(str(slippage_rate))
    raw = Decimal(str(ref_price)) * mult
    tick = Decimal(str(tick_size))
    return float((raw / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick)


def _rounded_price(value: float, tick_size: float) -> float:
    tick = Decimal(str(tick_size))
    return float((Decimal(str(value)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick)


def _cents(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _transaction_cost(quantity: int, price: float, rule: MarketRule, action: str) -> tuple[float, float, float]:
    notional = quantity * price
    commission = _cents(max(notional * rule.commission_rate, rule.min_commission)) if quantity > 0 else 0.0
    transfer_fee = _cents(notional * rule.transfer_fee_rate)
    stamp_duty = _cents(notional * rule.stamp_duty_rate) if action == "SELL" else 0.0
    return commission, transfer_fee, stamp_duty


def _to_date(val: Any) -> date:
    parsed = _to_date_or_none(val)
    if parsed is None:
        raise ValueError(f"Invalid date: {val!r}")
    return parsed


def _to_date_or_none(val: Any) -> date | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float, dict, list, tuple, set, bytes)):
        return None
    if type(val).__name__ in {"NaTType", "NAType"}:
        return None
    if isinstance(val, datetime):
        try:
            if val != val:
                return None
            return val.date()
        except Exception:
            return None
    if isinstance(val, date):
        return val
    text = str(val).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null"}:
        return None
    if len(text) <= 10:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    if text[4] != "-" or text[7] != "-" or text[10] not in ("T", " "):
        return None
    iso_text = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        return datetime.fromisoformat(iso_text).date()
    except ValueError:
        return None


def _resolve_int_param(params: dict[str, Any], key: str, default: int, min_val: int) -> int:
    raw = params.get(key)
    if raw is None:
        return max(min_val, int(default))
    if isinstance(raw, bool):
        raise ValueError(f"Parameter {key!r} must be an integer, got {raw!r}")
    try:
        val_f = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Parameter {key!r} must be numeric, got {raw!r}") from exc
    if not math.isfinite(val_f):
        raise ValueError(f"Parameter {key!r} must be finite, got {raw!r}")
    return max(min_val, int(val_f))


def _resolve_max_weight(params: dict[str, Any], rule: MarketRule) -> float:
    raw = params.get("max_weight")
    if raw is None:
        return float(rule.max_weight)
    if isinstance(raw, bool):
        raise ValueError(f"Parameter 'max_weight' must be numeric, got {raw!r}")
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Parameter 'max_weight' must be numeric, got {raw!r}") from exc
    if not math.isfinite(val) or val < 0.0:
        raise ValueError(f"Parameter 'max_weight' must be finite and >= 0, got {raw!r}")
    return val


_SOURCE_PRIORITY: dict[str, int] = {
    "akshare_tx": 0,
    "akshare_sina": 1,
    "yfinance": 2,
    "static_baseline": 90,
}


def _source_rank(source: str) -> tuple[int, str]:
    norm = source.strip().lower()
    if not norm:
        return (99, "")
    return (_SOURCE_PRIORITY.get(norm, 50), norm)


def _finite_float_or_none(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_bar_price(val: Any, field: str, sym: str, dt_str: str) -> float:
    if val is None or isinstance(val, bool):
        raise ValueError(f"Invalid {field!r} price for {sym} on {dt_str}: {val!r}")
    try:
        num = float(val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field!r} price for {sym} on {dt_str}: {val!r}") from exc
    if not math.isfinite(num) or num <= 0.0:
        raise ValueError(f"Invalid {field!r} price for {sym} on {dt_str}: {val!r}")
    return num


def _normalize_optional_volume(vol_raw: Any, vol_lots_raw: Any = None) -> int | None:
    if vol_raw is not None and vol_raw != "" and not isinstance(vol_raw, bool):
        try:
            vf = float(vol_raw)
        except (TypeError, ValueError):
            return None
        return int(vf) if math.isfinite(vf) else None
    if vol_lots_raw is not None and vol_lots_raw != "" and not isinstance(vol_lots_raw, bool):
        try:
            vl = float(vol_lots_raw)
        except (TypeError, ValueError):
            return None
        return (int(vl) * 100) if math.isfinite(vl) else None
    return None


def _default_market_db_path() -> Path:
    env_db = os.environ.get("JEVDB_MARKET_DB")
    if env_db:
        return Path(env_db)
    try:
        from stock_research.config import get_settings  # type: ignore

        settings = get_settings()
        if getattr(settings, "db_path", None):
            return Path(settings.db_path)
    except Exception:
        pass
    return Path(__file__).resolve().parents[2] / ".tmp" / "stock_research" / "market_research.duckdb"


_CN_TICKER_RE = re.compile(
    r"^(?:(?:sh|sz|bj)?(\d{6})|(\d{6})\.(?:sh|sz|bj))$",
    re.IGNORECASE,
)


def _normalize_symbol_for_market(symbol: str, market: str | None = None) -> str:
    clean = str(symbol or "").strip().upper()
    if not clean:
        return ""
    mkt = str(market or "").strip().upper()
    if not mkt or mkt == "CN":
        m = _CN_TICKER_RE.fullmatch(clean)
        if m:
            return m.group(1) or m.group(2)
    return clean


def load_bars_from_csv(path: str | Path, symbols: list[str] | None = None) -> list[dict[str, Any]]:
    """Load daily OHLCV bars from a CSV file in the baseline data format."""
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    wanted = {s.strip().upper() for s in symbols} if symbols else None
    rows: list[dict[str, Any]] = []
    with csv_path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(line for line in stream if line.strip() and not line.lstrip().startswith("#"))
        for raw in reader:
            sym = str(raw.get("symbol") or "").strip().upper()
            if not sym or (wanted is not None and sym not in wanted):
                continue
            dt_str = str(raw.get("date") or raw.get("trade_date") or raw.get("day") or "").strip()[:10]
            volume = _normalize_optional_volume(raw.get("volume"), raw.get("volume_lots"))
            rows.append(
                {
                    "date": dt_str,
                    "symbol": sym,
                    "open": _parse_bar_price(raw.get("open"), "open", sym, dt_str),
                    "high": _parse_bar_price(raw.get("high"), "high", sym, dt_str),
                    "low": _parse_bar_price(raw.get("low"), "low", sym, dt_str),
                    "close": _parse_bar_price(raw.get("close"), "close", sym, dt_str),
                    "volume": volume,
                    "name": str(raw.get("name") or "").strip(),
                }
            )
    rows.sort(key=lambda r: (r["date"], r["symbol"]))
    return rows


def load_bars_from_duckdb(
    symbols: list[str],
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    market: str | None = None,
    db_path: str | Path | None = None,
    con: Any | None = None,
) -> list[dict[str, Any]]:
    """Load daily OHLCV bars from DuckDB market_bars table."""
    import duckdb

    mkt_u = market.strip().upper() if (market and market.strip()) else None
    norm_syms = sorted({_normalize_symbol_for_market(s, mkt_u) for s in symbols if str(s).strip()})
    if not norm_syms:
        return []

    own_con = False
    conn = con
    if conn is None:
        target = Path(db_path) if db_path is not None else _default_market_db_path()
        if not target.exists():
            return []
        conn = duckdb.connect(str(target), read_only=True)
        own_con = True

    try:
        if own_con:
            conn.execute("PRAGMA disable_progress_bar")
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        if "market_bars" not in tables:
            return []
        col_rows = conn.execute("PRAGMA table_info('market_bars')").fetchall()
        cols = {row[1] for row in col_rows}
        date_col = "trade_date" if "trade_date" in cols else "date"
        vol_expr = (
            "volume"
            if "volume" in cols
            else ("volume_lots * 100 AS volume" if "volume_lots" in cols else "NULL AS volume")
        )
        placeholders = ", ".join("?" for _ in norm_syms)
        clauses = [f"UPPER(symbol) IN ({placeholders})"]
        params: list[Any] = list(norm_syms)
        if market and "market" in cols:
            clauses.append("UPPER(market) = ?")
            params.append(market.strip().upper())
        if start_date:
            clauses.append(f"CAST({date_col} AS VARCHAR) >= ?")
            params.append(str(start_date)[:10])
        if end_date:
            clauses.append(f"CAST({date_col} AS VARCHAR) <= ?")
            params.append(str(end_date)[:10])

        source_expr = (
            "COALESCE(CAST(source AS VARCHAR), '') AS src" if "source" in cols else "'' AS src"
        )
        sql = f"""
            SELECT CAST({date_col} AS VARCHAR) AS dt, UPPER(symbol) AS sym,
                   open, high, low, close, {vol_expr}, {source_expr}
            FROM market_bars
            WHERE {' AND '.join(clauses)}
            ORDER BY dt ASC, sym ASC, src ASC
        """
        fetched = conn.execute(sql, params).fetchall()
        best_by_key: dict[tuple[str, str], tuple[tuple[int, str], dict[str, Any]]] = {}
        for dt_val, sym_val, op, hi, lo, cl, vol, src_val in fetched:
            dt_parsed = _to_date_or_none(dt_val)
            if dt_parsed is None or sym_val is None:
                continue
            dt_str = dt_parsed.isoformat()
            sym_str = str(sym_val).strip().upper()
            if not sym_str:
                continue
            if any(v is None or isinstance(v, bool) for v in (op, hi, lo, cl)):
                continue
            try:
                op_f, hi_f, lo_f, cl_f = float(op), float(hi), float(lo), float(cl)
            except (TypeError, ValueError):
                continue
            if not all(math.isfinite(v) and v > 0.0 for v in (op_f, hi_f, lo_f, cl_f)):
                continue

            vol_out: int | None = None
            if vol is not None and not isinstance(vol, bool):
                try:
                    vol_f = float(vol)
                    if math.isfinite(vol_f) and vol_f >= 0.0:
                        vol_out = int(vol_f)
                except (TypeError, ValueError):
                    vol_out = None

            rank = _source_rank(str(src_val or ""))
            key = (dt_str, sym_str)
            cand = {
                "date": dt_str,
                "symbol": sym_str,
                "open": op_f,
                "high": hi_f,
                "low": lo_f,
                "close": cl_f,
                "volume": vol_out,
            }
            prev = best_by_key.get(key)
            if prev is None or rank < prev[0]:
                best_by_key[key] = (rank, cand)

        return [best_by_key[k][1] for k in sorted(best_by_key)]
    finally:
        if own_con and conn is not None:
            conn.close()


def _load_optional_duckdb_context(
    con: Any,
    symbols: list[str],
    end_date: str,
    *,
    market: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
    placeholders = ", ".join("?" for _ in symbols)
    mkt_u = market.strip().upper() if (market and market.strip()) else None
    funds: list[dict[str, Any]] = []
    news: list[dict[str, Any]] = []

    if "fundamentals" in tables:
        f_cols = {row[1] for row in con.execute("PRAGMA table_info('fundamentals')").fetchall()}
        clauses = [
            f"UPPER(symbol) IN ({placeholders})",
            "(report_date IS NULL OR CAST(report_date AS VARCHAR) <= ?)",
        ]
        params: list[Any] = [*symbols, end_date[:10]]
        if mkt_u and "market" in f_cols:
            clauses.append("UPPER(market) = ?")
            params.append(mkt_u)
        upd_expr = "updated_at" if "updated_at" in f_cols else "NULL AS updated_at"
        src_expr = "COALESCE(CAST(source AS VARCHAR), '') AS src" if "source" in f_cols else "'' AS src"
        order_parts = ["report_date ASC NULLS FIRST"]
        if "updated_at" in f_cols:
            order_parts.append("updated_at ASC NULLS LAST")
        if "source" in f_cols:
            order_parts.append("COALESCE(CAST(source AS VARCHAR), '') ASC")
        rows = con.execute(
            f"""
            SELECT UPPER(symbol), report_date, pe_ttm, pb, roe, revenue, net_income, debt_ratio,
                   {upd_expr}, {src_expr}
            FROM fundamentals
            WHERE {' AND '.join(clauses)}
            ORDER BY {', '.join(order_parts)}
            """,
            params,
        ).fetchall()
        for r in rows:
            funds.append(
                {
                    "symbol": r[0],
                    "report_date": str(r[1])[:10] if r[1] is not None else None,
                    "pe_ttm": _finite_float_or_none(r[2]),
                    "pb": _finite_float_or_none(r[3]),
                    "roe": _finite_float_or_none(r[4]),
                    "revenue": _finite_float_or_none(r[5]),
                    "net_income": _finite_float_or_none(r[6]),
                    "debt_ratio": _finite_float_or_none(r[7]),
                    "updated_at": str(r[8]) if r[8] is not None else None,
                    "source": str(r[9] or ""),
                }
            )

    if "news_events" in tables:
        n_cols = {row[1] for row in con.execute("PRAGMA table_info('news_events')").fetchall()}
        clauses = [f"UPPER(symbol) IN ({placeholders})"]
        params = list(symbols)
        if mkt_u and "market" in n_cols:
            clauses.append("UPPER(market) = ?")
            params.append(mkt_u)
        order_parts = ["published_at ASC NULLS FIRST"]
        if "event_id" in n_cols:
            order_parts.append("event_id ASC")
        rows = con.execute(
            f"""
            SELECT UPPER(symbol), published_at, title, summary, sentiment_label
            FROM news_events
            WHERE {' AND '.join(clauses)}
            ORDER BY {', '.join(order_parts)}
            """,
            params,
        ).fetchall()
        for r in rows:
            news.append(
                {
                    "symbol": r[0],
                    "published_at": str(r[1]) if r[1] is not None else None,
                    "title": str(r[2] or ""),
                    "summary": str(r[3] or ""),
                    "sentiment_label": str(r[4] or ""),
                }
            )
    return funds, news


def _normalize_bars(
    bars_input: list[dict[str, Any]] | dict[tuple[Any, str], Any] | str | Path | None,
    symbols: list[str],
    market: str,
    end_date: date,
    db_path: str | Path | None,
    con: Any | None,
) -> tuple[dict[tuple[date, str], _Bar], list[dict[str, Any]], list[dict[str, Any]]]:
    import duckdb

    raw_rows: list[dict[str, Any]] = []
    extra_funds: list[dict[str, Any]] = []
    extra_news: list[dict[str, Any]] = []

    if isinstance(bars_input, (str, Path)):
        raw_rows = load_bars_from_csv(bars_input, symbols=symbols)
    elif isinstance(bars_input, dict):
        for key, obj in bars_input.items():
            if not isinstance(key, tuple) or len(key) != 2:
                raise ValueError(f"Dict bars key must be (date, symbol) tuple, got {key!r}")
            dt_key, sym_key = key
            sym = _normalize_symbol_for_market(str(sym_key), market)
            if sym not in symbols:
                continue
            dt_parsed = _to_date_or_none(dt_key)
            if dt_parsed is None:
                continue
            dt_iso = dt_parsed.isoformat()
            if isinstance(obj, dict):
                vol = _normalize_optional_volume(obj.get("volume"), obj.get("volume_lots"))
                raw_rows.append(
                    {
                        "date": dt_iso,
                        "symbol": sym,
                        "open": _parse_bar_price(obj.get("open"), "open", sym, dt_iso),
                        "high": _parse_bar_price(obj.get("high"), "high", sym, dt_iso),
                        "low": _parse_bar_price(obj.get("low"), "low", sym, dt_iso),
                        "close": _parse_bar_price(obj.get("close"), "close", sym, dt_iso),
                        "volume": vol,
                        "name": str(obj.get("name") or ""),
                    }
                )
            else:
                vol = _normalize_optional_volume(
                    getattr(obj, "volume", None),
                    getattr(obj, "volume_lots", None),
                )
                raw_rows.append(
                    {
                        "date": dt_iso,
                        "symbol": sym,
                        "open": _parse_bar_price(getattr(obj, "open", None), "open", sym, dt_iso),
                        "high": _parse_bar_price(getattr(obj, "high", None), "high", sym, dt_iso),
                        "low": _parse_bar_price(getattr(obj, "low", None), "low", sym, dt_iso),
                        "close": _parse_bar_price(getattr(obj, "close", None), "close", sym, dt_iso),
                        "volume": vol,
                        "name": str(getattr(obj, "name", "") or ""),
                    }
                )
    elif isinstance(bars_input, list):
        for idx, item in enumerate(bars_input):
            if not isinstance(item, dict):
                raise ValueError(
                    f"bars[{idx}] must be a dict, got {type(item).__name__}"
                )
            sym = _normalize_symbol_for_market(str(item.get("symbol") or ""), market)
            if sym not in symbols:
                continue
            raw_dt: Any = None
            for dt_field in ("date", "trade_date", "day"):
                cand_dt = item.get(dt_field)
                if cand_dt is not None:
                    raw_dt = cand_dt
                    break
            dt_parsed = _to_date_or_none(raw_dt)
            if dt_parsed is None:
                continue
            dt_iso = dt_parsed.isoformat()
            vol = _normalize_optional_volume(item.get("volume"), item.get("volume_lots"))
            raw_rows.append(
                {
                    "date": dt_iso,
                    "symbol": sym,
                    "open": _parse_bar_price(item.get("open"), "open", sym, dt_iso),
                    "high": _parse_bar_price(item.get("high"), "high", sym, dt_iso),
                    "low": _parse_bar_price(item.get("low"), "low", sym, dt_iso),
                    "close": _parse_bar_price(item.get("close"), "close", sym, dt_iso),
                    "volume": vol,
                    "name": str(item.get("name") or ""),
                }
            )
    elif bars_input is None:
        raw_rows = load_bars_from_duckdb(
            symbols,
            start_date=None,
            end_date=end_date.isoformat(),
            market=market,
            db_path=db_path,
            con=con,
        )
        if con is not None:
            extra_funds, extra_news = _load_optional_duckdb_context(
                con,
                symbols,
                end_date.isoformat(),
                market=market,
            )
        else:
            target = Path(db_path) if db_path is not None else _default_market_db_path()
            if target.exists():
                tmp_con = duckdb.connect(str(target), read_only=True)
                try:
                    tmp_con.execute("PRAGMA disable_progress_bar")
                    extra_funds, extra_news = _load_optional_duckdb_context(
                        tmp_con,
                        symbols,
                        end_date.isoformat(),
                        market=market,
                    )
                finally:
                    tmp_con.close()
        if not raw_rows and db_path is None and con is None:
            fallback_csv = get_settings(ensure_dirs=False).baseline_data_dir / "prices.csv"
            if fallback_csv.exists():
                raw_rows = load_bars_from_csv(fallback_csv, symbols=symbols)
    else:
        raise ValueError(f"Unsupported bars input type: {type(bars_input).__name__}")

    bar_map: dict[tuple[date, str], _Bar] = {}
    for row in raw_rows:
        dt = _to_date_or_none(row.get("date"))
        if dt is None or dt > end_date:
            continue
        sym = _normalize_symbol_for_market(str(row["symbol"]), market)
        op = float(row["open"])
        hi = float(row["high"])
        lo = float(row["low"])
        cl = float(row["close"])
        vol = row.get("volume")
        if vol is not None:
            vol = int(vol)
        if (
            not all(math.isfinite(v) and v > 0 for v in (op, hi, lo, cl))
            or lo > min(op, cl)
            or hi < max(op, cl)
            or (vol is not None and vol < 0)
        ):
            raise ValueError(f"Invalid OHLC bar for {sym} on {dt}")
        bar_map[(dt, sym)] = _Bar(
            day=dt,
            symbol=sym,
            open=op,
            high=hi,
            low=lo,
            close=cl,
            volume=vol,
            name=str(row.get("name") or ""),
        )
    return bar_map, extra_funds, extra_news


def _normalize_fundamentals(
    fundamentals: list[dict[str, Any]] | dict[str, list[dict[str, Any]]] | None,
    market: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    by_sym: dict[str, list[dict[str, Any]]] = {}
    if not fundamentals:
        return by_sym
    if isinstance(fundamentals, dict):
        for sym_k, entries in fundamentals.items():
            sym = _normalize_symbol_for_market(str(sym_k), market)
            if not sym:
                continue
            for entry in entries:
                item = dict(entry)
                item["symbol"] = sym
                by_sym.setdefault(sym, []).append(item)
    else:
        for entry in fundamentals:
            sym = _normalize_symbol_for_market(str(entry.get("symbol") or ""), market)
            if not sym:
                continue
            item = dict(entry)
            item["symbol"] = sym
            by_sym.setdefault(sym, []).append(item)
    for sym, items in by_sym.items():
        items.sort(
            key=lambda r: (
                dt.isoformat()
                if (dt := _to_date_or_none(r.get("report_date"))) is not None
                else ""
            )
        )
    return by_sym


def classify_news_sentiment(item: dict[str, Any]) -> Literal["positive", "negative", "neutral"]:
    """Use a known label when present, otherwise compare keyword matches in the news text."""
    label = str(item.get("sentiment_label") or item.get("impact") or "").strip().lower()
    if label in {"positive", "pos", "bullish", "1", "+1"}:
        return "positive"
    if label in {"negative", "neg", "bearish", "-1"}:
        return "negative"
    if label in {"neutral", "neu", "0"}:
        return "neutral"
    text = f"{item.get('title', '')} {item.get('summary', '')} {item.get('body', '')}".lower()
    pos_hits = sum(1 for kw in _POSITIVE_KEYWORDS if kw in text)
    neg_hits = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in text)
    if pos_hits > neg_hits:
        return "positive"
    if neg_hits > pos_hits:
        return "negative"
    return "neutral"


def _normalize_news(
    news: list[dict[str, Any]] | None,
    market: str | None = None,
) -> dict[str, list[tuple[date, int]]]:
    by_sym: dict[str, list[tuple[date, int]]] = {}
    scores = {"positive": 1, "negative": -1, "neutral": 0}
    for item in news or []:
        sym = _normalize_symbol_for_market(str(item.get("symbol") or ""), market)
        pub_raw = item.get("published_at") or item.get("date")
        if not sym or not pub_raw:
            continue
        pub_dt = _to_date_or_none(pub_raw)
        if pub_dt is None:
            continue
        by_sym.setdefault(sym, []).append((pub_dt, scores[classify_news_sentiment(item)]))
    for sym in by_sym:
        by_sym[sym].sort(key=lambda pair: pair[0])
    return by_sym


def _compute_ema(values: list[float], window: int) -> float | None:
    if not values:
        return None
    w = max(1, int(window))
    alpha = 2.0 / (w + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1.0 - alpha) * ema
    return ema


def _compute_rsi(closes: list[float], window: int) -> float | None:
    if len(closes) < 2:
        return None
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = diffs[-max(1, int(window)) :]
    gains = [max(0.0, d) for d in recent]
    losses = [max(0.0, -d) for d in recent]
    avg_gain = sum(gains) / len(recent)
    avg_loss = sum(losses) / len(recent)
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 1e-12 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _build_features(
    bar_hist: list[_Bar],
    day: date,
    params: dict[str, Any],
    funds_for_sym: list[dict[str, Any]],
    news_for_sym: list[tuple[date, int]],
    held_qty: int,
    avg_cost: float,
    hold_days: int,
) -> dict[str, Any]:
    cur = bar_hist[-1]
    closes = [b.close for b in bar_hist]
    highs = [b.high for b in bar_hist]
    lows = [b.low for b in bar_hist]
    vols = [b.volume for b in bar_hist if b.volume is not None]

    sma_short_w = _resolve_int_param(params, "sma_short_window", 3, 1)
    sma_long_w = _resolve_int_param(params, "sma_long_window", 8, 1)
    mom_w = _resolve_int_param(params, "momentum_window", 3, 1)
    z_w = _resolve_int_param(params, "zscore_window", 5, 2)
    rsi_w = _resolve_int_param(params, "rsi_window", 6, 2)
    vol_w = _resolve_int_param(params, "volatility_window", 10, 2)
    lookback_n = _resolve_int_param(params, "high_low_window", 10, 2)
    sent_days = _resolve_int_param(params, "sentiment_lookback_days", 3, 1)

    prev_close = closes[-2] if len(closes) >= 2 else None
    daily_return = ((closes[-1] / prev_close) - 1.0) if prev_close and prev_close > 0 else None

    sma_short = statistics.fmean(closes[-min(len(closes), sma_short_w) :])
    sma_long = statistics.fmean(closes[-min(len(closes), sma_long_w) :])
    ema_short = _compute_ema(closes, sma_short_w)
    ema_long = _compute_ema(closes, sma_long_w)

    if len(closes) >= 2:
        k = min(mom_w, len(closes) - 1)
        momentum_return: float | None = closes[-1] / closes[-1 - k] - 1.0
    else:
        momentum_return = None

    if len(closes) >= 2:
        z_slice = closes[-min(len(closes), z_w) :]
        z_mean = statistics.fmean(z_slice)
        z_std = statistics.stdev(z_slice)
        zscore_close: float | None = (closes[-1] - z_mean) / z_std if z_std > 1e-12 else 0.0
    else:
        zscore_close = None

    rsi = _compute_rsi(closes, rsi_w)

    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) >= 2:
        vol_slice = rets[-min(len(rets), vol_w) :]
        volatility = statistics.stdev(vol_slice) * math.sqrt(252.0) if len(vol_slice) >= 2 else 0.0
    else:
        volatility = 0.0

    high_n = max(highs[-min(len(highs), lookback_n) :])
    low_n = min(lows[-min(len(lows), lookback_n) :])
    drawdown_from_high = ((high_n - cur.close) / high_n) if high_n > 0 else 0.0

    volume_ratio: float | None = None
    if vols and cur.volume is not None:
        avg_v = statistics.fmean(vols[-min(len(vols), 10) :])
        if avg_v > 0:
            volume_ratio = cur.volume / avg_v

    unrealized_return: float | None = None
    if held_qty > 0 and avg_cost > 0:
        unrealized_return = cur.close / avg_cost - 1.0

    # Causal fundamentals (report_date <= day), coalescing non-None fields across
    # co-dated/partial sources (e.g. yfinance valuation + sec_edgar financials)
    latest_fund: dict[str, float] = {}
    has_fundamentals = False
    for f in funds_for_sym:
        rep_dt = _to_date_or_none(f.get("report_date"))
        if rep_dt is None or rep_dt <= day:
            has_fundamentals = True
            for fld in ("pe_ttm", "pb", "roe", "debt_ratio", "revenue", "net_income"):
                val = _finite_float_or_none(f.get(fld))
                if val is not None:
                    latest_fund[fld] = val
        else:
            break

    pe_ttm = latest_fund.get("pe_ttm")
    pb = latest_fund.get("pb")
    roe = latest_fund.get("roe")
    debt_ratio = latest_fund.get("debt_ratio")
    revenue = latest_fund.get("revenue")
    net_income = latest_fund.get("net_income")
    earnings_yield = (1.0 / pe_ttm) if (pe_ttm is not None and pe_ttm > 0.0) else None

    # Causal news within [day - sent_days + 1, day]
    cutoff = day - timedelta(days=sent_days - 1)
    recent_news = [score for pub_dt, score in news_for_sym if cutoff <= pub_dt <= day]
    news_count = len(recent_news)
    pos_count = sum(1 for s in recent_news if s > 0)
    neg_count = sum(1 for s in recent_news if s < 0)
    sentiment_score = statistics.fmean(recent_news) if news_count > 0 else 0.0

    return {
        "open": cur.open,
        "high": cur.high,
        "low": cur.low,
        "close": cur.close,
        "prev_close": prev_close,
        "volume": cur.volume,
        "daily_return": daily_return,
        "sma_short": sma_short,
        "sma_long": sma_long,
        "ema_short": ema_short,
        "ema_long": ema_long,
        "momentum_return": momentum_return,
        "volatility": volatility,
        "zscore_close": zscore_close,
        "rsi": rsi,
        "high_n": high_n,
        "low_n": low_n,
        "drawdown_from_high": drawdown_from_high,
        "volume_ratio": volume_ratio,
        "has_position": held_qty > 0,
        "hold_days": hold_days,
        "unrealized_return": unrealized_return,
        "has_fundamentals": has_fundamentals,
        "pe_ttm": pe_ttm,
        "pb": pb,
        "roe": roe,
        "debt_ratio": debt_ratio,
        "revenue": revenue,
        "net_income": net_income,
        "earnings_yield": earnings_yield,
        "has_news": news_count > 0,
        "news_count": news_count,
        "positive_news_count": pos_count,
        "negative_news_count": neg_count,
        "sentiment_score": sentiment_score,
    }


def _validate_market_rule(rule: MarketRule, label: str) -> MarketRule:
    mkt = str(rule.market or "").strip().upper()
    if mkt not in {"CN", "US"}:
        raise ValueError(f"{label}.market must be 'CN' or 'US', got {rule.market!r}")
    for int_field, min_val in (("buy_lot", 1), ("sell_lot", 1), ("t_plus", 0)):
        val = getattr(rule, int_field)
        if isinstance(val, bool) or not isinstance(val, int) or val < min_val:
            raise ValueError(f"{label}.{int_field} must be an integer >= {min_val}, got {val!r}")
    for pos_field in ("tick_size", "max_participation_rate"):
        val = getattr(rule, pos_field)
        if (
            isinstance(val, bool)
            or not isinstance(val, (int, float))
            or not math.isfinite(float(val))
            or float(val) <= 0.0
        ):
            raise ValueError(f"{label}.{pos_field} must be a finite number > 0, got {val!r}")
    for nonneg_field in (
        "commission_rate",
        "min_commission",
        "transfer_fee_rate",
        "stamp_duty_rate",
        "slippage_rate",
        "max_weight",
    ):
        val = getattr(rule, nonneg_field)
        if (
            isinstance(val, bool)
            or not isinstance(val, (int, float))
            or not math.isfinite(float(val))
            or float(val) < 0.0
        ):
            raise ValueError(f"{label}.{nonneg_field} must be a finite number >= 0, got {val!r}")
    plr = rule.price_limit_rate
    if plr is not None:
        if (
            isinstance(plr, bool)
            or not isinstance(plr, (int, float))
            or not math.isfinite(float(plr))
            or float(plr) < 0.0
            or float(plr) >= 1.0
        ):
            raise ValueError(
                f"{label}.price_limit_rate must be None or a finite number in [0.0, 1.0), got {plr!r}"
            )
    return rule


def _resolve_market_rules(
    market: str,
    symbols: list[str],
    rules_override: dict[str, dict[str, Any]] | None,
) -> dict[str, MarketRule]:
    base = DEFAULT_CN_RULE if market == "CN" else DEFAULT_US_RULE
    out: dict[str, MarketRule] = {}
    if rules_override is not None and not isinstance(rules_override, dict):
        raise ValueError("rules_override must be a dict mapping symbol -> overrides")
    allowed_fields = set(MarketRule.__dataclass_fields__.keys())
    overrides: dict[str, dict[str, Any]] = {}
    for k, v in (rules_override or {}).items():
        if not isinstance(v, dict):
            raise ValueError(f"rules_override[{k!r}] must be a dict")
        unknown = set(v.keys()) - allowed_fields
        if unknown:
            raise ValueError(f"Unknown MarketRule override fields for {k!r}: {sorted(unknown)}")
        overrides[_normalize_symbol_for_market(str(k), market)] = v
    for sym in symbols:
        if sym in overrides:
            ov = dict(overrides[sym])
            out[sym] = _validate_market_rule(replace(base, **ov), f"rules_override[{sym!r}]")
        else:
            out[sym] = base
    return out


def _simulate_passive_benchmark(
    bar_map: dict[tuple[date, str], Any],
    days: list[date],
    symbols: list[str],
    market_rules: dict[str, MarketRule],
    capital: float,
    weight_cap: float | None,
) -> dict[str, float]:
    """Buy equal-weight at the first executable open and hold, with real costs.

    weight_cap=None gives the fully invested benchmark; passing max_weight gives
    the same-exposure benchmark used to isolate stock-picking from position size.
    """
    start_cap = _cents(float(capital))
    if not days or not symbols:
        return {}
    entry_idx = 1 if len(days) > 1 else 0
    entry_day = days[entry_idx]
    prev_day = days[entry_idx - 1] if entry_idx > 0 else None

    budget = start_cap / len(symbols)
    if weight_cap is not None:
        budget = min(budget, start_cap * weight_cap)

    cash = start_cap
    qty: dict[str, int] = {sym: 0 for sym in symbols}
    last_close: dict[str, float] = {}
    if prev_day is not None:
        for sym in symbols:
            prev_bar = bar_map.get((prev_day, sym))
            if prev_bar is not None:
                last_close[sym] = prev_bar.close

    for sym in sorted(symbols):
        bar = bar_map.get((entry_day, sym))
        if bar is None:
            continue
        rule = market_rules[sym]
        price = _slipped_price(bar.open, rule.slippage_rate, "BUY", rule.tick_size)
        prior = last_close.get(sym)
        if rule.price_limit_rate is not None and prior is not None:
            upper = _rounded_price(prior * (1.0 + rule.price_limit_rate), rule.tick_size)
            if bar.open >= upper or price > upper:
                continue
        if price < bar.low or price > bar.high:
            continue
        affordable = min(budget, cash)
        if bar.volume:
            affordable = min(affordable, bar.volume * rule.max_participation_rate * price)
        quantity = int(affordable / price // rule.buy_lot) * rule.buy_lot
        while quantity > 0:
            commission, transfer_fee, _ = _transaction_cost(quantity, price, rule, "BUY")
            if _cents(quantity * price + commission + transfer_fee) <= cash + 1e-8:
                break
            quantity -= rule.buy_lot
        if quantity <= 0:
            continue
        commission, transfer_fee, _ = _transaction_cost(quantity, price, rule, "BUY")
        cash = _cents(cash - quantity * price - commission - transfer_fee)
        qty[sym] += quantity
        last_close[sym] = bar.close

    curve: dict[str, float] = {}
    for day in days:
        for sym in symbols:
            bar = bar_map.get((day, sym))
            if bar is not None:
                last_close[sym] = bar.close
        if day < entry_day:
            curve[day.isoformat()] = start_cap
            continue
        value = cash + sum(qty[sym] * last_close.get(sym, 0.0) for sym in symbols)
        curve[day.isoformat()] = _cents(value)
    return curve


def run_backtest(
    strategy_id: str | StrategySpec,
    symbols: list[str],
    start_date: str | date,
    end_date: str | date,
    initial_capital: float = 100000.0,
    seed: int = 42,
    market: str = "CN",
    *,
    execution: str = "next_open",
    bars: list[dict[str, Any]] | dict[tuple[Any, str], Any] | str | Path | None = None,
    fundamentals: list[dict[str, Any]] | dict[str, list[dict[str, Any]]] | None = None,
    news: list[dict[str, Any]] | None = None,
    rules_override: dict[str, dict[str, Any]] | None = None,
    db_path: str | Path | None = None,
    con: Any | None = None,
    strategy_dir: str | Path | None = None,
    risk_free_rate: float = 0.02,
) -> BacktestResult:
    """Run a deterministic daily historical backtest for a strategy over symbols."""
    raw_syms = [str(s).strip() for s in symbols if str(s).strip()]
    if not raw_syms:
        raise ValueError("symbols must be a non-empty list")
    start_dt = _to_date(start_date)
    end_dt = _to_date(end_date)
    if start_dt > end_dt:
        raise ValueError(f"start_date ({start_dt}) must not exceed end_date ({end_dt})")
    if (
        isinstance(initial_capital, bool)
        or not isinstance(initial_capital, (int, float))
        or not math.isfinite(float(initial_capital))
        or float(initial_capital) <= 0
    ):
        raise ValueError("initial_capital must be positive and finite")
    mkt = str(market or "CN").strip().upper()
    if mkt not in {"CN", "US"}:
        raise ValueError(f"market must be 'CN' or 'US', got {market!r}")
    execution = str(execution or "next_open").strip().lower()
    if execution not in {"next_open", "next_close", "t2_open"}:
        raise ValueError(f"Unsupported execution: {execution!r}")
    execution_delay = 2 if execution == "t2_open" else 1
    norm_syms = sorted({_normalize_symbol_for_market(s, mkt) for s in raw_syms})

    spec = (
        strategy_id
        if isinstance(strategy_id, StrategySpec)
        else get_strategy(strategy_id, strategy_dir=strategy_dir, con=con)
    )
    market_rules = _resolve_market_rules(mkt, norm_syms, rules_override)
    for sym in norm_syms:
        _resolve_max_weight(spec.params, market_rules[sym])

    bar_map, db_funds, db_news = _normalize_bars(bars, norm_syms, mkt, end_dt, db_path, con)
    all_days = sorted({dt for dt, _ in bar_map if start_dt <= dt <= end_dt})
    if not all_days:
        raise ValueError(f"No bars available for {norm_syms} in [{start_dt}, {end_dt}]")

    passive_full = _simulate_passive_benchmark(
        bar_map, all_days, norm_syms, market_rules, float(initial_capital), None
    )
    aligned_cap = min(_resolve_max_weight(spec.params, market_rules[sym]) for sym in norm_syms)
    passive_aligned = _simulate_passive_benchmark(
        bar_map, all_days, norm_syms, market_rules, float(initial_capital), aligned_cap
    )

    fund_map = _normalize_fundamentals(
        fundamentals if fundamentals is not None else db_funds,
        market=mkt,
    )
    news_map = _normalize_news(
        news if news is not None else db_news,
        market=mkt,
    )

    # Pre-seed previous close and warmup bar history strictly prior to start_dt
    bar_history_by_sym: dict[str, list[_Bar]] = {s: [] for s in norm_syms}
    previous_close: dict[str, float] = {}
    for (dt, sym), bar in sorted(bar_map.items()):
        if dt < all_days[0]:
            bar_history_by_sym[sym].append(bar)
            previous_close[sym] = bar.close

    cash = _cents(float(initial_capital))
    # FIFO lots per symbol: list of [qty, buy_date, unit_cost]
    lots: dict[str, list[list[Any]]] = {s: [] for s in norm_syms}
    last_known_close: dict[str, float] = dict(previous_close)
    prev_features_by_sym: dict[str, dict[str, Any]] = {}
    pending_queue: list[list[dict[str, Any]]] = []

    equity_curve: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    peak_equity = cash
    benchmark_curve: list[dict[str, Any]] = []
    benchmark_aligned_curve: list[dict[str, Any]] = []

    for day_idx, day in enumerate(all_days):
        # Phase 1: Execute signals whose delay has elapsed
        ordered_signals = sorted(
            pending_queue.pop(0) if len(pending_queue) >= execution_delay else [],
            key=lambda sig: (sig["action"] != "SELL", -float(sig["score"]), sig["symbol"]),
        )
        for sig in ordered_signals:
            sym = sig["symbol"]
            action = sig["action"]
            rule = market_rules[sym]
            bar = bar_map.get((day, sym))
            if bar is None:
                trades.append(
                    {
                        "date": day.isoformat(),
                        "symbol": sym,
                        "action": action,
                        "side": action,
                        "status": "SKIPPED",
                        "quantity": 0,
                        "price": 0.0,
                        "notional": 0.0,
                        "commission": 0.0,
                        "transfer_fee": 0.0,
                        "stamp_duty": 0.0,
                        "total_fee": 0.0,
                        "realized_pnl": None,
                        "reason": "no_bar_on_trade_date",
                        "signal_reason": sig["reason"],
                        "cash_after": _cents(cash),
                    }
                )
                continue

            ref_price = bar.close if execution == "next_close" else bar.open
            fill_price = _slipped_price(ref_price, rule.slippage_rate, action, rule.tick_size)
            prior = previous_close.get(sym)

            # Price limit check (CN)
            if rule.price_limit_rate is not None and prior is not None:
                upper = _rounded_price(prior * (1.0 + rule.price_limit_rate), rule.tick_size)
                lower = _rounded_price(prior * (1.0 - rule.price_limit_rate), rule.tick_size)
                if (action == "BUY" and (ref_price >= upper or fill_price > upper)) or (
                    action == "SELL" and (ref_price <= lower or fill_price < lower)
                ):
                    trades.append(
                        {
                            "date": day.isoformat(),
                            "symbol": sym,
                            "action": action,
                            "side": action,
                            "status": "SKIPPED",
                            "quantity": 0,
                            "price": fill_price,
                            "notional": 0.0,
                            "commission": 0.0,
                            "transfer_fee": 0.0,
                            "stamp_duty": 0.0,
                            "total_fee": 0.0,
                            "realized_pnl": None,
                            "reason": "price_limit_exceeded",
                            "signal_reason": sig["reason"],
                            "cash_after": _cents(cash),
                        }
                    )
                    continue

            if fill_price < bar.low or fill_price > bar.high:
                trades.append(
                    {
                        "date": day.isoformat(),
                        "symbol": sym,
                        "action": action,
                        "side": action,
                        "status": "SKIPPED",
                        "quantity": 0,
                        "price": fill_price,
                        "notional": 0.0,
                        "commission": 0.0,
                        "transfer_fee": 0.0,
                        "stamp_duty": 0.0,
                        "total_fee": 0.0,
                        "realized_pnl": None,
                        "reason": "outside_bar_range",
                        "signal_reason": sig["reason"],
                        "cash_after": _cents(cash),
                    }
                )
                continue

            volume_cap = (
                int(bar.volume * rule.max_participation_rate)
                if bar.volume is not None
                else None
            )

            if action == "SELL":
                if rule.t_plus >= 1:
                    sellable_qty = sum(lot[0] for lot in lots[sym] if (day - lot[1]).days >= rule.t_plus)
                else:
                    sellable_qty = sum(lot[0] for lot in lots[sym])
                capped = min(sellable_qty, volume_cap) if volume_cap is not None else sellable_qty
                qty = (capped // rule.sell_lot) * rule.sell_lot
                if qty <= 0:
                    skip_reason = (
                        "t_plus_one_or_no_position" if sellable_qty <= 0 else "volume_participation_limit"
                    )
                    trades.append(
                        {
                            "date": day.isoformat(),
                            "symbol": sym,
                            "action": "SELL",
                            "side": "SELL",
                            "status": "SKIPPED",
                            "quantity": 0,
                            "price": fill_price,
                            "notional": 0.0,
                            "commission": 0.0,
                            "transfer_fee": 0.0,
                            "stamp_duty": 0.0,
                            "total_fee": 0.0,
                            "realized_pnl": None,
                            "reason": skip_reason,
                            "signal_reason": sig["reason"],
                            "cash_after": _cents(cash),
                        }
                    )
                    continue

                comm, xfer, stamp = _transaction_cost(qty, fill_price, rule, "SELL")
                total_fee = _cents(comm + xfer + stamp)
                notional = _cents(qty * fill_price)
                net_proceeds = _cents(notional - total_fee)
                cash = _cents(cash + net_proceeds)

                # Consume eligible lots FIFO
                rem = qty
                cost_basis = 0.0
                new_lots: list[list[Any]] = []
                for lot in lots[sym]:
                    lot_qty, lot_dt, lot_unit_cost = lot
                    eligible = (day - lot_dt).days >= rule.t_plus if rule.t_plus >= 1 else True
                    if eligible and rem > 0:
                        take = min(rem, lot_qty)
                        cost_basis += take * lot_unit_cost
                        leftover = lot_qty - take
                        rem -= take
                        if leftover > 0:
                            new_lots.append([leftover, lot_dt, lot_unit_cost])
                    else:
                        new_lots.append(lot)
                lots[sym] = new_lots
                realized_pnl = _cents(net_proceeds - cost_basis)

                trades.append(
                    {
                        "date": day.isoformat(),
                        "symbol": sym,
                        "action": "SELL",
                        "side": "SELL",
                        "status": "FILLED",
                        "quantity": qty,
                        "price": fill_price,
                        "notional": notional,
                        "commission": comm,
                        "transfer_fee": xfer,
                        "stamp_duty": stamp,
                        "total_fee": total_fee,
                        "realized_pnl": realized_pnl,
                        "reason": sig["reason"],
                        "cash_after": cash,
                    }
                )

            elif action == "BUY":
                held_value_open = 0.0
                for s in norm_syms:
                    s_qty = sum(lot[0] for lot in lots[s])
                    if s_qty > 0:
                        s_bar = bar_map.get((day, s))
                        px_open = s_bar.open if s_bar is not None else last_known_close.get(s, 0.0)
                        held_value_open += s_qty * px_open
                equity_at_open = cash + held_value_open
                max_weight = _resolve_max_weight(spec.params, rule)
                cur_sym_qty = sum(lot[0] for lot in lots[sym])
                remaining_weight_val = max(0.0, max_weight * equity_at_open - cur_sym_qty * fill_price)

                max_shares_vol = volume_cap if volume_cap is not None else 10**9
                max_by_weight = int(remaining_weight_val / fill_price) if fill_price > 0 else 0
                max_by_cash = int(cash / fill_price) if fill_price > 0 else 0
                raw_shares = min(max_shares_vol, max_by_weight, max_by_cash)
                qty = (raw_shares // rule.buy_lot) * rule.buy_lot

                comm = xfer = stamp = 0.0
                while qty > 0:
                    comm, xfer, stamp = _transaction_cost(qty, fill_price, rule, "BUY")
                    total_cost = _cents(qty * fill_price + comm + xfer)
                    if total_cost <= cash + 1e-8 and qty * fill_price <= remaining_weight_val + 1e-8:
                        break
                    qty -= rule.buy_lot

                if qty <= 0:
                    trades.append(
                        {
                            "date": day.isoformat(),
                            "symbol": sym,
                            "action": "BUY",
                            "side": "BUY",
                            "status": "SKIPPED",
                            "quantity": 0,
                            "price": fill_price,
                            "notional": 0.0,
                            "commission": 0.0,
                            "transfer_fee": 0.0,
                            "stamp_duty": 0.0,
                            "total_fee": 0.0,
                            "realized_pnl": None,
                            "reason": "insufficient_cash_or_limit",
                            "signal_reason": sig["reason"],
                            "cash_after": _cents(cash),
                        }
                    )
                    continue

                total_fee = _cents(comm + xfer + stamp)
                notional = _cents(qty * fill_price)
                total_outflow = _cents(notional + total_fee)
                cash = _cents(cash - total_outflow)
                lots[sym].append([qty, day, total_outflow / qty])

                trades.append(
                    {
                        "date": day.isoformat(),
                        "symbol": sym,
                        "action": "BUY",
                        "side": "BUY",
                        "status": "FILLED",
                        "quantity": qty,
                        "price": fill_price,
                        "notional": notional,
                        "commission": comm,
                        "transfer_fee": xfer,
                        "stamp_duty": stamp,
                        "total_fee": total_fee,
                        "realized_pnl": None,
                        "reason": sig["reason"],
                        "cash_after": cash,
                    }
                )

        # Phase 2: Mark-to-market at day_t close & update bar history
        market_value = 0.0
        for sym in norm_syms:
            bar = bar_map.get((day, sym))
            if bar is not None:
                bar_history_by_sym[sym].append(bar)
                last_known_close[sym] = bar.close
            sym_qty = sum(lot[0] for lot in lots[sym])
            if sym_qty > 0 and sym in last_known_close:
                market_value += sym_qty * last_known_close[sym]

        market_value = _cents(market_value)
        equity = _cents(cash + market_value)
        if equity > peak_equity:
            peak_equity = equity
        drawdown = round((peak_equity - equity) / peak_equity, 6) if peak_equity > 0 else 0.0

        # Tradable passive benchmarks: fully invested and same-exposure
        bench_equity = passive_full.get(day.isoformat(), _cents(float(initial_capital)))
        bench_aligned_equity = passive_aligned.get(day.isoformat(), _cents(float(initial_capital)))

        equity_curve.append(
            {
                "date": day.isoformat(),
                "equity": equity,
                "benchmark_equity": bench_equity,
                "benchmark_aligned_equity": bench_aligned_equity,
                "drawdown": drawdown,
                "cash": cash,
                "market_value": market_value,
            }
        )
        benchmark_curve.append({"date": day.isoformat(), "equity": bench_equity})
        benchmark_aligned_curve.append({"date": day.isoformat(), "equity": bench_aligned_equity})

        # Phase 3: Evaluate causal signals at day_t close, executed after the delay
        new_signals: list[dict[str, Any]] = []
        if day_idx + 1 < len(all_days):
            for sym in norm_syms:
                if bar_map.get((day, sym)) is None:
                    continue
                hist = bar_history_by_sym[sym]
                if not hist:
                    continue
                sym_lots = lots[sym]
                held_qty = sum(lot[0] for lot in sym_lots)
                avg_cost = (sum(lot[0] * lot[2] for lot in sym_lots) / held_qty) if held_qty > 0 else 0.0
                hold_days = max(((day - lot[1]).days for lot in sym_lots), default=0)
                feats = _build_features(
                    hist,
                    day,
                    spec.params,
                    fund_map.get(sym, []),
                    news_map.get(sym, []),
                    held_qty,
                    avg_cost,
                    hold_days,
                )
                prev_feats = prev_features_by_sym.get(sym)
                sig_out = evaluate_strategy_signals(
                    spec,
                    feats,
                    prev_feats,
                    has_position=held_qty > 0,
                    hold_days=hold_days,
                )
                prev_features_by_sym[sym] = feats

                action = sig_out["action"]
                if action == "SELL" and held_qty > 0:
                    new_signals.append(
                        {"symbol": sym, "action": "SELL", "score": sig_out["score"], "reason": sig_out["reason"]}
                    )
                elif action == "BUY":
                    rule = market_rules[sym]
                    max_w = _resolve_max_weight(spec.params, rule)
                    cur_w = (held_qty * last_known_close[sym] / equity) if equity > 0 else 0.0
                    if cur_w < max_w - 1e-6:
                        new_signals.append(
                            {"symbol": sym, "action": "BUY", "score": sig_out["score"], "reason": sig_out["reason"]}
                        )

        pending_queue.append(new_signals)

        for sym in norm_syms:
            bar = bar_map.get((day, sym))
            if bar is not None:
                previous_close[sym] = bar.close

    metrics = compute_risk_metrics(
        equity_curve,
        trades,
        initial_capital=float(initial_capital),
        risk_free_rate=risk_free_rate,
    )
    benchmark_metrics = compute_risk_metrics(
        benchmark_curve,
        [],
        initial_capital=float(initial_capital),
        risk_free_rate=risk_free_rate,
    )
    benchmark_aligned_metrics = compute_risk_metrics(
        benchmark_aligned_curve,
        [],
        initial_capital=float(initial_capital),
        risk_free_rate=risk_free_rate,
    )

    assumptions = {
        "execution_model": (
            "Signals evaluated at day_t close execute after "
            f"{execution_delay} trading day(s) at the "
            f"{'close' if execution == 'next_close' else 'open'}; sells before buys"
        ),
        "execution": execution,
        "market": mkt,
        "rules": {sym: market_rules[sym].to_dict() for sym in norm_syms},
        "benchmark": (
            "Tradable equal-weight buy-and-hold: buys at the first executable open with the same "
            "slippage, lot size, fee schedule and participation cap as the strategy, holds to the end"
        ),
        "benchmark_aligned": (
            f"Same-exposure benchmark capped at max_weight={aligned_cap:.4f} per symbol, "
            "otherwise identical to the fully invested benchmark"
        ),
        "price_limit_note": "CN default +/-10% main-board limit against prior close (override via rules_override)" if mkt == "CN" else "US has no daily price limit",
    }

    hash_payload = {
        "strategy_id": spec.strategy_id,
        "style": spec.style,
        "market": spec.market,
        "params": spec.params,
        "rules": spec.rules,
        "symbols": norm_syms,
        "start_date": start_dt.isoformat(),
        "end_date": end_dt.isoformat(),
        "initial_capital": float(initial_capital),
        "seed": int(seed),
        "execution": execution,
        "market_rules": {sym: market_rules[sym].to_dict() for sym in norm_syms},
        "bars_count": len(bar_map),
        "equity_curve": equity_curve,
        "trades": trades,
    }
    repro_hash = hashlib.sha256(
        json.dumps(hash_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    return BacktestResult(
        strategy_id=spec.strategy_id,
        strategy_name=spec.name,
        style=spec.style,
        market=mkt,
        symbols=norm_syms,
        start_date=start_dt.isoformat(),
        end_date=end_dt.isoformat(),
        initial_capital=_cents(float(initial_capital)),
        final_equity=equity_curve[-1]["equity"],
        seed=int(seed),
        equity_curve=equity_curve,
        trades=trades,
        metrics=metrics,
        benchmark_metrics=benchmark_metrics,
        benchmark_aligned_metrics=benchmark_aligned_metrics,
        assumptions=assumptions,
        reproducibility_hash=repro_hash,
    )


def _align_comparison_curves(
    ordered_ids: list[str],
    results: dict[str, BacktestResult],
    initial_capital: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    all_dates = sorted({row["date"] for sid in ordered_ids for row in results[sid].equity_curve})
    by_sid_date = {
        sid: {row["date"]: row for row in results[sid].equity_curve}
        for sid in ordered_ids
    }
    init_cap = _cents(float(initial_capital))
    last_eq: dict[str, float] = {sid: init_cap for sid in ordered_ids}
    last_dd: dict[str, float] = {sid: 0.0 for sid in ordered_ids}
    last_bench: float = init_cap
    last_bench_aligned: float = init_cap

    aligned_equity_curves: list[dict[str, Any]] = []
    aligned_drawdown_curves: list[dict[str, Any]] = []
    benchmark_curve: list[dict[str, Any]] = []
    benchmark_aligned_curve: list[dict[str, Any]] = []

    for dt_str in all_dates:
        eq_row: dict[str, Any] = {"date": dt_str}
        dd_row: dict[str, Any] = {"date": dt_str}
        bench_val: float | None = None
        bench_aligned_val: float | None = None
        for sid in ordered_ids:
            pt = by_sid_date[sid].get(dt_str)
            if pt is not None:
                eq_v = pt.get("equity")
                dd_v = pt.get("drawdown")
                if isinstance(eq_v, (int, float)) and not isinstance(eq_v, bool) and math.isfinite(float(eq_v)):
                    last_eq[sid] = float(eq_v)
                if isinstance(dd_v, (int, float)) and not isinstance(dd_v, bool) and math.isfinite(float(dd_v)):
                    last_dd[sid] = float(dd_v)
                b_v = pt.get("benchmark_equity")
                if (
                    bench_val is None
                    and isinstance(b_v, (int, float))
                    and not isinstance(b_v, bool)
                    and math.isfinite(float(b_v))
                ):
                    bench_val = float(b_v)
                ba_v = pt.get("benchmark_aligned_equity")
                if (
                    bench_aligned_val is None
                    and isinstance(ba_v, (int, float))
                    and not isinstance(ba_v, bool)
                    and math.isfinite(float(ba_v))
                ):
                    bench_aligned_val = float(ba_v)
            eq_row[sid] = last_eq[sid]
            dd_row[sid] = last_dd[sid]
        if bench_val is None:
            bench_val = last_bench
        else:
            last_bench = bench_val
        if bench_aligned_val is None:
            bench_aligned_val = last_bench_aligned
        else:
            last_bench_aligned = bench_aligned_val
        eq_row["benchmark_equity"] = bench_val
        eq_row["benchmark_aligned_equity"] = bench_aligned_val
        aligned_equity_curves.append(eq_row)
        aligned_drawdown_curves.append(dd_row)
        benchmark_curve.append({"date": dt_str, "equity": bench_val})
        benchmark_aligned_curve.append({"date": dt_str, "equity": bench_aligned_val})

    return aligned_equity_curves, aligned_drawdown_curves, benchmark_curve, benchmark_aligned_curve


def compare_strategies(
    strategy_ids: list[str | StrategySpec],
    symbols: list[str],
    start_date: str | date,
    end_date: str | date,
    initial_capital: float = 100000.0,
    market: str = "CN",
    *,
    seed: int = 42,
    bars: list[dict[str, Any]] | dict[tuple[Any, str], Any] | str | Path | None = None,
    fundamentals: list[dict[str, Any]] | dict[str, list[dict[str, Any]]] | None = None,
    news: list[dict[str, Any]] | None = None,
    rules_override: dict[str, dict[str, Any]] | None = None,
    db_path: str | Path | None = None,
    con: Any | None = None,
    strategy_dir: str | Path | None = None,
    risk_free_rate: float = 0.02,
    sort_by: str = "sharpe_ratio",
) -> ComparisonResult:
    """Run deterministic backtests across multiple strategies and align outputs."""
    if not strategy_ids:
        raise ValueError("strategy_ids must be a non-empty list")

    results: dict[str, BacktestResult] = {}
    ordered_ids: list[str] = []
    for item in strategy_ids:
        res = run_backtest(
            item,
            symbols,
            start_date,
            end_date,
            initial_capital=initial_capital,
            seed=seed,
            market=market,
            bars=bars,
            fundamentals=fundamentals,
            news=news,
            rules_override=rules_override,
            db_path=db_path,
            con=con,
            strategy_dir=strategy_dir,
            risk_free_rate=risk_free_rate,
        )
        if res.strategy_id not in results:
            ordered_ids.append(res.strategy_id)
        results[res.strategy_id] = res

    first_res = results[ordered_ids[0]]
    aligned_equity_curves, aligned_drawdown_curves, benchmark_curve, benchmark_aligned_curve = _align_comparison_curves(
        ordered_ids, results, initial_capital
    )

    ascending_metrics = {"max_drawdown", "annualized_volatility", "var_95", "cvar_95"}
    is_asc = sort_by in ascending_metrics

    def _rank_key(sid: str) -> tuple[float, str]:
        val = results[sid].metrics.get(sort_by)
        if val is None or not isinstance(val, (int, float)) or not math.isfinite(float(val)):
            num = float("inf") if is_asc else float("-inf")
        else:
            num = float(val) if is_asc else -float(val)
        return (num, sid)

    ranked_sids = sorted(ordered_ids, key=_rank_key)
    ranking: list[dict[str, Any]] = []
    for idx, sid in enumerate(ranked_sids, start=1):
        res = results[sid]
        m = res.metrics
        ranking.append(
            {
                "rank": idx,
                "strategy_id": sid,
                "strategy_name": res.strategy_name,
                "style": res.style,
                "final_equity": res.final_equity,
                "total_return": m["total_return"],
                "annualized_return": m["annualized_return"],
                "sharpe_ratio": m["sharpe_ratio"],
                "sortino_ratio": m["sortino_ratio"],
                "calmar_ratio": m["calmar_ratio"],
                "max_drawdown": m["max_drawdown"],
                "annualized_volatility": m["annualized_volatility"],
                "win_rate": m["win_rate"],
                "profit_factor": m["profit_factor"],
                "total_trades": m["total_trades"],
                "total_fees": m["total_fees"],
                "fee_drag": m.get("fee_drag"),
                "gross_return": m.get("gross_return"),
                "turnover": m.get("turnover"),
            }
        )

    trade_logs = {sid: results[sid].trades for sid in ordered_ids}
    comp_hash = hashlib.sha256(
        json.dumps(
            {
                "ordered_ids": ordered_ids,
                "results": {sid: results[sid].reproducibility_hash for sid in ordered_ids},
                "ranking": ranking,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    return ComparisonResult(
        strategy_ids=ordered_ids,
        symbols=first_res.symbols,
        start_date=first_res.start_date,
        end_date=first_res.end_date,
        initial_capital=first_res.initial_capital,
        market=first_res.market,
        results=results,
        ranking=ranking,
        aligned_equity_curves=aligned_equity_curves,
        aligned_drawdown_curves=aligned_drawdown_curves,
        benchmark_curve=benchmark_curve,
        benchmark_aligned_curve=benchmark_aligned_curve,
        trade_logs=trade_logs,
        reproducibility_hash=comp_hash,
    )


DEFAULT_FEE_SCENARIOS: tuple[tuple[float, float], ...] = (
    (0.0003, 5.0),
    (0.0002, 2.0),
    (0.0001, 0.0),
    (0.0, 0.0),
)


def fee_sensitivity(
    strategy_id: str | StrategySpec,
    symbols: list[str],
    start_date: str | date,
    end_date: str | date,
    initial_capital: float = 100000.0,
    scenarios: Any = None,
    market: str = "CN",
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Run one strategy under several fee scenarios; report gross vs net impact.

    Each scenario is (commission_rate, min_commission). Varying the rate alone is
    usually meaningless for small tickets because the per-order minimum dominates.
    """
    mkt = str(market or "CN").strip().upper()
    norm = sorted({_normalize_symbol_for_market(s, mkt) for s in symbols if str(s).strip()})
    pairs = scenarios if scenarios else DEFAULT_FEE_SCENARIOS
    rows: list[dict[str, Any]] = []
    for item in pairs:
        rate, min_commission = float(item[0]), float(item[1])
        override = {
            sym: {"commission_rate": rate, "min_commission": min_commission}
            for sym in norm
        }
        res = run_backtest(
            strategy_id,
            symbols,
            start_date,
            end_date,
            initial_capital=initial_capital,
            market=mkt,
            rules_override=override,
            **kwargs,
        )
        rows.append(
            {
                "commission_rate": rate,
                "min_commission": min_commission,
                "final_equity": res.final_equity,
                "total_return": res.metrics.get("total_return"),
                "gross_return": res.metrics.get("gross_return"),
                "fee_drag": res.metrics.get("fee_drag"),
                "total_fees": res.metrics.get("total_fees"),
                "turnover": res.metrics.get("turnover"),
                "total_trades": res.metrics.get("total_trades"),
            }
        )
    return rows


EXECUTION_MODES: tuple[str, ...] = ("next_open", "next_close", "t2_open")


def execution_sensitivity(
    strategy_id: str | StrategySpec,
    symbols: list[str],
    start_date: str | date,
    end_date: str | date,
    initial_capital: float = 100000.0,
    modes: Any = None,
    market: str = "CN",
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Run one strategy under several execution assumptions and compare outcomes."""
    mkt = str(market or "CN").strip().upper()
    rows: list[dict[str, Any]] = []
    for mode in (modes or EXECUTION_MODES):
        res = run_backtest(
            strategy_id,
            symbols,
            start_date,
            end_date,
            initial_capital=initial_capital,
            market=mkt,
            execution=str(mode),
            **kwargs,
        )
        rows.append(
            {
                "execution": str(mode),
                "final_equity": res.final_equity,
                "total_return": res.metrics.get("total_return"),
                "max_drawdown": res.metrics.get("max_drawdown"),
                "sharpe_ratio": res.metrics.get("sharpe_ratio"),
                "total_trades": res.metrics.get("total_trades"),
                "total_fees": res.metrics.get("total_fees"),
            }
        )
    return rows
