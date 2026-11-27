"""FastAPI REST & SSE backend integrating M1 storage/ingest, M2 quant/ledger, and M3 Pi agent."""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
import duckdb
import jevdb
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, BeforeValidator, Field, field_validator, model_validator

from stock_research.agent.pi_runner import run_research_query
from stock_research.config import get_settings
from stock_research.quant.backtest import (
    _to_date as _backtest_to_date,
    compare_strategies,
    load_bars_from_duckdb,
    run_backtest,
)
from stock_research.quant.ledger import PaperAccountService
from stock_research.quant.risk import _extract_price_series, compute_portfolio_risk
from stock_research.quant.strategies import (
    StrategyNotFoundError,
    StrategySpec,
    ensure_builtin_strategies,
    list_strategies,
    save_strategy,
)
from stock_research.storage.ingest.akshare_cn import (
    ingest_a_share_sector,
    ingest_a_share_stock,
    normalize_cn_symbol,
)
from stock_research.storage.ingest.finnhub_us import (
    ingest_finnhub_earnings,
    ingest_finnhub_news,
)
from stock_research.storage.ingest.sec_edgar import ingest_sec_edgar
from stock_research.storage.ingest.yfinance_us import ingest_us_stock
from stock_research.storage.schema import db_session, ensure_baseline_seeded

_MARKET_DB_LOCK = threading.RLock()
_WEB_DIST_DIR = Path(__file__).resolve().parents[1] / "web" / "dist"


def _resolve_market_db_path() -> str:
    env_db = os.environ.get("JEVDB_MARKET_DB")
    if env_db:
        return env_db
    return str(get_settings().db_path)


def _ensure_baseline_seeded(con: duckdb.DuckDBPyConnection) -> None:
    ensure_baseline_seeded(con, seed_strategies=True)


def _reject_bool_num(v: Any) -> Any:
    if isinstance(v, bool):
        raise ValueError("must be a finite number, not a boolean")
    return v


def _normalize_market_symbol(sym: str, mkt: str | None) -> tuple[str, str]:
    clean_sym = sym.strip().upper()
    if mkt in (None, "CN"):
        try:
            code6, _, _ = normalize_cn_symbol(clean_sym)
            return code6, "CN"
        except ValueError:
            pass
    return clean_sym, mkt or ("CN" if clean_sym.isdigit() else "US")


def _normalize_symbol_for_market(sym: str, mkt: str | None) -> str:
    clean = str(sym or "").strip().upper()
    if not clean:
        return ""
    mkt_u = str(mkt or "").strip().upper() or None
    norm_sym, _ = _normalize_market_symbol(clean, mkt_u)
    return norm_sym


def _json_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, str, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, Decimal):
        val = float(obj)
        return val if math.isfinite(val) else None
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    return str(obj)


def _rows_to_dicts(cursor_desc: Any, rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    cols = [col[0] for col in (cursor_desc or [])]
    return [_json_safe(dict(zip(cols, row))) for row in rows]


def _lookup_latest_bar_unlocked(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    market: str,
    *,
    trade_date: str | None = None,
) -> dict[str, float] | None:
    mkt_in = str(market or "").strip().upper()
    norm_sym, norm_mkt = _normalize_market_symbol(symbol, mkt_in or None)
    end_dt = (
        trade_date.strip()[:10]
        if isinstance(trade_date, str) and len(trade_date.strip()) >= 10
        else None
    )
    bars = load_bars_from_duckdb([norm_sym], end_date=end_dt, market=norm_mkt, con=con)
    if not bars and end_dt:
        bars = load_bars_from_duckdb([norm_sym], market=norm_mkt, con=con)
    if not bars:
        return None
    latest = bars[-1]
    prev_c = float(bars[-2]["close"]) if len(bars) > 1 else float(latest["close"])
    return {
        "open": float(latest["open"]),
        "high": float(latest["high"]),
        "low": float(latest["low"]),
        "close": float(latest["close"]),
        "prev_close": prev_c,
    }


def _auto_fetch_missing_bars_for_window(
    symbols: list[str],
    market: str,
    start_date: str,
    end_date: str,
    db_path: Path,
) -> list[str]:
    """Ensure local DuckDB covers [start_date, end_date] for each symbol, fetching missing ranges."""
    start_dt = _backtest_to_date(start_date)
    end_dt = _backtest_to_date(end_date)
    if start_dt > end_dt or not symbols:
        return []
    eff_end = min(end_dt, date.today())
    mkt = str(market or "CN").strip().upper()
    if mkt not in {"CN", "US"}:
        return []

    missing: list[str] = []
    with db_session(db_path) as con:
        _ensure_baseline_seeded(con)
        for sym in symbols:
            row = con.execute(
                """
                SELECT COUNT(*), MIN(trade_date), MAX(trade_date)
                FROM market_bars
                WHERE UPPER(symbol) = ?
                  AND UPPER(market) = ?
                  AND trade_date >= ?
                  AND trade_date <= ?
                """,
                [sym.upper(), mkt, start_dt, end_dt],
            ).fetchone()
            cnt = int(row[0] or 0) if row else 0
            min_d = row[1] if row else None
            max_d = row[2] if row else None
            span_days = (eff_end - start_dt).days
            needs_fetch = cnt == 0
            if not needs_fetch and span_days >= 14:
                if cnt < 2 or min_d is None or max_d is None:
                    needs_fetch = True
                elif (min_d - start_dt).days > 10 or (eff_end - max_d).days > 10:
                    needs_fetch = True
            if needs_fetch:
                missing.append(sym)

    fetched: list[str] = []
    for sym in missing:
        try:
            if mkt == "CN":
                res = ingest_a_share_stock(
                    symbol=sym,
                    start_date=start_date,
                    end_date=end_date,
                    include_fundamentals=False,
                    include_news=False,
                    db_path=db_path,
                )
            else:
                res = ingest_us_stock(
                    symbol=sym,
                    start_date=start_date,
                    end_date=end_date,
                    include_fundamentals=False,
                    include_news=False,
                    db_path=db_path,
                )
            if int(res.get("bars") or 0) > 0:
                fetched.append(sym)
        except Exception:
            # Degrade gracefully to whatever local bars already exist
            continue
    return fetched


# ---------------------------------------------------------------------------
# Pydantic Request Schemas
# ---------------------------------------------------------------------------


class AgentQueryRequest(BaseModel):
    prompt: str
    mode: str = "auto"
    session_id: str | None = None

    @field_validator("prompt")
    @classmethod
    def _validate_prompt(cls, value: str) -> str:
        clean = str(value or "").strip()
        if not clean:
            raise ValueError("Research prompt must be non-empty")
        return clean


_StrictInt = Annotated[int, BeforeValidator(_reject_bool_num)]
_StrictFloat = Annotated[float, BeforeValidator(_reject_bool_num)]


class MarketIngestRequest(BaseModel):
    kind: Literal[
        "a_share_sector",
        "a_share_stock",
        "us_stock",
        "sec_edgar",
        "finnhub_news",
        "finnhub_earnings",
    ]
    symbol: str | None = None
    sector: str | None = None
    days: _StrictInt = Field(default=30, ge=1, le=3650)
    period: str = "6mo"
    from_date: str | None = None
    to_date: str | None = None
    category: str = "general"
    force: bool = False
    top_constituents: _StrictInt = Field(default=5, ge=1, le=30)

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> "MarketIngestRequest":
        if self.kind in ("a_share_stock", "us_stock", "sec_edgar"):
            if not (self.symbol and self.symbol.strip()):
                raise ValueError(f"'symbol' is required for kind={self.kind!r}")
            self.symbol = self.symbol.strip().upper()
        elif self.kind == "a_share_sector":
            if self.sector is not None and not self.sector.strip():
                raise ValueError("'sector' must be non-empty when provided")
            self.sector = (self.sector or "电力").strip()
        elif self.kind == "finnhub_news":
            if self.symbol is not None:
                self.symbol = self.symbol.strip().upper() or None
        elif self.kind == "finnhub_earnings":
            if self.symbol is not None:
                self.symbol = self.symbol.strip().upper() or None
        return self


class QuantBacktestRequest(BaseModel):
    strategy_id: str = Field(..., min_length=1)
    symbols: list[str] = Field(..., min_length=1)
    start_date: str = Field(..., min_length=4)
    end_date: str = Field(..., min_length=4)
    initial_capital: _StrictFloat = Field(default=100000.0, gt=0.0, le=1e12)
    seed: _StrictInt = 42
    market: str = "CN"
    auto_fetch: bool = False

    @field_validator("start_date", "end_date")
    @classmethod
    def _validate_iso_date(cls, v: str) -> str:
        _backtest_to_date(v)
        return v

    @field_validator("initial_capital")
    @classmethod
    def _validate_finite_cap(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("initial_capital must be a finite number")
        return v

    @field_validator("strategy_id")
    @classmethod
    def _strip_strategy_id(cls, v: str) -> str:
        clean = v.strip()
        if not clean:
            raise ValueError("strategy_id must be non-empty")
        return clean

    @field_validator("symbols")
    @classmethod
    def _clean_symbols(cls, v: list[str]) -> list[str]:
        cleaned = [str(s).strip().upper() for s in v if str(s).strip()]
        if not cleaned:
            raise ValueError("symbols must contain at least one symbol")
        return cleaned

    @model_validator(mode="after")
    def _normalize_symbols_for_market(self) -> "QuantBacktestRequest":
        mkt_u = str(self.market or "CN").strip().upper() or "CN"
        self.symbols = list(
            dict.fromkeys(_normalize_symbol_for_market(s, mkt_u) for s in self.symbols)
        )
        return self


class QuantCompareRequest(BaseModel):
    strategy_ids: list[str] = Field(..., min_length=1)
    symbols: list[str] = Field(..., min_length=1)
    start_date: str = Field(..., min_length=4)
    end_date: str = Field(..., min_length=4)
    initial_capital: _StrictFloat = Field(default=100000.0, gt=0.0, le=1e12)
    market: str = "CN"
    seed: _StrictInt = 42
    sort_by: str = "sharpe_ratio"
    auto_fetch: bool = False

    @field_validator("start_date", "end_date")
    @classmethod
    def _validate_iso_date(cls, v: str) -> str:
        _backtest_to_date(v)
        return v

    @field_validator("initial_capital")
    @classmethod
    def _validate_finite_cap(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("initial_capital must be a finite number")
        return v

    @field_validator("strategy_ids")
    @classmethod
    def _clean_strategy_ids(cls, v: list[str]) -> list[str]:
        cleaned = [str(s).strip() for s in v if str(s).strip()]
        if not cleaned:
            raise ValueError("strategy_ids must contain at least one strategy_id")
        return cleaned

    @field_validator("symbols")
    @classmethod
    def _clean_symbols(cls, v: list[str]) -> list[str]:
        cleaned = [str(s).strip().upper() for s in v if str(s).strip()]
        if not cleaned:
            raise ValueError("symbols must contain at least one symbol")
        return cleaned

    @model_validator(mode="after")
    def _normalize_symbols_for_market(self) -> "QuantCompareRequest":
        mkt_u = str(self.market or "CN").strip().upper() or "CN"
        self.symbols = list(
            dict.fromkeys(_normalize_symbol_for_market(s, mkt_u) for s in self.symbols)
        )
        return self


class PortfolioRiskRequest(BaseModel):
    holdings: list[dict[str, Any]] = Field(default_factory=list)
    price_history: dict[str, Any] | None = None
    benchmark_history: list[Any] | None = None
    days: _StrictInt = Field(default=120, ge=2, le=3650)
    start_date: str | None = None
    end_date: str | None = None
    confidence: _StrictFloat = Field(default=0.95, gt=0.5, lt=1.0)

    @field_validator("start_date", "end_date")
    @classmethod
    def _validate_optional_iso_date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        clean = str(v).strip()
        if not clean:
            return None
        _backtest_to_date(clean)
        return clean

    @model_validator(mode="after")
    def _validate_date_order(self) -> "PortfolioRiskRequest":
        if self.start_date and self.end_date:
            if _backtest_to_date(self.start_date) > _backtest_to_date(self.end_date):
                raise ValueError(
                    f"start_date ({self.start_date}) must not exceed end_date ({self.end_date})"
                )
        return self

    @field_validator("confidence")
    @classmethod
    def _validate_finite_conf(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("confidence must be a finite number")
        return v

    @field_validator("price_history")
    @classmethod
    def _validate_price_history(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return None
        for sym, entries in v.items():
            try:
                _extract_price_series(entries)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"price_history[{sym!r}]: {exc}") from exc
        return v

    @field_validator("benchmark_history")
    @classmethod
    def _validate_benchmark_history(cls, v: list[Any] | None) -> list[Any] | None:
        if v is None:
            return None
        try:
            _extract_price_series(v)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"benchmark_history: {exc}") from exc
        return v

    @field_validator("holdings")
    @classmethod
    def _validate_holdings(cls, holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for idx, h in enumerate(holdings):
            if not isinstance(h, dict):
                raise ValueError(f"holdings[{idx}] must be an object")
            for key in ("weight", "quantity", "market_value", "value"):
                if key not in h or h[key] is None:
                    continue
                val = h[key]
                if isinstance(val, str) and not val.strip():
                    continue
                if isinstance(val, bool):
                    raise ValueError(f"holdings[{idx}].{key} must be a finite number")
                try:
                    num = float(val)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"holdings[{idx}].{key} must be a finite number") from exc
                if not math.isfinite(num):
                    raise ValueError(f"holdings[{idx}].{key} must be a finite number")
        return holdings


class PaperOrderRequest(BaseModel):
    symbol: str = Field(..., min_length=1)
    side: str = Field(..., min_length=1)
    quantity: _StrictInt = Field(..., gt=0, le=2_000_000_000)
    order_type: str = "market"
    limit_price: _StrictFloat | None = None
    market: str = "CN"
    trade_date: str | None = None
    reference_bar: dict[str, Any] | None = None
    strategy_id: str | None = None
    price_limit_rate: _StrictFloat | None = None

    @field_validator("symbol", "side")
    @classmethod
    def _clean_upper(cls, v: str) -> str:
        clean = v.strip().upper()
        if not clean:
            raise ValueError("field must be non-empty")
        return clean

    @field_validator("limit_price", "price_limit_rate")
    @classmethod
    def _validate_finite_opt_float(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v


class PaperAdvanceDayRequest(BaseModel):
    new_date: str = Field(..., min_length=8)
    mark_prices: dict[str, float] | None = None

    @field_validator("mark_prices", mode="before")
    @classmethod
    def _reject_bool_mark_prices(cls, v: Any) -> Any:
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ValueError("mark_prices must be a JSON object")
        for sym, px in v.items():
            if isinstance(px, bool):
                raise ValueError(f"mark_prices[{sym!r}] must be a finite number, not a boolean")
        return v

    @field_validator("mark_prices")
    @classmethod
    def _validate_mark_prices(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is not None:
            for sym, px in v.items():
                if not math.isfinite(px):
                    raise ValueError(f"mark_prices[{sym!r}] must be a finite number")
        return v


class PaperResetRequest(BaseModel):
    initial_capital: _StrictFloat = Field(default=100000.0, gt=0.0, le=1e12)
    initial_date: str | None = None

    @field_validator("initial_capital")
    @classmethod
    def _validate_finite_cap(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("initial_capital must be a finite number")
        return v


# ---------------------------------------------------------------------------
# FastAPI App Factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    def _init_on_startup() -> None:
        with _MARKET_DB_LOCK:
            with db_session(_resolve_market_db_path()) as con:
                _ensure_baseline_seeded(con)
        # Ensure paper-ledger schema exists without retaining open handles.
        PaperAccountService()

    await anyio.to_thread.run_sync(_init_on_startup)
    yield


def create_app() -> FastAPI:
    application = FastAPI(
        title="JEVDB Stock Research & Paper-Trading API",
        version="0.1.0",
        lifespan=_lifespan,
    )

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": _json_safe(exc.errors())},
        )

    # ---- Health & Misc ----

    @application.get("/api/health")
    def api_health() -> dict[str, str]:
        return {
            "status": "ok",
            "jevdb_version": str(getattr(jevdb, "__version__", "0.1.0")),
            "duckdb_version": str(duckdb.__version__),
        }

    @application.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    # ---- Agent Research Endpoints ----

    @application.post("/api/agent/query")
    async def api_agent_query(req: AgentQueryRequest) -> JSONResponse:
        def _run() -> dict[str, Any]:
            return run_research_query(
                prompt=req.prompt,
                mode=req.mode,
                session_id=req.session_id,
            )

        try:
            result = await anyio.to_thread.run_sync(_run)
            return JSONResponse(_json_safe(result))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.get("/api/agent/stream")
    async def api_agent_stream(
        prompt: str = Query(...),
        mode: str = Query("auto"),
        session_id: str | None = Query(None),
    ) -> StreamingResponse:
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            raise HTTPException(status_code=422, detail="Research prompt must be non-empty")

        queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _on_event(ev: dict[str, Any]) -> None:
            ev_type = str(ev.get("type") or "status")
            safe_payload = _json_safe(ev)
            loop.call_soon_threadsafe(queue.put_nowait, (ev_type, safe_payload))

        def _worker() -> None:
            try:
                run_research_query(
                    prompt=clean_prompt,
                    mode=mode,
                    session_id=session_id,
                    on_event=_on_event,
                )
            except Exception as exc:
                err_payload = _json_safe(
                    {
                        "type": "error",
                        "detail": str(exc),
                        "error_type": type(exc).__name__,
                    }
                )
                loop.call_soon_threadsafe(queue.put_nowait, ("error", err_payload))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        task = asyncio.create_task(asyncio.to_thread(_worker))

        async def _event_generator():
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    ev_type, payload = item
                    data_str = json.dumps(payload, ensure_ascii=False)
                    yield f"event: {ev_type}\ndata: {data_str}\n\n"
            finally:
                if not task.done():
                    try:
                        await task
                    except Exception:
                        pass

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---- Market & Fundamentals Endpoints ----

    @application.get("/api/market/bars")
    def api_market_bars(
        symbol: str = Query(..., min_length=1),
        market: str | None = Query(None),
        days: int = Query(120, ge=1, le=3650),
    ) -> JSONResponse:
        if not symbol.strip():
            raise HTTPException(status_code=422, detail="symbol must be non-empty")
        mkt = market.strip().upper() if (market and market.strip()) else None
        sym, inferred_mkt = _normalize_market_symbol(symbol, mkt)
        with _MARKET_DB_LOCK:
            with db_session(_resolve_market_db_path()) as con:
                _ensure_baseline_seeded(con)
                bars = load_bars_from_duckdb([sym], market=mkt, con=con)
        if len(bars) > days:
            bars = bars[-days:]
        formatted = [
            {
                "date": b["date"],
                "trade_date": b["date"],
                "symbol": b["symbol"],
                "market": inferred_mkt,
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b.get("volume"),
                "source": b.get("source"),
            }
            for b in bars
        ]
        return JSONResponse(_json_safe({"symbol": sym, "market": inferred_mkt, "bars": formatted}))

    @application.get("/api/market/sector")
    def api_market_sector(
        name: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
    ) -> JSONResponse:
        if name is None:
            sec_name = "电力"
        else:
            sec_name = name.strip()
            if not sec_name:
                raise HTTPException(status_code=422, detail="Sector 'name' must be non-empty when provided")
        with _MARKET_DB_LOCK:
            with db_session(_resolve_market_db_path()) as con:
                _ensure_baseline_seeded(con)
                snap_cur = con.execute(
                    """
                    SELECT market, sector_name, sector_code,
                           CAST(trade_date AS VARCHAR) AS trade_date,
                           change_pct, volume, turnover, leader_symbol, payload_json, source
                    FROM sector_snapshots
                    WHERE sector_name ILIKE ?
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY market, sector_name, trade_date
                        ORDER BY CASE source
                            WHEN 'akshare_ths' THEN 1
                            WHEN 'akshare_sina' THEN 2
                            ELSE 9
                        END
                    ) = 1
                    ORDER BY trade_date DESC
                    LIMIT ?
                    """,
                    [f"%{sec_name}%", limit],
                )
                snapshots = _rows_to_dicts(snap_cur.description, snap_cur.fetchall())

                const_cur = con.execute(
                    """
                    SELECT sector_name, symbol, name, price, change_pct, pe, pb, market_cap,
                           CAST(updated_at AS VARCHAR) AS updated_at, source
                    FROM sector_constituents
                    WHERE sector_name ILIKE ?
                    QUALIFY ROW_NUMBER() OVER (
                        PARTITION BY sector_name, symbol
                        ORDER BY updated_at DESC NULLS LAST
                    ) = 1
                    ORDER BY change_pct DESC NULLS LAST, symbol ASC
                    LIMIT ?
                    """,
                    [f"%{sec_name}%", limit],
                )
                constituents = _rows_to_dicts(const_cur.description, const_cur.fetchall())
        return JSONResponse(
            _json_safe(
                {
                    "sector": sec_name,
                    "snapshots": snapshots,
                    "constituents": constituents,
                }
            )
        )

    @application.get("/api/market/fundamentals")
    def api_market_fundamentals(
        symbol: str = Query(..., min_length=1),
        market: str | None = Query(None),
        limit: int = Query(20, ge=1, le=100),
    ) -> JSONResponse:
        if not symbol.strip():
            raise HTTPException(status_code=422, detail="symbol must be non-empty")
        mkt = market.strip().upper() if (market and market.strip()) else None
        sym, fallback_mkt = _normalize_market_symbol(symbol, mkt)
        with _MARKET_DB_LOCK:
            with db_session(_resolve_market_db_path()) as con:
                _ensure_baseline_seeded(con)
                if mkt:
                    fund_cur = con.execute(
                        """
                        SELECT symbol, market, CAST(report_date AS VARCHAR) AS report_date,
                               pe_ttm, pb, roe, revenue, net_income, debt_ratio,
                               metrics_json, source, CAST(updated_at AS VARCHAR) AS updated_at
                        FROM fundamentals
                        WHERE UPPER(symbol) = ? AND UPPER(market) = ?
                        ORDER BY report_date DESC, updated_at DESC NULLS LAST
                        LIMIT ?
                        """,
                        [sym, mkt, limit],
                    )
                else:
                    fund_cur = con.execute(
                        """
                        SELECT symbol, market, CAST(report_date AS VARCHAR) AS report_date,
                               pe_ttm, pb, roe, revenue, net_income, debt_ratio,
                               metrics_json, source, CAST(updated_at AS VARCHAR) AS updated_at
                        FROM fundamentals
                        WHERE UPPER(symbol) = ?
                        ORDER BY report_date DESC, updated_at DESC NULLS LAST
                        LIMIT ?
                        """,
                        [sym, limit],
                    )
                fundamentals = _rows_to_dicts(fund_cur.description, fund_cur.fetchall())

                edgar_cur = con.execute(
                    """
                    SELECT cik, symbol, accession_no, form,
                           CAST(filing_date AS VARCHAR) AS filing_date,
                           CAST(report_date AS VARCHAR) AS report_date,
                           primary_doc, title, url
                    FROM edgar_filings
                    WHERE UPPER(symbol) = ?
                    ORDER BY filing_date DESC NULLS LAST
                    LIMIT ?
                    """,
                    [sym, limit],
                )
                edgar_filings = _rows_to_dicts(edgar_cur.description, edgar_cur.fetchall())

                earn_cur = con.execute(
                    """
                    SELECT symbol, CAST(report_date AS VARCHAR) AS report_date,
                           eps_estimate, eps_actual, revenue_estimate, revenue_actual,
                           source, CAST(fetched_at AS VARCHAR) AS fetched_at
                    FROM earnings_calendar
                    WHERE UPPER(symbol) = ?
                    ORDER BY report_date DESC
                    LIMIT ?
                    """,
                    [sym, limit],
                )
                earnings_calendar = _rows_to_dicts(earn_cur.description, earn_cur.fetchall())
        inferred_mkt = mkt or (fundamentals[0].get("market") if fundamentals else fallback_mkt)
        return JSONResponse(
            _json_safe(
                {
                    "symbol": sym,
                    "market": inferred_mkt,
                    "fundamentals": fundamentals,
                    "edgar_filings": edgar_filings,
                    "earnings_calendar": earnings_calendar,
                }
            )
        )

    @application.get("/api/market/news")
    def api_market_news(
        symbol: str | None = Query(None),
        sector: str | None = Query(None),
        market: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
    ) -> JSONResponse:
        clauses: list[str] = []
        params: list[Any] = []
        mkt = market.strip().upper() if (market and market.strip()) else None
        if symbol and symbol.strip():
            clauses.append("UPPER(symbol) = ?")
            params.append(_normalize_symbol_for_market(symbol, mkt))
        if sector and sector.strip():
            clauses.append("sector ILIKE ?")
            params.append(f"%{sector.strip()}%")
        if mkt:
            clauses.append("UPPER(market) = ?")
            params.append(mkt)
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        with _MARKET_DB_LOCK:
            with db_session(_resolve_market_db_path()) as con:
                _ensure_baseline_seeded(con)
                cur = con.execute(
                    f"""
                    SELECT event_id, symbol, market, sector,
                           CAST(published_at AS VARCHAR) AS published_at,
                           title, summary, url, source, sentiment_label, keywords_json
                    FROM news_events
                    {where_sql}
                    ORDER BY published_at DESC NULLS LAST
                    LIMIT ?
                    """,
                    params,
                )
                news = _rows_to_dicts(cur.description, cur.fetchall())
        return JSONResponse(_json_safe({"news": news}))

    @application.post("/api/market/ingest")
    def api_market_ingest(req: MarketIngestRequest) -> JSONResponse:
        db_path = _resolve_market_db_path()
        try:
            with _MARKET_DB_LOCK:
                if req.kind == "a_share_sector":
                    res = ingest_a_share_sector(
                        sector=req.sector or "电力",
                        days=req.days,
                        db_path=db_path,
                        top_constituents=req.top_constituents,
                        force=req.force,
                    )
                elif req.kind == "a_share_stock":
                    res = ingest_a_share_stock(
                        symbol=req.symbol or "",
                        days=req.days,
                        db_path=db_path,
                        force=req.force,
                    )
                elif req.kind == "us_stock":
                    res = ingest_us_stock(
                        symbol=req.symbol or "",
                        period=req.period,
                        db_path=db_path,
                        force=req.force,
                    )
                elif req.kind == "sec_edgar":
                    res = ingest_sec_edgar(
                        symbol=req.symbol or "",
                        db_path=db_path,
                    )
                elif req.kind == "finnhub_news":
                    res = ingest_finnhub_news(
                        symbol=req.symbol,
                        category=req.category,
                        days=req.days,
                        db_path=db_path,
                    )
                elif req.kind == "finnhub_earnings":
                    today = date.today()
                    f_dt = req.from_date or (today - timedelta(days=7)).isoformat()
                    t_dt = req.to_date or (today + timedelta(days=14)).isoformat()
                    res = ingest_finnhub_earnings(
                        from_date=f_dt,
                        to_date=t_dt,
                        symbol=req.symbol,
                        db_path=db_path,
                    )
                else:
                    raise ValueError(f"Unsupported ingest kind: {req.kind}")
            return JSONResponse(_json_safe(res))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    # ---- Quant Strategy, Backtest & Risk Endpoints ----

    @application.get("/api/quant/strategies")
    def api_quant_list_strategies(
        style: str | None = Query(None),
        market: str | None = Query(None),
    ) -> JSONResponse:
        with _MARKET_DB_LOCK:
            with db_session(_resolve_market_db_path()) as con:
                ensure_builtin_strategies(con=con)
                specs = list_strategies(style=style, market=market, ensure_builtins=True, con=con)
        return JSONResponse(_json_safe({"strategies": [s.to_dict() for s in specs]}))

    @application.post("/api/quant/strategies")
    def api_quant_create_strategy(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="Strategy payload must be a JSON object")
        for req_field in ("strategy_id", "name", "style"):
            if req_field not in payload:
                raise HTTPException(status_code=422, detail=f"Missing required field: '{req_field}'")
            val = payload[req_field]
            if not isinstance(val, str) or not val.strip():
                raise HTTPException(
                    status_code=422,
                    detail=f"Field '{req_field}' must be a non-empty string",
                )
        for opt_str in ("market", "description", "source", "created_at"):
            if opt_str in payload and payload[opt_str] is not None:
                if not isinstance(payload[opt_str], str):
                    raise HTTPException(
                        status_code=422,
                        detail=f"Field '{opt_str}' must be a string",
                    )
                if opt_str in ("market", "source") and not payload[opt_str].strip():
                    raise HTTPException(
                        status_code=422,
                        detail=f"Field '{opt_str}' must be a non-empty string when provided",
                    )
        for dict_field in ("params", "rules"):
            if dict_field in payload and payload[dict_field] is not None and not isinstance(payload[dict_field], dict):
                raise HTTPException(
                    status_code=422,
                    detail=f"Field '{dict_field}' must be a JSON object",
                )
        if payload.get("description") is None:
            payload = {**payload, "description": ""}
        try:
            spec = StrategySpec.from_dict(payload)
            with _MARKET_DB_LOCK:
                with db_session(_resolve_market_db_path()) as con:
                    ensure_builtin_strategies(con=con)
                    saved = save_strategy(spec, con=con)
            return JSONResponse(_json_safe(saved.to_dict()))
        except KeyError as exc:
            raise HTTPException(status_code=422, detail=f"Missing required field: {exc}") from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/quant/backtest")
    def api_quant_backtest(req: QuantBacktestRequest) -> JSONResponse:
        try:
            db_path = _resolve_market_db_path()
            with _MARKET_DB_LOCK:
                if req.auto_fetch:
                    _auto_fetch_missing_bars_for_window(
                        symbols=req.symbols,
                        market=req.market.upper(),
                        start_date=req.start_date,
                        end_date=req.end_date,
                        db_path=db_path,
                    )
                with db_session(db_path) as con:
                    _ensure_baseline_seeded(con)
                    res = run_backtest(
                        strategy_id=req.strategy_id,
                        symbols=req.symbols,
                        start_date=req.start_date,
                        end_date=req.end_date,
                        initial_capital=req.initial_capital,
                        seed=req.seed,
                        market=req.market.upper(),
                        con=con,
                    )
            return JSONResponse(_json_safe(res.to_dict()))
        except StrategyNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/quant/compare")
    def api_quant_compare(req: QuantCompareRequest) -> JSONResponse:
        try:
            db_path = _resolve_market_db_path()
            fetched_symbols: list[str] = []
            with _MARKET_DB_LOCK:
                if req.auto_fetch:
                    fetched_symbols = _auto_fetch_missing_bars_for_window(
                        symbols=req.symbols,
                        market=req.market.upper(),
                        start_date=req.start_date,
                        end_date=req.end_date,
                        db_path=db_path,
                    )
                with db_session(db_path) as con:
                    _ensure_baseline_seeded(con)
                    res = compare_strategies(
                        strategy_ids=req.strategy_ids,
                        symbols=req.symbols,
                        start_date=req.start_date,
                        end_date=req.end_date,
                        initial_capital=req.initial_capital,
                        market=req.market.upper(),
                        seed=req.seed,
                        sort_by=req.sort_by,
                        con=con,
                    )
            payload = res.to_dict()
            if fetched_symbols:
                payload["auto_fetched_symbols"] = fetched_symbols
            return JSONResponse(_json_safe(payload))
        except StrategyNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/quant/portfolio-risk")
    def api_quant_portfolio_risk(req: PortfolioRiskRequest) -> JSONResponse:
        try:
            raw_holdings = list(req.holdings)
            us_syms = {
                str(h.get("symbol") or "").strip().upper()
                for h in raw_holdings
                if str(h.get("market") or "").strip().upper() == "US"
            }
            norm_holdings: list[dict[str, Any]] = []
            for h in raw_holdings:
                h_copy = dict(h)
                raw_sym = str(h_copy.get("symbol") or "").strip()
                if raw_sym:
                    h_mkt = str(h_copy.get("market") or "").strip().upper() or None
                    h_copy["symbol"] = _normalize_symbol_for_market(raw_sym, h_mkt)
                norm_holdings.append(h_copy)

            history = req.price_history
            if history is None:
                symbols = sorted(
                    {
                        str(h.get("symbol") or "").strip().upper()
                        for h in norm_holdings
                        if str(h.get("symbol") or "").strip()
                    }
                )
                history = {s: [] for s in symbols}
                if symbols:
                    if req.start_date or req.end_date:
                        end_dt = _backtest_to_date(req.end_date) if req.end_date else date.today()
                        start_dt = (
                            _backtest_to_date(req.start_date)
                            if req.start_date
                            else (end_dt - timedelta(days=max(30, req.days * 2)))
                        )
                    else:
                        end_dt = date.today()
                        start_dt = end_dt - timedelta(days=max(30, req.days * 2))
                    with _MARKET_DB_LOCK:
                        with db_session(_resolve_market_db_path()) as con:
                            _ensure_baseline_seeded(con)
                            raw_bars = load_bars_from_duckdb(
                                symbols,
                                start_date=start_dt.isoformat(),
                                end_date=end_dt.isoformat(),
                                con=con,
                            )
                            by_sym: dict[str, list[dict[str, Any]]] = {}
                            for r in raw_bars:
                                by_sym.setdefault(str(r["symbol"]).upper(), []).append(r)
                            if any(len(by_sym.get(s, [])) < 2 for s in symbols):
                                all_bars = load_bars_from_duckdb(symbols, con=con)
                                all_by_sym: dict[str, list[dict[str, Any]]] = {}
                                for r in all_bars:
                                    all_by_sym.setdefault(str(r["symbol"]).upper(), []).append(r)
                                merged: list[dict[str, Any]] = []
                                for s in symbols:
                                    win_rows = by_sym.get(s, [])
                                    merged.extend(win_rows if len(win_rows) >= 2 else all_by_sym.get(s, win_rows))
                                raw_bars = merged
                    for row in raw_bars:
                        sym = str(row["symbol"]).upper()
                        history.setdefault(sym, []).append(
                            {"date": str(row["date"]), "close": float(row["close"])}
                        )
            else:
                history = {
                    (
                        str(k).strip().upper()
                        if str(k).strip().upper() in us_syms
                        else _normalize_symbol_for_market(str(k), None)
                    ): v
                    for k, v in history.items()
                }
            res = compute_portfolio_risk(
                holdings=norm_holdings,
                price_history=history,
                benchmark_history=req.benchmark_history,
                confidence=req.confidence,
            )
            return JSONResponse(_json_safe(res))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ---- Paper-Trading Endpoints ----

    @application.get("/api/paper/account")
    def api_paper_account() -> JSONResponse:
        svc = PaperAccountService()
        summary = svc.get_account_summary()
        summary["equity_history"] = svc.get_equity_history()
        summary["orders"] = svc.get_orders(limit=50)
        summary["fills"] = svc.get_fills(limit=50)
        return JSONResponse(_json_safe(summary))

    @application.post("/api/paper/order")
    def api_paper_order(req: PaperOrderRequest) -> JSONResponse:
        mkt_in = req.market.strip().upper()
        if mkt_in:
            norm_sym, norm_mkt = _normalize_market_symbol(req.symbol, mkt_in)
        else:
            norm_sym, norm_mkt = req.symbol.strip().upper(), ""
        ref_bar = req.reference_bar
        if ref_bar is None:
            with _MARKET_DB_LOCK:
                with db_session(_resolve_market_db_path()) as con:
                    _ensure_baseline_seeded(con)
                    ref_bar = _lookup_latest_bar_unlocked(
                        con,
                        norm_sym,
                        norm_mkt or "CN",
                        trade_date=req.trade_date,
                    )
        try:
            svc = PaperAccountService()
            res = svc.submit_order(
                symbol=norm_sym,
                side=req.side,
                quantity=req.quantity,
                order_type=req.order_type,
                limit_price=req.limit_price,
                market=norm_mkt,
                trade_date=req.trade_date,
                reference_bar=ref_bar,
                strategy_id=req.strategy_id,
                price_limit_rate=req.price_limit_rate,
            )
            return JSONResponse(_json_safe(res))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/paper/advance-day")
    def api_paper_advance_day(req: PaperAdvanceDayRequest) -> JSONResponse:
        try:
            svc = PaperAccountService()
            res = svc.advance_day(req.new_date, mark_prices=req.mark_prices)
            return JSONResponse(_json_safe(res))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/paper/reset")
    def api_paper_reset(req: PaperResetRequest | None = None) -> JSONResponse:
        payload = req or PaperResetRequest()
        try:
            svc = PaperAccountService()
            res = svc.reset_account(
                initial_capital=payload.initial_capital,
                initial_date=payload.initial_date,
            )
            return JSONResponse(_json_safe(res))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ---- Static Web Frontend Serving ----

    assets_dir = _WEB_DIST_DIR / "assets"
    if assets_dir.is_dir():
        application.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    def _serve_index_response() -> Response:
        index_html = _WEB_DIST_DIR / "index.html"
        if index_html.is_file():
            return FileResponse(str(index_html), media_type="text/html; charset=utf-8")
        return HTMLResponse(
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'/>"
            "<link rel='icon' href='data:,'/>"
            "<title>JEVDB Stock Research</title></head>"
            "<body><div id='root'>Frontend dist not built yet.</div></body></html>",
            status_code=200,
        )

    @application.get("/", include_in_schema=False)
    def spa_root() -> Response:
        return _serve_index_response()

    @application.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> Response:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"Not Found: /{full_path}")
        candidate = (_WEB_DIST_DIR / full_path).resolve()
        try:
            candidate.relative_to(_WEB_DIST_DIR.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="Not Found")
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return _serve_index_response()

    return application


app = create_app()
