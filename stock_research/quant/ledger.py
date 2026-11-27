"""Persistent DuckDB paper-trading account and ledger service.

Uses short-lived connection-per-operation with explicit transactions + CHECKPOINT
for file-backed DuckDB databases so separate subprocesses never suffer file-lock
contention, while keeping an open connection only for ``":memory:"``.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]
import json
import math
import os
from pathlib import Path
import random
import re
import threading
import time
from typing import Any, Iterator
from uuid import uuid4

import duckdb

_EPS = 1e-8
_TICK_SIZE = 0.01
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CN_TICKER_RE = re.compile(r"^(?:(?:sh|sz|bj)?(\d{6})|(\d{6})\.(?:sh|sz|bj))$", re.IGNORECASE)


def _normalize_cn_ticker(sym: str) -> str:
    cn_match = _CN_TICKER_RE.fullmatch(sym)
    return (cn_match.group(1) or cn_match.group(2)) if cn_match else sym


def _open_duckdb_connection(target: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(target)
    try:
        con.execute("PRAGMA disable_progress_bar")
    except Exception:
        con.close()
        raise
    return con

_PATH_LOCK_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCK_DEPTH: dict[str, int] = {}


def _get_path_lock(resolved_path: str) -> threading.RLock:
    with _PATH_LOCK_GUARD:
        lock = _PATH_LOCKS.get(resolved_path)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[resolved_path] = lock
        return lock


def _parse_iso_date(raw_val: Any) -> str:
    if isinstance(raw_val, datetime):
        return raw_val.date().isoformat()
    if isinstance(raw_val, date):
        return raw_val.isoformat()
    if not isinstance(raw_val, str):
        raise ValueError(f"Expected ISO date string YYYY-MM-DD, got {type(raw_val).__name__}")
    s = raw_val.strip()
    if _ISO_DATE_RE.match(s):
        return date.fromisoformat(s).isoformat()
    if len(s) > 10 and s[10] in ("T", " "):
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        return datetime.fromisoformat(iso).date().isoformat()
    raise ValueError(f"Invalid ISO date string: {raw_val!r}")


def _parse_mark_price_value(raw_v: Any, label: str) -> float:
    if isinstance(raw_v, dict):
        raw_px = raw_v.get("close")
        if raw_px is None:
            raw_px = raw_v.get("price")
    else:
        raw_px = raw_v
    if raw_px is None or isinstance(raw_px, bool):
        raise ValueError(f"Invalid {label}: {raw_v!r}")
    try:
        px = float(raw_px)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {label}: {raw_v!r}") from exc
    if not math.isfinite(px) or px <= 0:
        raise ValueError(f"Invalid {label}: {raw_v!r}")
    return px


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _cents(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _rounded_price(value: float, tick_size: float = _TICK_SIZE) -> float:
    tick = Decimal(str(tick_size))
    return float((Decimal(str(value)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick)


def _slipped_price(ref_price: float, slippage_rate: float, side: str, tick_size: float = _TICK_SIZE) -> float:
    mult = Decimal("1") + Decimal(str(slippage_rate)) if side == "BUY" else Decimal("1") - Decimal(str(slippage_rate))
    raw = Decimal(str(ref_price)) * mult
    tick = Decimal(str(tick_size))
    return float((raw / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick)


def _default_ledger_db_path() -> Path:
    env_db = os.environ.get("JEVDB_PAPER_LEDGER_DB")
    if env_db:
        return Path(env_db)
    try:
        from stock_research.config import get_settings  # type: ignore

        settings = get_settings()
        if getattr(settings, "ledger_db_path", None):
            return Path(settings.ledger_db_path)
    except Exception:
        pass
    return Path(__file__).resolve().parents[2] / ".tmp" / "stock_research" / "paper_ledger.duckdb"


class PaperAccountService:
    """Multi-day persistent paper-trading ledger supporting CN (T+1) and US (T+0) rules."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        initial_capital: float = 100000.0,
        slippage_bps: float = 5.0,
        us_commission_rate: float = 0.0,
        us_min_commission: float = 0.0,
        us_per_share_fee: float = 0.0,
        initial_date: str | None = None,
    ) -> None:
        if (
            isinstance(initial_capital, bool)
            or not isinstance(initial_capital, (int, float))
            or not math.isfinite(float(initial_capital))
            or float(initial_capital) <= 0
        ):
            raise ValueError("initial_capital must be positive and finite")
        if (
            isinstance(slippage_bps, bool)
            or not isinstance(slippage_bps, (int, float))
            or not math.isfinite(float(slippage_bps))
            or float(slippage_bps) < 0
        ):
            raise ValueError("slippage_bps must be non-negative and finite")
        for name, val in (
            ("us_commission_rate", us_commission_rate),
            ("us_min_commission", us_min_commission),
            ("us_per_share_fee", us_per_share_fee),
        ):
            if (
                isinstance(val, bool)
                or not isinstance(val, (int, float))
                or not math.isfinite(float(val))
                or float(val) < 0
            ):
                raise ValueError(f"{name} must be non-negative and finite")

        self.db_path_str = str(db_path) if db_path is not None else str(_default_ledger_db_path())
        self.is_memory = self.db_path_str == ":memory:"
        self.db_path = None if self.is_memory else Path(self.db_path_str)
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self.default_initial_capital = _cents(float(initial_capital))
        self.slippage_bps = float(slippage_bps)
        self.slippage_rate = float(slippage_bps) / 10000.0
        self.us_commission_rate = float(us_commission_rate)
        self.us_min_commission = float(us_min_commission)
        self.us_per_share_fee = float(us_per_share_fee)
        self.default_initial_date = (
            _parse_iso_date(initial_date)
            if initial_date is not None
            else _today_iso()
        )

        self._lock = threading.RLock()
        self._mem_con: duckdb.DuckDBPyConnection | None = (
            _open_duckdb_connection(":memory:") if self.is_memory else None
        )
        self._init_schema_and_account()

    @contextmanager
    def _acquire_file_locks(self) -> Iterator[None]:
        if self.is_memory:
            with self._lock:
                yield
            return
        assert self.db_path is not None
        path_key = os.path.realpath(str(self.db_path))
        proc_lock = _get_path_lock(path_key)
        with proc_lock:
            depth = _PATH_LOCK_DEPTH.get(path_key, 0)
            fd: int | None = None
            if depth == 0 and fcntl is not None:
                lock_path = self.db_path.with_name(f"{self.db_path.name}.lock")
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o666)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except Exception:
                    os.close(fd)
                    raise
            _PATH_LOCK_DEPTH[path_key] = depth + 1
            try:
                yield
            finally:
                if depth == 0:
                    _PATH_LOCK_DEPTH.pop(path_key, None)
                else:
                    _PATH_LOCK_DEPTH[path_key] = depth
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)

    @contextmanager
    def _connect(self, *, write: bool = True) -> Iterator[duckdb.DuckDBPyConnection]:
        if self.is_memory:
            with self._lock:
                if self._mem_con is None:
                    self._mem_con = _open_duckdb_connection(":memory:")
                con = self._mem_con
                if write:
                    con.execute("BEGIN TRANSACTION")
                try:
                    yield con
                    if write:
                        con.execute("COMMIT")
                except Exception:
                    if write:
                        try:
                            con.execute("ROLLBACK")
                        except Exception:
                            pass
                    raise
            return

        assert self.db_path is not None
        with self._acquire_file_locks():
            last_exc: Exception | None = None
            con: duckdb.DuckDBPyConnection | None = None
            for attempt in range(15):
                try:
                    con = _open_duckdb_connection(str(self.db_path))
                    break
                except duckdb.IOException as exc:
                    last_exc = exc
                    delay = min(0.25, 0.015 * (1.35 ** attempt)) + random.uniform(0.0, 0.015)
                    time.sleep(delay)
            if con is None:
                assert last_exc is not None
                raise last_exc

            try:
                if write:
                    con.execute("BEGIN TRANSACTION")
                yield con
                if write:
                    con.execute("COMMIT")
                    con.execute("CHECKPOINT")
            except Exception:
                if write:
                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass
                raise
            finally:
                con.close()

    def close(self) -> None:
        with self._lock:
            if self._mem_con is not None:
                self._mem_con.close()
                self._mem_con = None

    def __enter__(self) -> "PaperAccountService":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _create_tables(self, con: duckdb.DuckDBPyConnection) -> None:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS account_meta (
                account_id VARCHAR PRIMARY KEY,
                initial_capital DOUBLE,
                cash DOUBLE,
                realized_pnl DOUBLE,
                current_date VARCHAR,
                created_at VARCHAR,
                updated_at VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS position_lots (
                lot_id VARCHAR PRIMARY KEY,
                symbol VARCHAR,
                market VARCHAR,
                quantity BIGINT,
                remaining_qty BIGINT,
                buy_date VARCHAR,
                avg_cost DOUBLE,
                total_cost DOUBLE,
                strategy_id VARCHAR,
                created_at VARCHAR,
                updated_at VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id VARCHAR PRIMARY KEY,
                seq_no INTEGER,
                trade_date VARCHAR,
                symbol VARCHAR,
                market VARCHAR,
                side VARCHAR,
                order_type VARCHAR,
                requested_qty BIGINT,
                filled_qty BIGINT,
                limit_price DOUBLE,
                fill_price DOUBLE,
                status VARCHAR,
                reject_code VARCHAR,
                reject_reason VARCHAR,
                strategy_id VARCHAR,
                created_at VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS fills (
                fill_id VARCHAR PRIMARY KEY,
                order_id VARCHAR,
                seq_no INTEGER,
                trade_date VARCHAR,
                symbol VARCHAR,
                market VARCHAR,
                side VARCHAR,
                quantity BIGINT,
                reference_price DOUBLE,
                fill_price DOUBLE,
                notional DOUBLE,
                slippage_cost DOUBLE,
                commission DOUBLE,
                transfer_fee DOUBLE,
                stamp_duty DOUBLE,
                total_fee DOUBLE,
                net_cash_delta DOUBLE,
                realized_pnl DOUBLE,
                cash_after DOUBLE,
                created_at VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                snapshot_date VARCHAR PRIMARY KEY,
                cash DOUBLE,
                market_value DOUBLE,
                total_equity DOUBLE,
                realized_pnl DOUBLE,
                unrealized_pnl DOUBLE,
                positions_json VARCHAR,
                updated_at VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS mark_prices (
                symbol VARCHAR,
                market VARCHAR,
                price_date VARCHAR,
                close_price DOUBLE,
                prev_close DOUBLE,
                PRIMARY KEY (symbol, market)
            )
            """
        )

    def _ensure_bigint_columns(self, con: duckdb.DuckDBPyConnection) -> None:
        targets = (
            ("position_lots", "quantity"),
            ("position_lots", "remaining_qty"),
            ("orders", "requested_qty"),
            ("orders", "filled_qty"),
            ("fills", "quantity"),
        )
        for tbl, col in targets:
            info = con.execute(f"PRAGMA table_info('{tbl}')").fetchall()
            col_type = next((str(r[2]).upper() for r in info if r[1] == col), "")
            if col_type and col_type != "BIGINT":
                con.execute(f"ALTER TABLE {tbl} ALTER COLUMN {col} TYPE BIGINT")

    def _init_schema_and_account(self) -> None:
        if (
            not self.is_memory
            and self.db_path is not None
            and self.db_path.exists()
            and self.db_path.stat().st_size > 0
        ):
            try:
                with self._connect(write=False) as con:
                    tbl_cnt = con.execute(
                        """
                        SELECT COUNT(*) FROM information_schema.tables
                        WHERE table_name IN (
                            'account_meta', 'position_lots', 'orders',
                            'fills', 'equity_snapshots', 'mark_prices'
                        )
                        """
                    ).fetchone()
                    if tbl_cnt and int(tbl_cnt[0]) == 6:
                        has_default = con.execute(
                            "SELECT 1 FROM account_meta WHERE account_id = 'default'"
                        ).fetchone()
                        ord_info = con.execute("PRAGMA table_info('orders')").fetchall()
                        req_type = next((str(r[2]).upper() for r in ord_info if r[1] == "requested_qty"), "")
                        if has_default is not None and req_type == "BIGINT":
                            return
            except duckdb.Error:
                pass

        with self._connect(write=True) as con:
            self._create_tables(con)
            self._ensure_bigint_columns(con)
            row = con.execute(
                "SELECT account_id FROM account_meta WHERE account_id = 'default'"
            ).fetchone()
            if row is None:
                now = _utc_now_iso()
                con.execute(
                    """
                    INSERT INTO account_meta (
                        account_id, initial_capital, cash, realized_pnl,
                        current_date, created_at, updated_at
                    ) VALUES ('default', ?, ?, 0.0, ?, ?, ?)
                    """,
                    [
                        self.default_initial_capital,
                        self.default_initial_capital,
                        self.default_initial_date,
                        now,
                        now,
                    ],
                )
                con.execute(
                    """
                    INSERT OR REPLACE INTO equity_snapshots (
                        snapshot_date, cash, market_value, total_equity,
                        realized_pnl, unrealized_pnl, positions_json, updated_at
                    ) VALUES (?, ?, 0.0, ?, 0.0, 0.0, '[]', ?)
                    """,
                    [
                        self.default_initial_date,
                        self.default_initial_capital,
                        self.default_initial_capital,
                        now,
                    ],
                )

    def _compute_fees(
        self, market: str, side: str, quantity: int, fill_price: float
    ) -> tuple[float, float, float, float, float]:
        notional = _cents(quantity * fill_price)
        if market == "CN":
            commission = _cents(max(5.0, notional * 0.0003))
            transfer_fee = _cents(notional * 0.00001)
            stamp_duty = _cents(notional * 0.0005) if side == "SELL" else 0.0
        else:
            commission = _cents(
                max(
                    self.us_min_commission,
                    notional * self.us_commission_rate + quantity * self.us_per_share_fee,
                )
            )
            transfer_fee = 0.0
            stamp_duty = 0.0
        total_fee = _cents(commission + transfer_fee + stamp_duty)
        return notional, commission, transfer_fee, stamp_duty, total_fee

    def _next_order_seq(self, con: duckdb.DuckDBPyConnection) -> int:
        row = con.execute("SELECT COALESCE(MAX(seq_no), 0) + 1 FROM orders").fetchone()
        return int(row[0]) if row else 1

    def _next_fill_seq(self, con: duckdb.DuckDBPyConnection) -> int:
        row = con.execute("SELECT COALESCE(MAX(seq_no), 0) + 1 FROM fills").fetchone()
        return int(row[0]) if row else 1

    def _infer_bare_symbol_market(
        self,
        con: duckdb.DuckDBPyConnection,
        raw_sym: str,
        *,
        held_markets: dict[str, set[str]],
        mark_markets: dict[str, set[str]],
    ) -> tuple[str, str]:
        norm_cn = _normalize_cn_ticker(raw_sym)
        if norm_cn != raw_sym:
            # Explicit CN exchange prefix/suffix (e.g., sh600900, 600900.SH)
            return norm_cn, "CN"

        held_mkts = held_markets.get(raw_sym, set())
        if held_mkts == {"US"}:
            return raw_sym, "US"
        if held_mkts == {"CN"}:
            return raw_sym, "CN"

        if not held_mkts:
            mark_mkts = mark_markets.get(raw_sym, set())
            if mark_mkts == {"US"}:
                return raw_sym, "US"
            if mark_mkts == {"CN"}:
                return raw_sym, "CN"

            lot_hist = con.execute(
                "SELECT market FROM position_lots WHERE symbol = ? ORDER BY updated_at DESC, created_at DESC LIMIT 1",
                [raw_sym],
            ).fetchone()
            if lot_hist:
                lot_mkt = str(lot_hist[0] or "").strip().upper()
                if lot_mkt in {"CN", "US"}:
                    return raw_sym, lot_mkt

            ord_hist = con.execute(
                "SELECT market FROM orders WHERE symbol = ? AND status = 'FILLED' ORDER BY seq_no DESC LIMIT 1",
                [raw_sym],
            ).fetchone()
            if ord_hist:
                ord_mkt = str(ord_hist[0] or "").strip().upper()
                if ord_mkt in {"CN", "US"}:
                    return raw_sym, ord_mkt

        if _CN_TICKER_RE.fullmatch(raw_sym) or raw_sym.isdigit():
            return raw_sym, "CN"
        return raw_sym, "US"

    def _build_positions_snapshot(
        self,
        con: duckdb.DuckDBPyConnection,
        current_date: str,
        price_overrides: dict[Any, float] | None = None,
    ) -> tuple[list[dict[str, Any]], float, float]:
        lot_rows = con.execute(
            """
            SELECT lot_id, symbol, market, quantity, remaining_qty, buy_date, avg_cost, total_cost, strategy_id
            FROM position_lots
            WHERE remaining_qty > 0
            ORDER BY symbol ASC, market ASC, buy_date ASC, created_at ASC
            """
        ).fetchall()

        mark_rows = con.execute(
            "SELECT symbol, market, close_price, prev_close, price_date FROM mark_prices"
        ).fetchall()
        marks: dict[tuple[str, str], float] = {}
        for r in mark_rows:
            if r[2] is None:
                continue
            try:
                c_px = float(r[2])
            except (TypeError, ValueError):
                continue
            if math.isfinite(c_px) and c_px > 0:
                marks[(r[0], r[1])] = c_px

        held_markets: dict[str, set[str]] = {}
        for r in lot_rows:
            h_sym = str(r[1]).strip().upper()
            h_mkt = str(r[2]).strip().upper()
            if h_mkt in {"CN", "US"}:
                held_markets.setdefault(h_sym, set()).add(h_mkt)
        mark_markets: dict[str, set[str]] = {}
        for sym_m, mkt_m in marks:
            s_u = str(sym_m).strip().upper()
            m_u = str(mkt_m).strip().upper()
            if m_u in {"CN", "US"}:
                mark_markets.setdefault(s_u, set()).add(m_u)

        overrides: dict[Any, float] = {}
        for k, v in (price_overrides or {}).items():
            px = _parse_mark_price_value(v, f"price override for {k!r}")
            if isinstance(k, tuple) and len(k) == 2:
                sym_k = str(k[0]).strip().upper()
                mkt_k = str(k[1]).strip().upper()
                if mkt_k == "CN":
                    sym_k = _normalize_cn_ticker(sym_k)
                overrides[(sym_k, mkt_k)] = px
            else:
                sym_k = str(k).strip().upper()
                if isinstance(v, dict) and v.get("market"):
                    mkt_k = str(v.get("market")).strip().upper()
                    if mkt_k == "CN":
                        sym_k = _normalize_cn_ticker(sym_k)
                    overrides[(sym_k, mkt_k)] = px
                else:
                    sym_resolved, mkt_resolved = self._infer_bare_symbol_market(
                        con,
                        sym_k,
                        held_markets=held_markets,
                        mark_markets=mark_markets,
                    )
                    overrides[(sym_resolved, mkt_resolved)] = px

        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for lot_id, sym, mkt, qty, rem_qty, buy_dt, avg_cost, total_cost, strat_id in lot_rows:
            key = (sym, mkt)
            prop_cost = float(total_cost) * (float(rem_qty) / float(qty))
            sellable = rem_qty if (mkt == "US" or str(buy_dt) < current_date) else 0
            entry = grouped.setdefault(
                key,
                {
                    "symbol": sym,
                    "market": mkt,
                    "quantity": 0,
                    "sellable_qty": 0,
                    "locked_qty": 0,
                    "cost_basis": 0.0,
                    "lots": [],
                },
            )
            entry["quantity"] += int(rem_qty)
            entry["sellable_qty"] += int(sellable)
            entry["locked_qty"] += int(rem_qty) - int(sellable)
            entry["cost_basis"] += prop_cost
            entry["lots"].append(
                {
                    "lot_id": lot_id,
                    "quantity": int(rem_qty),
                    "buy_date": str(buy_dt),
                    "avg_cost": round(float(avg_cost), 6),
                    "strategy_id": strat_id,
                }
            )

        positions: list[dict[str, Any]] = []
        total_mv = 0.0
        total_unrealized = 0.0
        for (sym, mkt), pos in sorted(grouped.items()):
            avg_cost = pos["cost_basis"] / pos["quantity"] if pos["quantity"] > 0 else 0.0
            mark_px = (
                overrides.get((sym, mkt))
                if (sym, mkt) in overrides
                else overrides.get(sym, marks.get((sym, mkt), avg_cost))
            )
            mv = _cents(pos["quantity"] * float(mark_px))
            unrealized = _cents(mv - pos["cost_basis"])
            total_mv += mv
            total_unrealized += unrealized
            positions.append(
                {
                    "symbol": sym,
                    "market": mkt,
                    "quantity": pos["quantity"],
                    "sellable_qty": pos["sellable_qty"],
                    "locked_qty": pos["locked_qty"],
                    "avg_cost": round(avg_cost, 6),
                    "cost_basis": _cents(pos["cost_basis"]),
                    "mark_price": round(float(mark_px), 4),
                    "market_value": mv,
                    "unrealized_pnl": unrealized,
                    "lots": pos["lots"],
                }
            )
        return positions, _cents(total_mv), _cents(total_unrealized)

    def _record_snapshot(
        self,
        con: duckdb.DuckDBPyConnection,
        snapshot_date: str,
        cash: float,
        realized_pnl: float,
    ) -> dict[str, Any]:
        positions, mv, unrealized = self._build_positions_snapshot(con, snapshot_date)
        total_equity = _cents(cash + mv)
        now = _utc_now_iso()
        con.execute(
            """
            INSERT OR REPLACE INTO equity_snapshots (
                snapshot_date, cash, market_value, total_equity,
                realized_pnl, unrealized_pnl, positions_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                snapshot_date,
                _cents(cash),
                mv,
                total_equity,
                _cents(realized_pnl),
                unrealized,
                json.dumps(positions, ensure_ascii=False),
                now,
            ],
        )
        return {
            "snapshot_date": snapshot_date,
            "cash": _cents(cash),
            "market_value": mv,
            "total_equity": total_equity,
            "realized_pnl": _cents(realized_pnl),
            "unrealized_pnl": unrealized,
            "positions": positions,
        }

    def get_simplification_assumptions(self) -> dict[str, Any]:
        """Return explicit documentation of paper-trading market rules and simplifications."""
        return {
            "cn_a_share_rules": {
                "t_plus": 1,
                "buy_lot_size": 100,
                "sell_lot_size": "Multiple of 100, or exact odd-lot remainder closing",
                "price_limit_default": 0.10,
                "price_limit_override_supported": "Pass price_limit_rate (e.g. 0.05 ST, 0.20 ChiNext/STAR)",
                "commission": "0.03% with RMB 5.00 minimum per order (buy & sell)",
                "transfer_fee": "0.001% (both buy & sell)",
                "stamp_duty": "0.05% (sell only)",
                "tick_size": 0.01,
            },
            "us_equity_rules": {
                "t_plus": 0,
                "buy_lot_size": 1,
                "sell_lot_size": 1,
                "price_limit": None,
                "commission_rate": self.us_commission_rate,
                "min_commission": self.us_min_commission,
                "per_share_fee": self.us_per_share_fee,
                "tick_size": 0.01,
            },
            "execution_and_slippage": {
                "slippage_bps": self.slippage_bps,
                "fill_price_model": "BUY fills at ref*(1+slippage_bps/10000), SELL at ref*(1-slippage_bps/10000), rounded to tick; limit orders clamped by limit_price",
                "bar_range_feasibility": "Fill rejected if fill_price lies outside [reference_bar.low, reference_bar.high]",
                "cost_basis_accounting": "FIFO lot consumption including entry fees in lot cost basis",
            },
            "simplifications": [
                "Immediate synchronous fill on submission when order passes all validity and bar feasibility checks (no partial fills or resting order book).",
                "No corporate actions (dividends, splits, rights issues), margin financing, or short selling.",
                "Single cash currency pool matching the simulated account base currency.",
                "Volume liquidity impact beyond bar [low, high] and slippage_bps is not modeled in interactive paper orders.",
            ],
        }

    def get_account_summary(self, price_overrides: dict[Any, float] | None = None) -> dict[str, Any]:
        """Return current account cash, positions, unrealized/realized PnL, and market rules."""
        with self._connect(write=False) as con:
            meta = con.execute(
                """
                SELECT account_id, initial_capital, cash, realized_pnl, current_date, created_at, updated_at
                FROM account_meta WHERE account_id = 'default'
                """
            ).fetchone()
            assert meta is not None
            account_id, init_cap, cash, realized_pnl, cur_date, created_at, updated_at = meta
            positions, mv, unrealized = self._build_positions_snapshot(con, str(cur_date), price_overrides)
            total_equity = _cents(float(cash) + mv)
            total_pnl = _cents(total_equity - float(init_cap))
            return {
                "account_id": account_id,
                "initial_capital": _cents(float(init_cap)),
                "cash": _cents(float(cash)),
                "market_value": mv,
                "total_equity": total_equity,
                "realized_pnl": _cents(float(realized_pnl)),
                "unrealized_pnl": unrealized,
                "total_pnl": total_pnl,
                "current_date": str(cur_date),
                "created_at": str(created_at),
                "updated_at": str(updated_at),
                "positions": positions,
                "market_rules": {
                    "CN": {"t_plus": 1, "buy_lot": 100, "price_limit_rate": 0.10},
                    "US": {"t_plus": 0, "buy_lot": 1, "price_limit_rate": None},
                },
                "simplification_assumptions": self.get_simplification_assumptions(),
            }

    def _persist_rejection(
        self,
        con: duckdb.DuckDBPyConnection,
        *,
        order_id: str,
        seq_no: int,
        trade_date: str,
        symbol: str,
        market: str,
        side: str,
        order_type: str,
        quantity: int,
        limit_price: float | None,
        reject_code: str,
        reason: str,
        strategy_id: str | None,
    ) -> dict[str, Any]:
        now = _utc_now_iso()
        # Clamp only for DuckDB BIGINT column safety if an extreme arbitrary-precision int (e.g. > 2^63-1) is passed.
        db_qty = max(-(2**63), min(2**63 - 1, quantity))
        con.execute(
            """
            INSERT INTO orders (
                order_id, seq_no, trade_date, symbol, market, side, order_type,
                requested_qty, filled_qty, limit_price, fill_price, status,
                reject_code, reject_reason, strategy_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, 'REJECTED', ?, ?, ?, ?)
            """,
            [
                order_id,
                seq_no,
                trade_date,
                symbol,
                market,
                side,
                order_type,
                db_qty,
                limit_price,
                reject_code,
                reason,
                strategy_id,
                now,
            ],
        )
        return {
            "accepted": False,
            "status": "REJECTED",
            "order_id": order_id,
            "seq_no": seq_no,
            "reject_code": reject_code,
            "reason": reason,
            "symbol": symbol,
            "market": market,
            "side": side,
            "order_type": order_type,
            "quantity": quantity,
            "trade_date": trade_date,
        }

    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "market",
        limit_price: float | None = None,
        market: str = "CN",
        trade_date: str | None = None,
        reference_bar: dict[str, Any] | None = None,
        strategy_id: str | None = None,
        price_limit_rate: float | None = None,
    ) -> dict[str, Any]:
        """Validate, execute (or reject and audit-log), and persist a paper order."""
        sym = str(symbol or "").strip().upper()
        mkt = str(market or "").strip().upper()
        if mkt == "CN" and sym:
            cn_match = _CN_TICKER_RE.fullmatch(sym)
            if cn_match:
                sym = cn_match.group(1) or cn_match.group(2)
        side_norm = str(side or "").strip().upper()
        otype_norm = str(order_type or "").strip().lower()
        order_id = f"ord_{uuid4().hex[:12]}"

        with self._connect(write=True) as con:
            meta = con.execute(
                "SELECT cash, realized_pnl, current_date FROM account_meta WHERE account_id = 'default'"
            ).fetchone()
            assert meta is not None
            cash, realized_pnl, cur_date = float(meta[0]), float(meta[1]), str(meta[2])
            trade_date_err: str | None = None
            if trade_date is None:
                eff_date = cur_date
            else:
                try:
                    eff_date = _parse_iso_date(trade_date)
                except ValueError as exc:
                    eff_date = cur_date
                    trade_date_err = f"Invalid trade_date {trade_date!r}: {exc}"
            seq_no = self._next_order_seq(con)

            safe_qty = quantity if (isinstance(quantity, int) and not isinstance(quantity, bool)) else 0
            safe_limit = (
                float(limit_price)
                if (
                    limit_price is not None
                    and not isinstance(limit_price, bool)
                    and isinstance(limit_price, (int, float))
                    and math.isfinite(float(limit_price))
                )
                else None
            )

            def _reject(code: str, msg: str) -> dict[str, Any]:
                return self._persist_rejection(
                    con,
                    order_id=order_id,
                    seq_no=seq_no,
                    trade_date=eff_date,
                    symbol=sym,
                    market=mkt,
                    side=side_norm,
                    order_type=otype_norm,
                    quantity=safe_qty,
                    limit_price=safe_limit,
                    reject_code=code,
                    reason=msg,
                    strategy_id=strategy_id,
                )

            if trade_date_err is not None:
                return _reject("invalid_trade_date", trade_date_err)
            if eff_date < cur_date:
                return _reject(
                    "invalid_trade_date",
                    f"trade_date ({eff_date}) cannot precede current_date ({cur_date})",
                )
            if not sym:
                return _reject("invalid_symbol", "Symbol must be non-empty")
            if mkt not in {"CN", "US"}:
                return _reject("invalid_market", f"Unsupported market {market!r}; must be CN or US")
            if side_norm not in {"BUY", "SELL"}:
                return _reject("invalid_side", f"Invalid side {side!r}; must be BUY or SELL")
            if otype_norm not in {"market", "limit"}:
                return _reject("invalid_order_type", f"Invalid order_type {order_type!r}; must be market or limit")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                return _reject("invalid_quantity", f"Quantity must be a positive integer, got {quantity!r}")
            if quantity > 1_000_000_000_000_000:
                return _reject(
                    "invalid_quantity",
                    f"Quantity {quantity} exceeds maximum supported order size",
                )
            if mkt == "CN" and side_norm == "BUY" and quantity % 100 != 0:
                return _reject("invalid_cn_buy_lot", f"CN BUY quantity must be a multiple of 100, got {quantity}")
            if otype_norm == "limit":
                if (
                    limit_price is None
                    or isinstance(limit_price, bool)
                    or not isinstance(limit_price, (int, float))
                    or not math.isfinite(float(limit_price))
                    or float(limit_price) <= 0
                ):
                    return _reject("invalid_limit_price", f"Limit order requires positive finite limit_price, got {limit_price!r}")
            if price_limit_rate is not None:
                if (
                    isinstance(price_limit_rate, bool)
                    or not isinstance(price_limit_rate, (int, float))
                    or not math.isfinite(float(price_limit_rate))
                    or float(price_limit_rate) < 0.0
                    or float(price_limit_rate) >= 1.0
                ):
                    return _reject(
                        "invalid_price_limit_rate",
                        f"price_limit_rate must be a finite number in [0.0, 1.0), got {price_limit_rate!r}",
                    )

            bar_open = bar_high = bar_low = bar_close = bar_price = bar_prev_close = None
            if reference_bar is not None:
                if not isinstance(reference_bar, dict):
                    return _reject("invalid_reference_bar", "reference_bar must be a dict")
                parsed_bar: dict[str, float] = {}
                for fld in ("open", "high", "low", "close", "price", "prev_close"):
                    if fld in reference_bar and reference_bar[fld] is not None:
                        raw_v = reference_bar[fld]
                        if isinstance(raw_v, bool) or not isinstance(raw_v, (int, float)) or not math.isfinite(float(raw_v)) or float(raw_v) <= 0:
                            return _reject("invalid_reference_bar", f"reference_bar.{fld} must be positive and finite")
                        parsed_bar[fld] = float(raw_v)
                bar_open = parsed_bar.get("open")
                bar_high = parsed_bar.get("high")
                bar_low = parsed_bar.get("low")
                bar_close = parsed_bar.get("close")
                bar_price = parsed_bar.get("price")
                bar_prev_close = parsed_bar.get("prev_close")
                if bar_low is not None and bar_high is not None and bar_low > bar_high:
                    return _reject("invalid_reference_bar", "reference_bar low > high")
                for px_val in (bar_open, bar_close, bar_price):
                    if px_val is not None:
                        if bar_low is not None and px_val < bar_low - 1e-12:
                            return _reject("invalid_reference_bar", "reference_bar price below low")
                        if bar_high is not None and px_val > bar_high + 1e-12:
                            return _reject("invalid_reference_bar", "reference_bar price above high")

            mark_row = con.execute(
                "SELECT close_price, prev_close FROM mark_prices WHERE symbol = ? AND market = ?",
                [sym, mkt],
            ).fetchone()
            mark_close: float | None = None
            mark_prev: float | None = None
            if mark_row:
                if mark_row[0] is not None:
                    try:
                        cand_c = float(mark_row[0])
                        if math.isfinite(cand_c) and cand_c > 0:
                            mark_close = cand_c
                    except (TypeError, ValueError):
                        mark_close = None
                if mark_row[1] is not None:
                    try:
                        cand_p = float(mark_row[1])
                        if math.isfinite(cand_p) and cand_p > 0:
                            mark_prev = cand_p
                    except (TypeError, ValueError):
                        mark_prev = None

            fill_ref = (
                bar_open
                if bar_open is not None
                else (
                    bar_price
                    if bar_price is not None
                    else (bar_close if bar_close is not None else mark_close)
                )
            )
            if fill_ref is None:
                if otype_norm == "market":
                    return _reject("missing_reference_price", f"No reference_bar or mark price available for {sym}")
                assert safe_limit is not None
                fill_ref = safe_limit

            prev_close = bar_prev_close if bar_prev_close is not None else mark_prev
            raw_slipped = _slipped_price(fill_ref, self.slippage_rate, side_norm, _TICK_SIZE)

            # CN price limit check
            upper_lim = lower_lim = None
            if mkt == "CN" and prev_close is not None:
                lim_rate = float(price_limit_rate) if price_limit_rate is not None else 0.10
                upper_lim = _rounded_price(prev_close * (1.0 + lim_rate), _TICK_SIZE)
                lower_lim = _rounded_price(prev_close * (1.0 - lim_rate), _TICK_SIZE)
                if otype_norm == "limit" and safe_limit is not None:
                    if safe_limit > upper_lim + 1e-12 or safe_limit < lower_lim - 1e-12:
                        return _reject(
                            "price_limit_exceeded",
                            f"Limit price {safe_limit} exceeds CN price limit [{lower_lim}, {upper_lim}]",
                        )
                if (side_norm == "BUY" and fill_ref >= upper_lim - 1e-12) or (
                    side_norm == "SELL" and fill_ref <= lower_lim + 1e-12
                ):
                    return _reject(
                        "price_limit_exceeded",
                        f"Reference price {fill_ref} is locked at CN price limit [{lower_lim}, {upper_lim}]",
                    )

            # Determine fill price & check limit-price feasibility
            if otype_norm == "limit":
                assert safe_limit is not None
                if side_norm == "BUY":
                    if safe_limit + 1e-12 < fill_ref or (bar_low is not None and safe_limit + 1e-12 < bar_low):
                        return _reject(
                            "limit_price_not_met",
                            f"BUY limit_price {safe_limit} below reference/bar price {fill_ref}",
                        )
                    fill_price = _rounded_price(min(safe_limit, raw_slipped), _TICK_SIZE)
                else:
                    if safe_limit - 1e-12 > fill_ref or (bar_high is not None and safe_limit - 1e-12 > bar_high):
                        return _reject(
                            "limit_price_not_met",
                            f"SELL limit_price {safe_limit} above reference/bar price {fill_ref}",
                        )
                    fill_price = _rounded_price(max(safe_limit, raw_slipped), _TICK_SIZE)
            else:
                fill_price = raw_slipped

            if upper_lim is not None and lower_lim is not None:
                if fill_price > upper_lim + 1e-12 or fill_price < lower_lim - 1e-12:
                    return _reject(
                        "price_limit_exceeded",
                        f"Fill price {fill_price} exceeds CN price limit [{lower_lim}, {upper_lim}]",
                    )

            if bar_low is not None and fill_price < bar_low - 1e-12:
                return _reject(
                    "outside_bar_range",
                    f"Fill price {fill_price} is below bar low {bar_low}",
                )
            if bar_high is not None and fill_price > bar_high + 1e-12:
                return _reject(
                    "outside_bar_range",
                    f"Fill price {fill_price} is above bar high {bar_high}",
                )

            notional, commission, transfer_fee, stamp_duty, total_fee = self._compute_fees(
                mkt, side_norm, quantity, fill_price
            )

            # Position & lot checks for SELL; cash check for BUY
            if side_norm == "SELL":
                lots = con.execute(
                    """
                    SELECT lot_id, quantity, remaining_qty, buy_date, total_cost
                    FROM position_lots
                    WHERE symbol = ? AND market = ? AND remaining_qty > 0
                    ORDER BY buy_date ASC, created_at ASC
                    """,
                    [sym, mkt],
                ).fetchall()
                total_held = sum(int(r[2]) for r in lots)
                if quantity > total_held:
                    return _reject(
                        "insufficient_position",
                        f"Requested sell {quantity} exceeds held quantity {total_held} for {sym}",
                    )
                if mkt == "CN":
                    sellable = sum(int(r[2]) for r in lots if str(r[3]) < eff_date)
                    if quantity > sellable:
                        return _reject(
                            "t_plus_one_restricted",
                            f"CN T+1 restriction: sellable_qty={sellable} on {eff_date}, requested={quantity}",
                        )
                    if quantity % 100 != 0:
                        rem_odd = sellable % 100
                        if rem_odd == 0 or quantity not in {sellable, rem_odd}:
                            return _reject(
                                "invalid_cn_sell_lot",
                                f"CN SELL odd-lot {quantity} allowed only when closing odd remainder (sellable={sellable})",
                            )
            else:
                total_required = _cents(notional + total_fee)
                if total_required > cash + _EPS:
                    return _reject(
                        "insufficient_cash",
                        f"Required cash {total_required:.2f} (incl. fee {total_fee:.2f}) exceeds available {cash:.2f}",
                    )

            # Execute valid order
            now = _utc_now_iso()
            fill_id = f"fill_{uuid4().hex[:12]}"
            fill_seq = self._next_fill_seq(con)
            slippage_cost = _cents(abs(fill_price - fill_ref) * quantity)
            fill_realized_pnl: float | None = None

            if side_norm == "BUY":
                total_outflow = _cents(notional + total_fee)
                new_cash = _cents(cash - total_outflow)
                net_cash_delta = -total_outflow
                avg_cost = total_outflow / float(quantity)
                lot_id = f"lot_{uuid4().hex[:12]}"
                con.execute(
                    """
                    INSERT INTO position_lots (
                        lot_id, symbol, market, quantity, remaining_qty, buy_date,
                        avg_cost, total_cost, strategy_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        lot_id,
                        sym,
                        mkt,
                        quantity,
                        quantity,
                        eff_date,
                        avg_cost,
                        total_outflow,
                        strategy_id,
                        now,
                        now,
                    ],
                )
            else:
                net_proceeds = _cents(notional - total_fee)
                new_cash = _cents(cash + net_proceeds)
                net_cash_delta = net_proceeds

                lots = con.execute(
                    """
                    SELECT lot_id, quantity, remaining_qty, buy_date, total_cost
                    FROM position_lots
                    WHERE symbol = ? AND market = ? AND remaining_qty > 0
                    ORDER BY buy_date ASC, created_at ASC
                    """,
                    [sym, mkt],
                ).fetchall()
                rem_to_sell = quantity
                consumed_cost = 0.0
                for lot_id, orig_qty, rem_qty, buy_dt, lot_total_cost in lots:
                    if rem_to_sell <= 0:
                        break
                    if mkt == "CN" and str(buy_dt) >= eff_date:
                        continue
                    take = min(rem_to_sell, int(rem_qty))
                    consumed_cost += float(lot_total_cost) * (float(take) / float(orig_qty))
                    new_rem = int(rem_qty) - take
                    rem_to_sell -= take
                    con.execute(
                        "UPDATE position_lots SET remaining_qty = ?, updated_at = ? WHERE lot_id = ?",
                        [new_rem, now, lot_id],
                    )
                fill_realized_pnl = _cents(net_proceeds - consumed_cost)
                realized_pnl = _cents(realized_pnl + fill_realized_pnl)

            con.execute(
                """
                UPDATE account_meta
                SET cash = ?, realized_pnl = ?, updated_at = ?
                WHERE account_id = 'default'
                """,
                [new_cash, realized_pnl, now],
            )
            con.execute(
                """
                INSERT INTO orders (
                    order_id, seq_no, trade_date, symbol, market, side, order_type,
                    requested_qty, filled_qty, limit_price, fill_price, status,
                    reject_code, reject_reason, strategy_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'FILLED', NULL, NULL, ?, ?)
                """,
                [
                    order_id,
                    seq_no,
                    eff_date,
                    sym,
                    mkt,
                    side_norm,
                    otype_norm,
                    quantity,
                    quantity,
                    safe_limit,
                    fill_price,
                    strategy_id,
                    now,
                ],
            )
            con.execute(
                """
                INSERT INTO fills (
                    fill_id, order_id, seq_no, trade_date, symbol, market, side,
                    quantity, reference_price, fill_price, notional, slippage_cost,
                    commission, transfer_fee, stamp_duty, total_fee,
                    net_cash_delta, realized_pnl, cash_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    fill_id,
                    order_id,
                    fill_seq,
                    eff_date,
                    sym,
                    mkt,
                    side_norm,
                    quantity,
                    fill_ref,
                    fill_price,
                    notional,
                    slippage_cost,
                    commission,
                    transfer_fee,
                    stamp_duty,
                    total_fee,
                    net_cash_delta,
                    fill_realized_pnl,
                    new_cash,
                    now,
                ],
            )

            latest_close = bar_close if bar_close is not None else (bar_price if bar_price is not None else fill_price)
            con.execute(
                """
                INSERT OR REPLACE INTO mark_prices (symbol, market, price_date, close_price, prev_close)
                VALUES (?, ?, ?, ?, ?)
                """,
                [sym, mkt, eff_date, latest_close, prev_close],
            )
            self._record_snapshot(con, eff_date, new_cash, realized_pnl)

            return {
                "accepted": True,
                "status": "FILLED",
                "order_id": order_id,
                "fill_id": fill_id,
                "seq_no": seq_no,
                "symbol": sym,
                "market": mkt,
                "side": side_norm,
                "order_type": otype_norm,
                "quantity": quantity,
                "reference_price": fill_ref,
                "fill_price": fill_price,
                "notional": notional,
                "slippage_cost": slippage_cost,
                "commission": commission,
                "transfer_fee": transfer_fee,
                "stamp_duty": stamp_duty,
                "total_fee": total_fee,
                "net_cash_delta": net_cash_delta,
                "realized_pnl": fill_realized_pnl,
                "cash_after": new_cash,
                "trade_date": eff_date,
                "strategy_id": strategy_id,
            }

    def advance_day(
        self,
        new_date: str | date,
        mark_prices: dict[Any, Any] | None = None,
    ) -> dict[str, Any]:
        """Advance ledger date, unlock prior-day CN T+1 lots, update marks, and snapshot equity."""
        target_date = _parse_iso_date(new_date)
        with self._connect(write=True) as con:
            meta = con.execute(
                "SELECT cash, realized_pnl, current_date FROM account_meta WHERE account_id = 'default'"
            ).fetchone()
            assert meta is not None
            cash, realized_pnl, cur_date = float(meta[0]), float(meta[1]), str(meta[2])
            if target_date < cur_date:
                raise ValueError(f"new_date ({target_date}) cannot precede current_date ({cur_date})")

            existing_marks = con.execute(
                "SELECT symbol, market, price_date, close_price, prev_close FROM mark_prices"
            ).fetchall()
            mark_map: dict[tuple[str, str], dict[str, Any]] = {}
            for r in existing_marks:
                if r[3] is None:
                    continue
                try:
                    c_px = float(r[3])
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(c_px) or c_px <= 0:
                    continue
                p_px: float | None = None
                if r[4] is not None:
                    try:
                        cand_p = float(r[4])
                        if math.isfinite(cand_p) and cand_p > 0:
                            p_px = cand_p
                    except (TypeError, ValueError):
                        p_px = None
                mark_map[(r[0], r[1])] = {
                    "price_date": str(r[2]),
                    "close_price": c_px,
                    "prev_close": p_px,
                }

            if target_date > cur_date:
                for (sym, mkt), info in mark_map.items():
                    if info["price_date"] < target_date:
                        info["prev_close"] = info["close_price"]
                        info["price_date"] = target_date

            # Parse incoming mark_prices
            held_rows = con.execute(
                "SELECT DISTINCT symbol, market FROM position_lots WHERE remaining_qty > 0"
            ).fetchall()
            held_markets: dict[str, set[str]] = {}
            for r in held_rows:
                h_sym = str(r[0]).strip().upper()
                h_mkt = str(r[1]).strip().upper()
                if h_mkt in {"CN", "US"}:
                    held_markets.setdefault(h_sym, set()).add(h_mkt)
            mark_markets: dict[str, set[str]] = {}
            for sym_m, mkt_m in mark_map:
                s_u = str(sym_m).strip().upper()
                m_u = str(mkt_m).strip().upper()
                if m_u in {"CN", "US"}:
                    mark_markets.setdefault(s_u, set()).add(m_u)

            for raw_k, raw_v in (mark_prices or {}).items():
                explicit_mkt: str | None = None
                if isinstance(raw_k, tuple) and len(raw_k) == 2:
                    sym = str(raw_k[0]).strip().upper()
                    explicit_mkt = str(raw_k[1]).strip().upper()
                else:
                    sym = str(raw_k).strip().upper()
                if isinstance(raw_v, dict):
                    if raw_v.get("market"):
                        explicit_mkt = str(raw_v.get("market")).strip().upper()
                    explicit_prev = raw_v.get("prev_close")
                else:
                    explicit_prev = None

                if explicit_mkt:
                    mkt = explicit_mkt
                    if mkt == "CN":
                        sym = _normalize_cn_ticker(sym)
                else:
                    sym, mkt = self._infer_bare_symbol_market(
                        con,
                        sym,
                        held_markets=held_markets,
                        mark_markets=mark_markets,
                    )

                if mkt not in {"CN", "US"}:
                    raise ValueError(f"Unsupported market {mkt!r} for {sym}")
                close_px = _parse_mark_price_value(raw_v, f"mark price for {sym}")
                prev_info = mark_map.get((sym, mkt))
                if explicit_prev is not None:
                    prev_close: float | None = _parse_mark_price_value(
                        explicit_prev, f"prev_close for {sym}"
                    )
                else:
                    prev_close = (
                        prev_info["prev_close"]
                        if prev_info and prev_info["price_date"] == target_date
                        else (prev_info["close_price"] if prev_info else None)
                    )
                mark_map[(sym, mkt)] = {
                    "price_date": target_date,
                    "close_price": close_px,
                    "prev_close": prev_close,
                }

            for (sym, mkt), info in mark_map.items():
                con.execute(
                    """
                    INSERT OR REPLACE INTO mark_prices (symbol, market, price_date, close_price, prev_close)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [sym, mkt, info["price_date"], info["close_price"], info["prev_close"]],
                )

            now = _utc_now_iso()
            con.execute(
                "UPDATE account_meta SET current_date = ?, updated_at = ? WHERE account_id = 'default'",
                [target_date, now],
            )
            snap = self._record_snapshot(con, target_date, cash, realized_pnl)
            return {
                "current_date": target_date,
                "cash": snap["cash"],
                "market_value": snap["market_value"],
                "total_equity": snap["total_equity"],
                "realized_pnl": snap["realized_pnl"],
                "unrealized_pnl": snap["unrealized_pnl"],
                "positions": snap["positions"],
            }

    def reset_account(
        self,
        initial_capital: float = 100000.0,
        initial_date: str | None = None,
    ) -> dict[str, Any]:
        """Atomically wipe all positions/orders/fills/history and reset account."""
        if (
            isinstance(initial_capital, bool)
            or not isinstance(initial_capital, (int, float))
            or not math.isfinite(float(initial_capital))
            or float(initial_capital) <= 0
        ):
            raise ValueError("initial_capital must be positive and finite")
        cap = _cents(float(initial_capital))
        init_dt = (
            _parse_iso_date(initial_date)
            if initial_date is not None
            else self.default_initial_date
        )
        now = _utc_now_iso()
        with self._connect(write=True) as con:
            con.execute("DELETE FROM position_lots")
            con.execute("DELETE FROM orders")
            con.execute("DELETE FROM fills")
            con.execute("DELETE FROM equity_snapshots")
            con.execute("DELETE FROM mark_prices")
            con.execute(
                """
                INSERT OR REPLACE INTO account_meta (
                    account_id, initial_capital, cash, realized_pnl,
                    current_date, created_at, updated_at
                ) VALUES ('default', ?, ?, 0.0, ?, ?, ?)
                """,
                [cap, cap, init_dt, now, now],
            )
            con.execute(
                """
                INSERT INTO equity_snapshots (
                    snapshot_date, cash, market_value, total_equity,
                    realized_pnl, unrealized_pnl, positions_json, updated_at
                ) VALUES (?, ?, 0.0, ?, 0.0, 0.0, '[]', ?)
                """,
                [init_dt, cap, cap, now],
            )
        return self.get_account_summary()

    def get_orders(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return order audit records (both FILLED and REJECTED) ordered by seq_no DESC."""
        with self._connect(write=False) as con:
            sql = """
                SELECT order_id, seq_no, trade_date, symbol, market, side, order_type,
                       requested_qty, filled_qty, limit_price, fill_price, status,
                       reject_code, reject_reason, strategy_id, created_at
                FROM orders
                ORDER BY seq_no DESC
            """
            if limit is not None:
                sql += f" LIMIT {int(limit)}"
            rows = con.execute(sql).fetchall()
            return [
                {
                    "order_id": r[0],
                    "seq_no": int(r[1]),
                    "trade_date": str(r[2]),
                    "symbol": r[3],
                    "market": r[4],
                    "side": r[5],
                    "order_type": r[6],
                    "requested_qty": int(r[7]),
                    "filled_qty": int(r[8]),
                    "limit_price": float(r[9]) if r[9] is not None else None,
                    "fill_price": float(r[10]) if r[10] is not None else None,
                    "status": r[11],
                    "reject_code": r[12],
                    "reject_reason": r[13],
                    "strategy_id": r[14],
                    "created_at": str(r[15]),
                }
                for r in rows
            ]

    def get_fills(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return executed fill records ordered by seq_no DESC."""
        with self._connect(write=False) as con:
            sql = """
                SELECT fill_id, order_id, seq_no, trade_date, symbol, market, side,
                       quantity, reference_price, fill_price, notional, slippage_cost,
                       commission, transfer_fee, stamp_duty, total_fee,
                       net_cash_delta, realized_pnl, cash_after, created_at
                FROM fills
                ORDER BY seq_no DESC
            """
            if limit is not None:
                sql += f" LIMIT {int(limit)}"
            rows = con.execute(sql).fetchall()
            return [
                {
                    "fill_id": r[0],
                    "order_id": r[1],
                    "seq_no": int(r[2]),
                    "trade_date": str(r[3]),
                    "symbol": r[4],
                    "market": r[5],
                    "side": r[6],
                    "quantity": int(r[7]),
                    "reference_price": float(r[8]),
                    "fill_price": float(r[9]),
                    "notional": float(r[10]),
                    "slippage_cost": float(r[11]),
                    "commission": float(r[12]),
                    "transfer_fee": float(r[13]),
                    "stamp_duty": float(r[14]),
                    "total_fee": float(r[15]),
                    "net_cash_delta": float(r[16]),
                    "realized_pnl": float(r[17]) if r[17] is not None else None,
                    "cash_after": float(r[18]),
                    "created_at": str(r[19]),
                }
                for r in rows
            ]

    def get_equity_history(self) -> list[dict[str, Any]]:
        """Return daily equity snapshot history ordered chronologically."""
        with self._connect(write=False) as con:
            rows = con.execute(
                """
                SELECT snapshot_date, cash, market_value, total_equity,
                       realized_pnl, unrealized_pnl, positions_json, updated_at
                FROM equity_snapshots
                ORDER BY snapshot_date ASC
                """
            ).fetchall()
            return [
                {
                    "date": str(r[0]),
                    "snapshot_date": str(r[0]),
                    "cash": float(r[1]),
                    "market_value": float(r[2]),
                    "equity": float(r[3]),
                    "total_equity": float(r[3]),
                    "realized_pnl": float(r[4]),
                    "unrealized_pnl": float(r[5]),
                    "positions": json.loads(r[6]) if r[6] else [],
                    "updated_at": str(r[7]),
                }
                for r in rows
            ]
