"""Stdio MCP server (mcp==2.3.0) exposing JEVDB, data ingestion, web, quant, and paper-trading tools."""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BeforeValidator

from stock_research.config import get_settings
from stock_research.mcp_server import web_tools
from stock_research.quant.backtest import (
    compare_strategies,
    load_bars_from_duckdb,
    run_backtest,
)
from stock_research.quant.ledger import PaperAccountService
from stock_research.quant.risk import compute_portfolio_risk
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
from stock_research.storage.jevdb_bridge import hybrid_semantic_query
from stock_research.storage.schema import db_session, ensure_baseline_seeded

logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("stock_research.mcp_server")
_MARKET_DB_LOCK = threading.RLock()

_ALLOWED_TABLES = frozenset(
    {
        "assets",
        "market_bars",
        "sector_snapshots",
        "sector_constituents",
        "fundamentals",
        "edgar_filings",
        "news_events",
        "earnings_calendar",
        "static_trading_rules",
    }
)


class _OfflineSemanticEvalServer:
    """Ephemeral OpenAI-compatible local evaluator used only when backend='mock'."""

    def __init__(self) -> None:
        self._srv: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url: str = ""

    def __enter__(self) -> "_OfflineSemanticEvalServer":
        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                body = json.loads(raw.decode("utf-8"))
                msgs = body.get("messages") or []
                sys_msg = "\n".join(str(m.get("content") or "") for m in msgs if m.get("role") == "system")
                user_msg = next((str(m.get("content") or "") for m in msgs if m.get("role") == "user"), "{}")
                try:
                    user_obj = json.loads(user_msg)
                except ValueError:
                    user_obj = {}
                items = user_obj.get("items") if isinstance(user_obj, dict) else None
                if not isinstance(items, list):
                    items = [user_obj if isinstance(user_obj, dict) else {}]

                rf = body.get("response_format") or {}
                items_schema = (
                    (rf.get("json_schema") or {})
                    .get("schema", {})
                    .get("properties", {})
                    .get("answers", {})
                    .get("items", {})
                )
                item_type = items_schema.get("type")

                answers: list[Any] = []
                if item_type == "object":
                    answers = [{"id": idx, "verdict": True} for idx in range(len(items))]
                elif item_type == "string" and isinstance(items_schema.get("enum"), list):
                    enum_vals = items_schema["enum"]
                    for it in items:
                        txt = json.dumps(it, ensure_ascii=False).lower()
                        picked = next((c for c in enum_vals if str(c).lower() in txt), enum_vals[0])
                        answers.append(picked)
                elif item_type in ("number", "integer"):
                    hi = float(items_schema.get("maximum", 1.0))
                    lo = float(items_schema.get("minimum", 0.0))
                    mid = round((hi + lo) / 2.0, 2)
                    for it in items:
                        txt = json.dumps(it, ensure_ascii=False).lower()
                        is_pos = any(k in txt for k in ("增长", "positive", "good", "改善", "利好", "电力"))
                        answers.append(min(hi, max(lo, hi * 0.85 if is_pos else mid)))
                else:
                    answers = [True for _ in items]

                payload = {
                    "id": "offline-mock-eval",
                    "object": "chat.completion",
                    "model": body.get("model") or "mock-eval",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"answers": answers}, ensure_ascii=False),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}/v1"
        return self

    def __exit__(self, *args: Any) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


mcp = MCPServer(
    "stock_research",
    instructions=(
        "JEVDB Stock Research & Paper-Trading MCP server providing hybrid semantic+SQL queries, "
        "multi-source CN/US data ingestion, web search/fetch, quant strategy backtesting & comparison, "
        "portfolio risk analytics, and persistent paper-trading ledger tools."
    ),
)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _normalize_symbol_for_market(symbol: str, market: str | None = None) -> str:
    clean = str(symbol or "").strip().upper()
    if not clean:
        return ""
    mkt = str(market or "").strip().upper()
    if not mkt or mkt == "CN":
        try:
            code6, _, _ = normalize_cn_symbol(clean)
            return code6
        except ValueError:
            pass
    return clean


def _query_table_rows(
    con: Any,
    table: str,
    *,
    symbol: str | None = None,
    market: str | None = None,
    sector: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if table not in _ALLOWED_TABLES:
        raise ToolError(f"Unsupported table {table!r}; allowed: {sorted(_ALLOWED_TABLES)}")

    lim = max(1, min(int(limit or 50), 500))
    where: list[str] = []
    params: list[Any] = []

    mkt = market.strip().upper() if market and market.strip() else None
    sym = _normalize_symbol_for_market(symbol, mkt) if symbol and symbol.strip() else None
    sec = sector.strip() if sector and sector.strip() else None

    if sym and table not in ("sector_snapshots",):
        where.append("UPPER(symbol) = ?")
        params.append(sym)
    if mkt and table in ("assets", "market_bars", "sector_snapshots", "fundamentals", "news_events"):
        where.append("UPPER(market) = ?")
        params.append(mkt)
    if sec:
        if table in ("assets", "news_events"):
            where.append("sector ILIKE ?")
            params.append(f"%{sec}%")
        elif table in ("sector_snapshots", "sector_constituents"):
            where.append("sector_name ILIKE ?")
            params.append(f"%{sec}%")

    date_col_map = {
        "market_bars": "trade_date",
        "sector_snapshots": "trade_date",
        "fundamentals": "report_date",
        "edgar_filings": "filing_date",
        "news_events": "CAST(published_at AS DATE)",
        "earnings_calendar": "report_date",
    }
    date_col = date_col_map.get(table)
    if date_col and start_date:
        where.append(f"{date_col} >= CAST(? AS DATE)")
        params.append(start_date[:10])
    if date_col and end_date:
        where.append(f"{date_col} <= CAST(? AS DATE)")
        params.append(end_date[:10])

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    if table == "sector_constituents":
        sql = f"""
            SELECT sector_name, symbol, name, price, change_pct, pe, pb, market_cap,
                   CAST(updated_at AS VARCHAR) AS updated_at, source
            FROM sector_constituents
            {where_sql}
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY sector_name, symbol
                ORDER BY updated_at DESC NULLS LAST
            ) = 1
            ORDER BY change_pct DESC NULLS LAST, symbol ASC
            LIMIT ?
        """
    elif table == "market_bars":
        sql = f"""
            SELECT symbol, market, CAST(trade_date AS VARCHAR) AS trade_date,
                   open, high, low, close, volume, amount, source
            FROM market_bars
            {where_sql}
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY symbol, market, trade_date
                ORDER BY CASE LOWER(COALESCE(source, ''))
                    WHEN 'akshare_tx' THEN 1
                    WHEN 'akshare_sina' THEN 2
                    WHEN 'yfinance' THEN 3
                    WHEN 'static_baseline' THEN 90
                    ELSE 50
                END,
                LOWER(COALESCE(source, '')) ASC
            ) = 1
            ORDER BY trade_date DESC, symbol ASC
            LIMIT ?
        """
    elif table == "sector_snapshots":
        sql = f"""
            SELECT market, sector_name, sector_code, CAST(trade_date AS VARCHAR) AS trade_date,
                   change_pct, volume, turnover, leader_symbol, payload_json, source
            FROM sector_snapshots
            {where_sql}
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY market, sector_name, trade_date
                ORDER BY CASE LOWER(COALESCE(source, ''))
                    WHEN 'akshare_ths' THEN 1
                    WHEN 'akshare_ths_summary' THEN 2
                    WHEN 'akshare_ths_index' THEN 3
                    WHEN 'akshare_sina' THEN 4
                    ELSE 9
                END,
                LOWER(COALESCE(source, '')) ASC
            ) = 1
            ORDER BY trade_date DESC
            LIMIT ?
        """
    elif table == "fundamentals":
        sql = f"""
            SELECT symbol, market, CAST(report_date AS VARCHAR) AS report_date,
                   pe_ttm, pb, roe, revenue, net_income, debt_ratio, metrics_json, source,
                   CAST(updated_at AS VARCHAR) AS updated_at
            FROM fundamentals
            {where_sql}
            ORDER BY report_date DESC
            LIMIT ?
        """
    elif table == "edgar_filings":
        sql = f"""
            SELECT cik, symbol, accession_no, form, CAST(filing_date AS VARCHAR) AS filing_date,
                   CAST(report_date AS VARCHAR) AS report_date, primary_doc, title, url
            FROM edgar_filings
            {where_sql}
            ORDER BY filing_date DESC NULLS LAST
            LIMIT ?
        """
    elif table == "news_events":
        sql = f"""
            SELECT event_id, symbol, market, sector, CAST(published_at AS VARCHAR) AS published_at,
                   title, summary, url, source, sentiment_label, keywords_json
            FROM news_events
            {where_sql}
            ORDER BY published_at DESC NULLS LAST
            LIMIT ?
        """
    elif table == "earnings_calendar":
        sql = f"""
            SELECT symbol, CAST(report_date AS VARCHAR) AS report_date,
                   eps_estimate, eps_actual, revenue_estimate, revenue_actual, source,
                   CAST(fetched_at AS VARCHAR) AS fetched_at
            FROM earnings_calendar
            {where_sql}
            ORDER BY report_date DESC
            LIMIT ?
        """
    else:
        sql = f"SELECT * FROM {table} {where_sql} LIMIT ?"

    params.append(lim)
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [_json_safe(dict(zip(cols, row))) for row in cur.fetchall()]


@mcp.tool()
def jevdb_semantic_query(
    condition: str,
    query_keys: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    symbol: str | None = None,
    market: str | None = None,
    sector: str | None = None,
    source: str | None = None,
    op: str = "holds",
    target: str = "news_events",
    choices: list[str] | None = None,
    levels: list[str] | None = None,
    only_matching: bool = False,
    threshold: float = 0.5,
    use_cache: bool = True,
    backend: str = "auto",
    limit: int = 20,
) -> dict[str, Any]:
    """Run a hybrid SQL + SBF (jev_keys_overlap) + JEVDB semantic query over static and dynamic tables.

    Valid `target` values: 'news_events', 'edgar_filings', 'fundamentals', 'combined'.
    Valid `op` values: 'holds', 'probability', 'choose', 'score'.
    """
    clean_cond = str(condition or "").strip()
    if not clean_cond:
        raise ToolError("condition must be a non-empty string")

    merged_filters = dict(filters or {})
    if symbol is not None and "symbol" not in merged_filters:
        merged_filters["symbol"] = symbol
    if market is not None and "market" not in merged_filters:
        merged_filters["market"] = market
    if sector is not None and "sector" not in merged_filters:
        merged_filters["sector"] = sector
    if source is not None and "source_scope" not in merged_filters:
        norm_src = source.strip().lower()
        if norm_src in ("all", "static", "dynamic"):
            merged_filters["source_scope"] = norm_src
        elif norm_src == "static_baseline":
            merged_filters["source_scope"] = "static"
    if "limit" not in merged_filters and limit is not None:
        merged_filters["limit"] = int(limit)

    settings = get_settings(ensure_dirs=False)
    req_backend = str(backend or "auto").strip().lower()
    if req_backend == "auto":
        eff_backend = "llm" if settings.deepseek_api_key else "mock"
    elif req_backend in ("llm", "mock", "jev"):
        eff_backend = req_backend
    else:
        raise ToolError(f"Unsupported backend {backend!r}; expected 'auto', 'llm', 'mock', or 'jev'")

    spec: dict[str, Any] = {
        "condition": clean_cond,
        "op": op,
        "target": target,
        "filters": merged_filters,
        "only_matching": only_matching,
        "threshold": threshold,
        "use_cache": use_cache,
    }
    if query_keys:
        spec["query_keys"] = list(query_keys)
    if choices is not None:
        spec["choices"] = list(choices)
    if levels is not None:
        spec["levels"] = list(levels)

    try:
        with _MARKET_DB_LOCK:
            if eff_backend == "mock":
                with _OfflineSemanticEvalServer() as mock_srv:
                    spec["backend"] = "llm"
                    spec["cache_backend"] = "mock"
                    spec["endpoint"] = mock_srv.url
                    spec["api_key"] = "offline-mock"
                    res = hybrid_semantic_query(spec)
                    res["backend"] = "mock"
                    return _json_safe(res)

            if eff_backend == "llm" and not settings.deepseek_api_key:
                raise ToolError("DEEPSEEK_API_KEY is required when backend='llm'")

            spec["backend"] = eff_backend
            res = hybrid_semantic_query(spec)
            return _json_safe(res)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"jevdb_semantic_query failed: {exc}") from exc


def _reject_bool_int(val: Any) -> Any:
    if isinstance(val, bool):
        raise ValueError("days must be an integer >= 1, not a boolean")
    return val


@mcp.tool()
def fetch_market_data(
    source: str = "auto",
    symbol: str | None = None,
    sector: str | None = None,
    market: str = "CN",
    days: Annotated[int, BeforeValidator(_reject_bool_int)] = 30,
    period: str = "6mo",
    from_date: str | None = None,
    to_date: str | None = None,
    category: str = "general",
    force: bool = False,
) -> dict[str, Any]:
    """Fetch fresh market bars, sector snapshots, fundamentals, filings, or news into DuckDB.

    Valid `source` values: 'auto', 'akshare_sector', 'akshare_stock', 'yfinance', 'sec_edgar', 'finnhub_news', 'finnhub_earnings'.
    """
    src = str(source or "auto").strip().lower()
    mkt = str(market or "CN").strip().upper()
    sym = symbol.strip() if symbol and symbol.strip() else None
    sec = sector.strip() if sector and sector.strip() else None

    if isinstance(days, bool):
        raise ToolError("days must be an integer >= 1, not a boolean")
    try:
        days_int = int(days)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"days must be an integer >= 1: {days!r}") from exc
    if days_int < 1:
        raise ToolError(f"days must be >= 1, got {days_int}")

    clean_period = str(period or "6mo").strip() or "6mo"
    clean_category = str(category or "general").strip().lower() or "general"

    if src == "auto":
        if sec and mkt == "CN":
            src = "akshare_sector"
        elif mkt == "US" and sym:
            src = "yfinance"
        elif sym:
            src = "akshare_stock"
        else:
            raise ToolError("Must specify either symbol or sector when source='auto'")

    try:
        with _MARKET_DB_LOCK:
            if src == "akshare_sector":
                res = ingest_a_share_sector(sector=sec or "电力", days=days_int, force=force)
            elif src == "akshare_stock":
                if not sym:
                    raise ToolError("symbol is required for source='akshare_stock'")
                res = ingest_a_share_stock(symbol=sym, days=days_int, force=force)
            elif src == "yfinance":
                if not sym:
                    raise ToolError("symbol is required for source='yfinance'")
                res = ingest_us_stock(symbol=sym, period=clean_period, force=force)
            elif src == "sec_edgar":
                if not sym:
                    raise ToolError("symbol is required for source='sec_edgar'")
                res = ingest_sec_edgar(symbol=sym)
            elif src == "finnhub_news":
                res = ingest_finnhub_news(symbol=sym, category=clean_category, days=days_int)
            elif src == "finnhub_earnings":
                today = date.today()
                f_date = (today - timedelta(days=7)).isoformat() if from_date is None else str(from_date).strip()
                t_date = (today + timedelta(days=14)).isoformat() if to_date is None else str(to_date).strip()
                try:
                    f_dt = date.fromisoformat(f_date)
                    t_dt = date.fromisoformat(t_date)
                except ValueError as exc:
                    raise ToolError(
                        f"Invalid finnhub_earnings date(s): from_date={from_date!r}, to_date={to_date!r}"
                    ) from exc
                if f_dt > t_dt:
                    raise ToolError(
                        f"finnhub_earnings from_date ({f_dt.isoformat()}) must be <= to_date ({t_dt.isoformat()})"
                    )
                res = ingest_finnhub_earnings(from_date=f_dt.isoformat(), to_date=t_dt.isoformat(), symbol=sym)
            else:
                raise ToolError(
                    f"Unsupported source {source!r}; expected auto, akshare_sector, akshare_stock, yfinance, sec_edgar, finnhub_news, or finnhub_earnings"
                )
        return _json_safe(res)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"fetch_market_data ({src}) failed: {exc}") from exc


@mcp.tool()
def query_stored_market_data(
    table: str = "auto",
    symbol: str | None = None,
    market: str | None = None,
    sector: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Query stored DuckDB tables (bars, sectors, deduplicated constituents, fundamentals, filings, news)."""
    tbl = str(table or "auto").strip().lower()

    try:
        with _MARKET_DB_LOCK:
            with db_session() as con:
                ensure_baseline_seeded(con)

                if tbl in ("auto", "all"):
                    eff_lim = min(int(limit or 50), 20)
                    bars = _query_table_rows(
                        con, "market_bars", symbol=symbol, market=market, start_date=start_date, end_date=end_date, limit=eff_lim
                    )
                    fundamentals = _query_table_rows(
                        con, "fundamentals", symbol=symbol, market=market, start_date=start_date, end_date=end_date, limit=min(eff_lim, 8)
                    )
                    # Keep auto-summary compact (<20KB) so Pi MCP never middle-truncates tool results
                    for f_row in fundamentals:
                        mj = f_row.get("metrics_json")
                        if isinstance(mj, str) and len(mj) > 400:
                            f_row["metrics_json"] = mj[:400] + "..."
                    news = _query_table_rows(
                        con, "news_events", symbol=symbol, market=market, sector=sector, start_date=start_date, end_date=end_date, limit=min(eff_lim, 12)
                    )
                    for n_row in news:
                        sm = n_row.get("summary")
                        if isinstance(sm, str) and len(sm) > 300:
                            n_row["summary"] = sm[:300] + "..."
                    norm_sym = _normalize_symbol_for_market(symbol, market) if symbol and symbol.strip() else None
                    payload: dict[str, Any] = {
                        "symbol": norm_sym or None,
                        "sector": sector,
                        "market_bars": bars,
                        "fundamentals": fundamentals,
                        "news_events": news,
                    }
                    if sector:
                        payload["sector_snapshots"] = _query_table_rows(
                            con, "sector_snapshots", market=market, sector=sector, start_date=start_date, end_date=end_date, limit=min(eff_lim, 10)
                        )
                        payload["sector_constituents"] = _query_table_rows(
                            con, "sector_constituents", sector=sector, limit=min(eff_lim, 20)
                        )
                    eff_mkt = (market or "").strip().upper()
                    is_non_cn = False
                    if symbol and not eff_mkt:
                        try:
                            normalize_cn_symbol(symbol)
                        except ValueError:
                            is_non_cn = True
                    if symbol and (eff_mkt == "US" or (not eff_mkt and is_non_cn)):
                        payload["edgar_filings"] = _query_table_rows(
                            con, "edgar_filings", symbol=symbol, start_date=start_date, end_date=end_date, limit=min(eff_lim, 10)
                        )
                        payload["earnings_calendar"] = _query_table_rows(
                            con, "earnings_calendar", symbol=symbol, start_date=start_date, end_date=end_date, limit=min(eff_lim, 10)
                        )
                    elif symbol and eff_mkt == "":
                        edgar_rows = _query_table_rows(
                            con, "edgar_filings", symbol=symbol, start_date=start_date, end_date=end_date, limit=min(eff_lim, 10)
                        )
                        earn_rows = _query_table_rows(
                            con, "earnings_calendar", symbol=symbol, start_date=start_date, end_date=end_date, limit=min(eff_lim, 10)
                        )
                        if edgar_rows:
                            payload["edgar_filings"] = edgar_rows
                        if earn_rows:
                            payload["earnings_calendar"] = earn_rows
                    return payload

                rows = _query_table_rows(
                    con,
                    tbl,
                    symbol=symbol,
                    market=market,
                    sector=sector,
                    start_date=start_date,
                    end_date=end_date,
                    limit=limit,
                )
                return {"table": tbl, "count": len(rows), "rows": rows}
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"query_stored_market_data failed: {exc}") from exc


@mcp.tool()
def web_search(
    query: str,
    max_results: int = 8,
    search_type: str = "text",
) -> dict[str, Any]:
    """Search public web articles and recent financial news via DuckDuckGo (no API key required)."""
    try:
        results = web_tools.web_search(query=query, max_results=max_results, search_type=search_type)
        return {"query": query, "count": len(results), "results": results}
    except Exception as exc:
        raise ToolError(f"web_search failed: {exc}") from exc


@mcp.tool()
def web_fetch_url(
    url: str,
    max_chars: int = 12000,
) -> dict[str, Any]:
    """Fetch a public http(s) URL with SSRF protection and extract clean Markdown text."""
    try:
        markdown = web_tools.web_fetch_url(url=url, allow_loopback_for_test=False, max_chars=max_chars)
        return {"url": url, "length": len(markdown), "markdown": markdown}
    except Exception as exc:
        raise ToolError(f"web_fetch_url failed: {exc}") from exc


@mcp.tool()
def list_quant_strategies(
    style: str | None = None,
    market: str | None = None,
) -> dict[str, Any]:
    """List built-in and saved quantitative/value investment strategies."""
    try:
        ensure_builtin_strategies()
        items = list_strategies(style=style, market=market)
        return {"count": len(items), "strategies": [_json_safe(s.to_dict()) for s in items]}
    except Exception as exc:
        raise ToolError(f"list_quant_strategies failed: {exc}") from exc


@mcp.tool()
def save_quant_strategy(
    strategy_id: str,
    name: str,
    style: str,
    market: str = "CN",
    description: str = "",
    params: dict[str, Any] | None = None,
    rules: dict[str, Any] | None = None,
    source: str = "agent",
) -> dict[str, Any]:
    """Validate and save a declarative StrategySpec (rules AST + params) to the strategy registry."""
    try:
        spec = StrategySpec(
            strategy_id=strategy_id,
            name=name,
            style=style,
            market=market,
            description=description,
            params=dict(params or {}),
            rules=dict(rules or {}),
            source=source,
        )
        saved = save_strategy(spec)
        return _json_safe(saved.to_dict())
    except Exception as exc:
        raise ToolError(f"save_quant_strategy failed: {exc}") from exc


def _compact_backtest_payload(payload: dict[str, Any], max_bytes: int = 16000) -> dict[str, Any]:
    """Ensure backtest/comparison payloads stay under Pi MCP's 20KB truncation threshold."""
    def _serialized_len(obj: dict[str, Any]) -> int:
        return len(json.dumps(obj, ensure_ascii=False, default=str))

    if _serialized_len(payload) <= max_bytes:
        return payload

    def _thin_curve(curve: list[Any], keep: int = 12) -> list[Any]:
        if keep <= 0:
            return []
        if len(curve) <= keep:
            return curve
        if keep == 1:
            return [curve[-1]]
        step = max(1, (len(curve) - 1) // (keep - 1))
        sampled = curve[::step]
        if len(sampled) > keep:
            sampled = sampled[: keep - 1] + [curve[-1]]
        elif sampled[-1] != curve[-1]:
            if len(sampled) >= keep:
                sampled[-1] = curve[-1]
            else:
                sampled.append(curve[-1])
        return sampled

    def _compact_pass(
        obj: dict[str, Any],
        *,
        curve_keep: int,
        res_curve_keep: int,
        trades_keep: int,
    ) -> dict[str, Any]:
        out = dict(obj)
        if isinstance(out.get("equity_curve"), list):
            out["equity_curve"] = _thin_curve(out["equity_curve"], keep=curve_keep)
        if isinstance(out.get("trades"), list) and len(out["trades"]) > trades_keep:
            out["trades_total"] = out.get("trades_total", len(out["trades"]))
            out["trades"] = out["trades"][-trades_keep:] if trades_keep > 0 else []
        if isinstance(out.get("aligned_equity_curves"), dict):
            out["aligned_equity_curves"] = {
                k: _thin_curve(v, keep=curve_keep) if isinstance(v, list) else v
                for k, v in out["aligned_equity_curves"].items()
            }
        elif isinstance(out.get("aligned_equity_curves"), list):
            out["aligned_equity_curves"] = _thin_curve(out["aligned_equity_curves"], keep=curve_keep)
        if isinstance(out.get("aligned_drawdown_curves"), dict):
            out["aligned_drawdown_curves"] = {
                k: _thin_curve(v, keep=curve_keep) if isinstance(v, list) else v
                for k, v in out["aligned_drawdown_curves"].items()
            }
        elif isinstance(out.get("aligned_drawdown_curves"), list):
            out["aligned_drawdown_curves"] = _thin_curve(out["aligned_drawdown_curves"], keep=curve_keep)
        if isinstance(out.get("benchmark_curve"), list):
            out["benchmark_curve"] = _thin_curve(out["benchmark_curve"], keep=curve_keep)
        if isinstance(out.get("trade_logs"), dict):
            out["trade_logs"] = {
                k: ((v[-trades_keep:] if trades_keep > 0 else []) if isinstance(v, list) and len(v) > trades_keep else v)
                for k, v in out["trade_logs"].items()
            }
        if isinstance(out.get("results"), dict):
            compacted_results = {}
            for sid, r in out["results"].items():
                if isinstance(r, dict):
                    cr = dict(r)
                    if isinstance(cr.get("equity_curve"), list):
                        cr["equity_curve"] = _thin_curve(cr["equity_curve"], keep=res_curve_keep)
                    if isinstance(cr.get("trades"), list) and len(cr["trades"]) > trades_keep:
                        cr["trades_total"] = cr.get("trades_total", len(cr["trades"]))
                        cr["trades"] = cr["trades"][-trades_keep:] if trades_keep > 0 else []
                    compacted_results[sid] = cr
                else:
                    compacted_results[sid] = r
            out["results"] = compacted_results
        return out

    out = _compact_pass(payload, curve_keep=12, res_curve_keep=8, trades_keep=20)
    if _serialized_len(out) <= max_bytes:
        return out

    for c_keep, rc_keep, t_keep in ((8, 5, 8), (4, 3, 3), (2, 2, 0), (0, 0, 0)):
        out = _compact_pass(out, curve_keep=c_keep, res_curve_keep=rc_keep, trades_keep=t_keep)
        if _serialized_len(out) <= max_bytes:
            return out

    # Final fallback: trim bulky secondary metadata if many strategies still exceed max_bytes
    for drop_key in ("trade_logs", "aligned_drawdown_curves", "aligned_equity_curves", "benchmark_curve", "assumptions"):
        if drop_key in out:
            out.pop(drop_key, None)
            if _serialized_len(out) <= max_bytes:
                return out
    if isinstance(out.get("results"), dict):
        for sid, r in list(out["results"].items()):
            if isinstance(r, dict):
                r.pop("assumptions", None)
                r.pop("trades", None)
                r.pop("equity_curve", None)
    return out


@mcp.tool()
def run_backtest_tool(
    strategy_id: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
    initial_capital: float = 100000.0,
    market: str = "CN",
    seed: int = 42,
) -> dict[str, Any]:
    """Run a deterministic historical backtest and return equity curve, trades, metrics, and hash."""
    try:
        ensure_builtin_strategies()
        norm_syms = [_normalize_symbol_for_market(s, market) for s in symbols if str(s).strip()]
        with _MARKET_DB_LOCK:
            with db_session() as con:
                ensure_baseline_seeded(con, seed_strategies=True)
                res = run_backtest(
                    strategy_id=strategy_id,
                    symbols=norm_syms if norm_syms else symbols,
                    start_date=start_date,
                    end_date=end_date,
                    initial_capital=initial_capital,
                    seed=seed,
                    market=market,
                    con=con,
                )
        return _compact_backtest_payload(_json_safe(res.to_dict()))
    except StrategyNotFoundError as exc:
        raise ToolError(f"Strategy not found: {exc}") from exc
    except Exception as exc:
        raise ToolError(f"run_backtest_tool failed: {exc}") from exc


@mcp.tool()
def compare_strategies_tool(
    strategy_ids: list[str],
    symbols: list[str],
    start_date: str,
    end_date: str,
    initial_capital: float = 100000.0,
    market: str = "CN",
    sort_by: str = "sharpe_ratio",
) -> dict[str, Any]:
    """Compare multiple quantitative/value strategies over identical symbols and date range."""
    try:
        ensure_builtin_strategies()
        norm_syms = [_normalize_symbol_for_market(s, market) for s in symbols if str(s).strip()]
        with _MARKET_DB_LOCK:
            with db_session() as con:
                ensure_baseline_seeded(con, seed_strategies=True)
                res = compare_strategies(
                    strategy_ids=strategy_ids,
                    symbols=norm_syms if norm_syms else symbols,
                    start_date=start_date,
                    end_date=end_date,
                    initial_capital=initial_capital,
                    market=market,
                    sort_by=sort_by,
                    con=con,
                )
        return _compact_backtest_payload(_json_safe(res.to_dict()))
    except Exception as exc:
        raise ToolError(f"compare_strategies_tool failed: {exc}") from exc


@mcp.tool()
def analyze_portfolio_risk(
    holdings: list[dict[str, Any]],
    price_history: dict[str, Any] | None = None,
    days: int = 120,
    confidence: float = 0.95,
) -> dict[str, Any]:
    """Compute portfolio volatility, 95% VaR/CVaR, correlation matrix, HHI concentration, and warnings."""
    try:
        if holdings is not None and not isinstance(holdings, list):
            raise ValueError(f"holdings must be a list of holding objects, got {type(holdings).__name__}")
        if price_history is not None and not isinstance(price_history, dict):
            raise ValueError(f"price_history must be a mapping of symbol -> price series, got {type(price_history).__name__}")
        raw_holdings = list(holdings or [])
        us_syms = {
            str(h.get("symbol") or "").strip().upper()
            for h in raw_holdings
            if isinstance(h, dict) and str(h.get("market") or "").strip().upper() == "US"
        }
        norm_holdings: list[dict[str, Any]] = []
        for h in raw_holdings:
            if not isinstance(h, dict):
                norm_holdings.append(h)
                continue
            h_copy = dict(h)
            raw_sym = str(h_copy.get("symbol") or "").strip()
            if raw_sym:
                h_mkt = str(h_copy.get("market") or "").strip().upper() or None
                h_copy["symbol"] = _normalize_symbol_for_market(raw_sym, h_mkt)
            norm_holdings.append(h_copy)

        history = price_history
        if history is None:
            symbols = sorted(
                {
                    str(h.get("symbol") or "").strip().upper()
                    for h in norm_holdings
                    if isinstance(h, dict) and str(h.get("symbol") or "").strip()
                }
            )
            history = {s: [] for s in symbols}
            if symbols:
                end_dt = date.today()
                start_dt = end_dt - timedelta(days=max(30, int(days) * 2))
                with _MARKET_DB_LOCK:
                    with db_session() as con:
                        ensure_baseline_seeded(con)
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
                    history.setdefault(sym, []).append({"date": str(row["date"]), "close": float(row["close"])})
        else:
            history = {
                (
                    str(k).strip().upper()
                    if str(k).strip().upper() in us_syms
                    else _normalize_symbol_for_market(str(k), None)
                ): v
                for k, v in history.items()
            }
        res = compute_portfolio_risk(holdings=norm_holdings, price_history=history, confidence=confidence)
        return _json_safe(res)
    except Exception as exc:
        raise ToolError(f"analyze_portfolio_risk failed: {exc}") from exc


@mcp.tool()
def paper_account_status(
    price_overrides: dict[str, float] | None = None,
    include_orders: bool = True,
    include_fills: bool = True,
    limit: int = 20,
) -> dict[str, Any]:
    """Return persistent paper-trading account cash, positions, PnL, rules, and recent orders/fills."""
    try:
        svc = PaperAccountService()
        summary = svc.get_account_summary(price_overrides=price_overrides)
        if include_orders:
            summary["orders"] = svc.get_orders(limit=int(limit))
        if include_fills:
            summary["fills"] = svc.get_fills(limit=int(limit))
        summary["simplification_assumptions"] = svc.get_simplification_assumptions()
        return _json_safe(summary)
    except Exception as exc:
        raise ToolError(f"paper_account_status failed: {exc}") from exc


@mcp.tool()
def submit_paper_order(
    symbol: str,
    side: str,
    quantity: int,
    order_type: str = "market",
    limit_price: float | None = None,
    market: str = "CN",
    trade_date: str | None = None,
    reference_bar: dict[str, Any] | None = None,
    strategy_id: str | None = None,
) -> dict[str, Any]:
    """Submit a simulated paper-trading order enforcing T+1, lot sizes, price limits, and fees."""
    eff_bar = reference_bar
    mkt_u = str(market).strip().upper() if market is not None else "CN"
    sym_u = _normalize_symbol_for_market(symbol, mkt_u) if mkt_u else str(symbol or "").strip().upper()
    if eff_bar is None and sym_u:
        end_dt = (
            trade_date.strip()[:10]
            if isinstance(trade_date, str) and len(trade_date.strip()) >= 10
            else None
        )
        try:
            with _MARKET_DB_LOCK:
                with db_session() as con:
                    ensure_baseline_seeded(con)
                    bars = load_bars_from_duckdb(
                        [sym_u],
                        end_date=end_dt,
                        market=mkt_u or "CN",
                        con=con,
                    )
                    if not bars and end_dt:
                        bars = load_bars_from_duckdb([sym_u], market=mkt_u or "CN", con=con)
            if bars:
                latest = bars[-1]
                prev_c = float(bars[-2]["close"]) if len(bars) > 1 else float(latest["close"])
                eff_bar = {
                    "open": float(latest["open"]),
                    "high": float(latest["high"]),
                    "low": float(latest["low"]),
                    "close": float(latest["close"]),
                    "prev_close": prev_c,
                }
        except Exception as exc:
            logger.debug("Optional market_bars lookup for %s failed: %s", sym_u, exc)

    try:
        svc = PaperAccountService()
        res = svc.submit_order(
            symbol=sym_u if sym_u else symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            market=market,
            trade_date=trade_date,
            reference_bar=eff_bar,
            strategy_id=strategy_id,
        )
    except Exception as exc:
        raise ToolError(f"submit_paper_order failed: {exc}") from exc

    if not res.get("accepted"):
        raise ToolError(f"Order rejected ({res.get('reject_code', 'rejected')}): {res.get('reason', 'invalid order')}")

    return _json_safe(res)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
