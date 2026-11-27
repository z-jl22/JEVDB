"""Contract reference harness for pre-implementation E2E self-validation (`--e2e-mode=contract`).

This module implements `PROJECT.md § Interface Contracts` faithfully in-process so the E2E suite
can be validated end-to-end before M1-M5 implementation completes.

IMPORTANT: In `--e2e-mode=live`, tests MUST NEVER fall back to this module for `stock_research/*`
targets. Only `contract` mode (or `auto` resolving to `contract` when targets do not exist yet)
uses these contract fixtures.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
import sqlite3
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import jevdb

from stock_research.config import BASELINE_DATA_PATH, DEFAULT_REPO_ROOT

REPO_ROOT = DEFAULT_REPO_ROOT
STATIC_PRICES_CSV = REPO_ROOT / BASELINE_DATA_PATH / "prices.csv"
STATIC_NEWS_CSV = REPO_ROOT / BASELINE_DATA_PATH / "news.csv"
STATIC_RULES_CSV = REPO_ROOT / BASELINE_DATA_PATH / "trading_rules.csv"

MCP_CONTRACT_TOOLS = [
    "jevdb_semantic_query",
    "fetch_market_data",
    "query_stored_market_data",
    "web_search",
    "web_fetch_url",
    "list_quant_strategies",
    "save_quant_strategy",
    "run_backtest_tool",
    "compare_strategies_tool",
    "analyze_portfolio_risk",
    "paper_account_status",
    "submit_paper_order",
]


# ---------------------------------------------------------------------------
# 1. Storage Schema & Static Baseline Loader (DuckDB via jevdb.connect)
# ---------------------------------------------------------------------------

def init_db(db_path: str | Path | None = None):
    path_str = ":memory:" if db_path is None else str(db_path)
    if path_str != ":memory:":
        Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    con = jevdb.connect(path_str)
    con.execute("""
        CREATE TABLE IF NOT EXISTS assets (
            symbol VARCHAR,
            market VARCHAR,
            name VARCHAR,
            sector VARCHAR,
            industry VARCHAR,
            updated_at TIMESTAMP,
            PRIMARY KEY (symbol, market)
        );
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
            PRIMARY KEY (symbol, market, trade_date)
        );
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
            PRIMARY KEY (market, sector_name, trade_date)
        );
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
            PRIMARY KEY (sector_name, symbol)
        );
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
        );
        CREATE TABLE IF NOT EXISTS edgar_filings (
            cik VARCHAR,
            symbol VARCHAR,
            accession_no VARCHAR,
            form VARCHAR,
            filing_date DATE,
            report_date DATE,
            primary_doc VARCHAR,
            title VARCHAR,
            url VARCHAR,
            PRIMARY KEY (accession_no)
        );
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
        );
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
        );
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            run_id VARCHAR PRIMARY KEY,
            source VARCHAR,
            target VARCHAR,
            status VARCHAR,
            rows_written INTEGER,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            error_msg VARCHAR
        );
    """)
    return con


def validate_ohlc_bar(open_p: float, high_p: float, low_p: float, close_p: float, volume: float) -> None:
    vals = (open_p, high_p, low_p, close_p)
    if any((not math.isfinite(v) or v <= 0) for v in vals):
        raise ValueError("OHLC prices must be finite and positive")
    if low_p > min(open_p, close_p) or high_p < max(open_p, close_p) or high_p < low_p:
        raise ValueError("Invalid OHLC bounds")
    if not math.isfinite(volume) or volume < 0:
        raise ValueError("Volume must be non-negative")


def load_static_baseline(con) -> dict[str, int]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    bars_loaded = 0
    news_loaded = 0
    seen_assets: dict[str, str] = {}

    with STATIC_PRICES_CSV.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(line for line in f if not line.startswith("#")):
            sym = row["symbol"].strip().upper()
            name = row.get("name", "").strip()
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            vol = float(row["volume_lots"]) * 100.0
            validate_ohlc_bar(o, h, l, c, vol)
            seen_assets[sym] = name
            con.execute(
                """
                INSERT OR REPLACE INTO market_bars
                (symbol, market, trade_date, open, high, low, close, volume, amount, source)
                VALUES (?, 'CN', ?, ?, ?, ?, ?, ?, ?, 'static_baseline')
                """,
                [sym, row["date"].strip(), o, h, l, c, vol, round(vol * c, 2)],
            )
            bars_loaded += 1

    for sym, name in seen_assets.items():
        sector = "白酒" if sym == "000858" else "保险"
        con.execute(
            """
            INSERT OR REPLACE INTO assets (symbol, market, name, sector, industry, updated_at)
            VALUES (?, 'CN', ?, ?, ?, ?)
            """,
            [sym, name, sector, sector, now],
        )

    with STATIC_NEWS_CSV.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(line for line in f if not line.startswith("#")):
            sym = row["symbol"].strip().upper()
            keys_list = [sym, "CN", "Q1", "财报", "增长", "static_baseline"]
            kw = json.dumps(keys_list, ensure_ascii=False)
            con.execute(
                """
                INSERT OR REPLACE INTO news_events
                (event_id, symbol, market, sector, published_at, title, summary, url, source, sentiment_label, keywords_json, keys)
                VALUES (?, ?, 'CN', ?, ?, ?, ?, ?, 'static_baseline', 'positive', ?, ?)
                """,
                [
                    row["news_id"].strip(),
                    sym,
                    "白酒" if sym == "000858" else "保险",
                    row["published_at"].strip(),
                    row["title"].strip(),
                    row["body"].strip(),
                    row.get("source_url", "").strip(),
                    kw,
                    keys_list,
                ],
            )
            news_loaded += 1
    return {"bars": bars_loaded, "news": news_loaded}


# ---------------------------------------------------------------------------
# 2. LLM Shim HTTP Server (json_schema strict -> json_object)
# ---------------------------------------------------------------------------

def _extract_json_payload(text: str) -> Any:
    cleaned = text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    return json.loads(cleaned)


class ShimServer:
    """Contract ShimServer matching `stock_research.storage.llm_shim.ShimServer`."""

    def __init__(
        self,
        address: tuple[str, int] | None = None,
        *,
        upstream_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float | None = None,
    ):
        self._bind_address = address or ("127.0.0.1", 0)
        self.upstream_url = upstream_url
        self.api_key = api_key or "contract-key"
        self.default_model = default_model or "deepseek-flash"
        self.timeout = timeout or 10.0
        self.captured_upstream_requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = self._bind_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> str:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.rstrip("/") in ("/health", "/v1/health"):
                    encoded = json.dumps({"status": "ok"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                req_json = json.loads(raw.decode("utf-8"))

                translated = dict(req_json)
                rf = req_json.get("response_format")
                if isinstance(rf, dict) and rf.get("type") == "json_schema":
                    schema_obj = rf.get("json_schema", {}).get("schema", {})
                    translated["response_format"] = {"type": "json_object"}
                    msgs = list(translated.get("messages", []))
                    msgs.append({
                        "role": "system",
                        "content": f"Return strictly valid JSON matching schema: {json.dumps(schema_obj, ensure_ascii=False)}",
                    })
                    translated["messages"] = msgs

                if parent.upstream_url:
                    parent.captured_upstream_requests.append(translated)
                    target = parent.upstream_url.rstrip("/") + "/chat/completions"
                    http_req = Request(
                        target,
                        data=json.dumps(translated, ensure_ascii=False).encode("utf-8"),
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {parent.api_key}"},
                        method="POST",
                    )
                    try:
                        with urlopen(http_req, timeout=parent.timeout) as resp:
                            up_body = json.loads(resp.read().decode("utf-8"))
                    except Exception as exc:
                        err = json.dumps({"error": f"upstream error: {exc}"}).encode("utf-8")
                        self.send_response(502)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(err)))
                        self.end_headers()
                        self.wfile.write(err)
                        return

                    choice = (up_body.get("choices") or [{}])[0]
                    msg = dict(choice.get("message") or {})
                    msg.pop("reasoning_content", None)
                    content = msg.get("content", "")
                    try:
                        parsed = _extract_json_payload(content)
                        msg["content"] = json.dumps(parsed, ensure_ascii=False)
                    except Exception:
                        err = json.dumps({"error": "upstream returned malformed non-JSON content"}).encode("utf-8")
                        self.send_response(502)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(err)))
                        self.end_headers()
                        self.wfile.write(err)
                        return

                    out_payload = {
                        "id": up_body.get("id", "shim-1"),
                        "object": "chat.completion",
                        "model": up_body.get("model", translated.get("model", parent.default_model)),
                        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                    }
                else:
                    user_msg = ""
                    for m in translated.get("messages", []):
                        if m.get("role") == "user":
                            user_msg = m.get("content", "")
                    user_data = json.loads(user_msg) if user_msg.startswith("{") else {"items": [{}]}
                    items = user_data.get("items", [{}])
                    orig_rf = rf if isinstance(rf, dict) else {}
                    orig_item_schema = (
                        orig_rf.get("json_schema", {})
                        .get("schema", {})
                        .get("properties", {})
                        .get("answers", {})
                        .get("items", {})
                    )
                    criteria = user_data.get("criteria")
                    choices: list[Any] | None = None
                    if isinstance(criteria, list):
                        choices = criteria
                    elif isinstance(criteria, dict) and isinstance(criteria.get("choices"), list):
                        choices = criteria["choices"]
                    elif isinstance(orig_item_schema.get("enum"), list):
                        choices = orig_item_schema["enum"]

                    answers = []
                    for item in items:
                        txt = json.dumps(item, ensure_ascii=False).lower()
                        is_pos = ("positive" in txt or "good" in txt or "增长" in txt)
                        if choices:
                            picked = next((c for c in choices if str(c).lower() in txt), choices[0])
                            answers.append(picked)
                        elif orig_item_schema.get("type") == "number":
                            hi = float(orig_item_schema.get("maximum", 1))
                            lo = float(orig_item_schema.get("minimum", 0))
                            answers.append(hi if (hi > 1 and is_pos) else (0.9 if is_pos else lo))
                        else:
                            answers.append(is_pos)
                    out_payload = {
                        "id": "shim-offline",
                        "object": "chat.completion",
                        "model": translated.get("model", parent.default_model),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": json.dumps({"answers": answers})},
                                "finish_reason": "stop",
                            }
                        ],
                    }

                encoded = json.dumps(out_payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(self._bind_address, Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


_CONTRACT_GLOBAL_SHIM: ShimServer | None = None


def ensure_shim_running(settings: Any = None) -> str:
    global _CONTRACT_GLOBAL_SHIM
    if _CONTRACT_GLOBAL_SHIM is None or _CONTRACT_GLOBAL_SHIM._server is None:
        _CONTRACT_GLOBAL_SHIM = ShimServer(("127.0.0.1", 0))
        _CONTRACT_GLOBAL_SHIM.start()
    return _CONTRACT_GLOBAL_SHIM.url


class ContractLLMShimServer:
    def __init__(self, upstream_url: str | None = None):
        self.upstream_url = upstream_url
        self.captured_upstream_requests: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    def start(self) -> int:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                req_json = json.loads(raw.decode("utf-8"))

                # Translate OpenAI json_schema strict payload -> DeepSeek json_object
                translated = dict(req_json)
                rf = req_json.get("response_format")
                if isinstance(rf, dict) and rf.get("type") == "json_schema":
                    schema_obj = rf.get("json_schema", {}).get("schema", {})
                    translated["response_format"] = {"type": "json_object"}
                    msgs = list(translated.get("messages", []))
                    msgs.append({
                        "role": "system",
                        "content": f"Return strictly valid JSON matching schema: {json.dumps(schema_obj, ensure_ascii=False)}",
                    })
                    translated["messages"] = msgs

                if parent.upstream_url:
                    parent.captured_upstream_requests.append(translated)
                    target = parent.upstream_url.rstrip("/") + "/chat/completions"
                    http_req = Request(
                        target,
                        data=json.dumps(translated, ensure_ascii=False).encode("utf-8"),
                        headers={"Content-Type": "application/json", "Authorization": "Bearer shim-key"},
                        method="POST",
                    )
                    try:
                        with urlopen(http_req, timeout=10) as resp:
                            up_body = json.loads(resp.read().decode("utf-8"))
                    except Exception as exc:
                        err = json.dumps({"error": f"upstream error: {exc}"}).encode("utf-8")
                        self.send_response(502)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(err)))
                        self.end_headers()
                        self.wfile.write(err)
                        return

                    choice = (up_body.get("choices") or [{}])[0]
                    msg = dict(choice.get("message") or {})
                    msg.pop("reasoning_content", None)
                    content = msg.get("content", "")
                    try:
                        parsed = _extract_json_payload(content)
                        msg["content"] = json.dumps(parsed, ensure_ascii=False)
                    except Exception:
                        err = json.dumps({"error": "upstream returned malformed non-JSON content"}).encode("utf-8")
                        self.send_response(502)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(err)))
                        self.end_headers()
                        self.wfile.write(err)
                        return

                    out_payload = {
                        "id": up_body.get("id", "shim-1"),
                        "object": "chat.completion",
                        "model": up_body.get("model", translated.get("model", "deepseek-flash")),
                        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                    }
                else:
                    # Deterministic offline JEVDB response generator
                    user_msg = ""
                    for m in translated.get("messages", []):
                        if m.get("role") == "user":
                            user_msg = m.get("content", "")
                    user_data = json.loads(user_msg) if user_msg.startswith("{") else {"items": [{}]}
                    items = user_data.get("items", [{}])
                    orig_rf = rf if isinstance(rf, dict) else {}
                    orig_item_schema = (
                        orig_rf.get("json_schema", {})
                        .get("schema", {})
                        .get("properties", {})
                        .get("answers", {})
                        .get("items", {})
                    )
                    criteria = user_data.get("criteria")
                    choices: list[Any] | None = None
                    if isinstance(criteria, list):
                        choices = criteria
                    elif isinstance(criteria, dict) and isinstance(criteria.get("choices"), list):
                        choices = criteria["choices"]
                    elif isinstance(orig_item_schema.get("enum"), list):
                        choices = orig_item_schema["enum"]

                    answers = []
                    for item in items:
                        txt = json.dumps(item, ensure_ascii=False).lower()
                        is_pos = ("positive" in txt or "good" in txt or "增长" in txt)
                        if choices:
                            picked = next((c for c in choices if str(c).lower() in txt), choices[0])
                            answers.append(picked)
                        elif orig_item_schema.get("type") == "number":
                            hi = float(orig_item_schema.get("maximum", 1))
                            lo = float(orig_item_schema.get("minimum", 0))
                            answers.append(hi if (hi > 1 and is_pos) else (0.9 if is_pos else lo))
                        else:
                            answers.append(is_pos)
                    out_payload = {
                        "id": "shim-offline",
                        "object": "chat.completion",
                        "model": translated.get("model", "deepseek-flash"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": json.dumps({"answers": answers})},
                                "finish_reason": "stop",
                            }
                        ],
                    }

                encoded = json.dumps(out_payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self.port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


# ---------------------------------------------------------------------------
# 3. Hybrid Semantic Query Bridge
# ---------------------------------------------------------------------------

def hybrid_semantic_query(
    query_spec: dict[str, Any],
    db_path: str | Path,
) -> dict[str, Any]:
    con = init_db(db_path)
    try:
        filters = dict(query_spec.get("filters") or {})
        symbol = filters.get("symbol") or query_spec.get("symbol")
        sector = filters.get("sector") or query_spec.get("sector")
        market = filters.get("market") or query_spec.get("market")
        source_filter = filters.get("source") or query_spec.get("source")
        source_scope = str(filters.get("source_scope") or "all").lower()
        limit = int(query_spec.get("limit") or filters.get("limit") or 50)
        target_keys = query_spec.get("query_keys") or query_spec.get("target_keys")
        condition = query_spec.get("condition", "Evaluate relevance and positive impact")

        clauses = []
        params: list[Any] = []
        if source_scope == "static":
            clauses.append("source = 'static_baseline'")
        elif source_scope == "dynamic":
            clauses.append("source <> 'static_baseline'")
        if symbol:
            clauses.append("UPPER(symbol) = ?")
            params.append(str(symbol).upper())
        if market:
            clauses.append("UPPER(market) = ?")
            params.append(str(market).upper())
        if sector:
            clauses.append("sector = ?")
            params.append(sector)
        if source_filter:
            clauses.append("source = ?")
            params.append(source_filter)
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        candidates = con.execute(
            f"""
            SELECT event_id, symbol, market, sector, CAST(published_at AS VARCHAR),
                   title, summary, url, source, sentiment_label, keywords_json,
                   COALESCE(keys, CAST([] AS VARCHAR[])) AS keys
            FROM news_events
            {where_sql}
            ORDER BY published_at DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()

        total_rows = len(candidates)
        retained: list[tuple[Any, ...]] = []
        pruned = 0
        if target_keys:
            for row in candidates:
                row_keys = list(row[11]) if row[11] else json.loads(row[10] or "[]")
                overlap = con.execute("SELECT jev_keys_overlap(?, ?)", [row_keys, list(target_keys)]).fetchone()[0]
                if overlap:
                    retained.append(row)
                else:
                    pruned += 1
        else:
            retained = candidates

        out_rows = []
        sources_set = set()
        for row in retained:
            sources_set.add(row[8])
            text_blob = f"{row[5]} {row[6]} {row[9] or ''}"
            decision = "positive" if ("增长" in text_blob or "positive" in text_blob or "突破" in text_blob or "green" in text_blob.lower()) else (
                "negative" if ("下滑" in text_blob or "negative" in text_blob or "风险" in text_blob) else "neutral"
            )
            out_rows.append({
                "record_id": row[0],
                "record_type": "news_events",
                "event_id": row[0],
                "symbol": row[1],
                "market": row[2],
                "sector": row[3],
                "published_at": row[4],
                "title": row[5],
                "summary": row[6],
                "url": row[7],
                "source": row[8],
                "semantic_decision": decision,
                "condition": condition,
            })

        return {
            "rows": out_rows,
            "stats": {
                "total_rows": total_rows,
                "pruned_rows": pruned,
                "evaluated_rows": len(retained),
                "sbf_enabled": bool(target_keys),
            },
            "backend": query_spec.get("backend") or "contract_hybrid_jevdb",
            "sources": sorted(sources_set),
        }
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 4. Ingestors & Scheduler (Deterministic Contract Data)
# ---------------------------------------------------------------------------

class Push2RateLimitError(RuntimeError):
    """Simulated EastMoney push2 failure triggering fallback routing."""


def _record_run(con, source: str, target: str, status: str, rows_written: int, error_msg: str = "") -> str:
    run_id = f"run_{uuid4().hex[:10]}"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    con.execute(
        """
        INSERT INTO ingestion_runs (run_id, source, target, status, rows_written, started_at, finished_at, error_msg)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [run_id, source, target, status, rows_written, now, now, error_msg],
    )
    return run_id


def ingest_a_share_sector(
    sector: str = "电力",
    days: int = 30,
    db_path: str | Path | None = None,
    simulate_push2_error: bool = False,
) -> dict[str, Any]:
    con = init_db(db_path)
    try:
        source_used = "akshare_ths_sina_fallback" if simulate_push2_error else "akshare_ths"
        if simulate_push2_error:
            # Verify fallback path activates cleanly instead of failing on push2
            _ = Push2RateLimitError("push2.eastmoney.com blocked; falling back to THS/Sina")

        today = date(2025, 5, 15)
        rows_written = 0
        constituents = [
            ("600900", "长江电力", 29.45, 2.18, 21.4, 2.85, 7.2e11),
            ("600011", "华能国际", 7.82, 1.56, 13.8, 1.42, 1.2e11),
            ("601985", "中国核电", 9.64, 1.92, 16.2, 1.75, 1.8e11),
        ]
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        for d_off in range(min(max(days, 3), 10)):
            d = (today - timedelta(days=d_off)).isoformat()
            chg = round(1.85 - d_off * 0.25, 2)
            con.execute(
                """
                INSERT OR REPLACE INTO sector_snapshots
                (market, sector_name, sector_code, trade_date, change_pct, volume, turnover, leader_symbol, payload_json, source)
                VALUES ('CN', ?, 'BK0428', ?, ?, ?, ?, '600900', ?, ?)
                """,
                [sector, d, chg, 2.45e8, 3.12e10, json.dumps({"days": days, "fallback": simulate_push2_error}), source_used],
            )
            rows_written += 1

        for sym, name, price, chg, pe, pb, mcap in constituents:
            con.execute(
                """
                INSERT OR REPLACE INTO sector_constituents
                (sector_name, symbol, name, price, change_pct, pe, pb, market_cap, updated_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [sector, sym, name, price, chg, pe, pb, mcap, now, source_used],
            )
            con.execute(
                """
                INSERT OR REPLACE INTO assets (symbol, market, name, sector, industry, updated_at)
                VALUES (?, 'CN', ?, ?, ?, ?)
                """,
                [sym, name, sector, sector, now],
            )
            rows_written += 2

        for sym, title, summary in [
            ("600900", f"{sector}行业装机与发电量稳步增长", "水电来水改善叠加绿电消纳政策支持，板块近3日涨幅1.85%，成交额放大。"),
            ("600011", f"火电盈利修复带动{sector}板块走强", "煤价回落改善火电毛利，多只电力龙头股连续三日净流入。"),
        ]:
            sec_keys = [sector, sym, "发电量", "增长", "CN"]
            con.execute(
                """
                INSERT OR REPLACE INTO news_events
                (event_id, symbol, market, sector, published_at, title, summary, url, source, sentiment_label, keywords_json, keys)
                VALUES (?, ?, 'CN', ?, ?, ?, ?, ?, ?, 'positive', ?, ?)
                """,
                [
                    f"cn_sec_{sym}_{today.isoformat()}",
                    sym,
                    sector,
                    f"{today.isoformat()} 09:30:00",
                    title,
                    summary,
                    f"https://finance.sina.com.cn/realstock/company/sh{sym}/nc.shtml",
                    source_used,
                    json.dumps(sec_keys, ensure_ascii=False),
                    sec_keys,
                ],
            )
            rows_written += 1

        run_id = _record_run(con, source_used, sector, "success", rows_written)
        return {"status": "success", "source": source_used, "sector": sector, "rows_written": rows_written, "run_id": run_id}
    finally:
        con.close()


def ingest_a_share_stock(
    symbol: str,
    days: int = 30,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    sym = (symbol or "").strip().upper()
    con = init_db(db_path)
    try:
        if not sym or not re.fullmatch(r"\d{6}", sym):
            run_id = _record_run(con, "akshare_cn", sym or "<empty>", "failed", 0, "Invalid A-share symbol")
            return {"status": "failed", "symbol": sym, "rows_written": 0, "error": "Invalid A-share symbol", "run_id": run_id}

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        name = "五粮液" if sym == "000858" else ("中国平安" if sym == "601318" else f"A股{sym}")
        con.execute(
            "INSERT OR REPLACE INTO assets (symbol, market, name, sector, industry, updated_at) VALUES (?, 'CN', ?, '消费', '白酒', ?)",
            [sym, name, now],
        )
        base = 128.0 if sym == "000858" else 50.0
        rows_written = 0
        start = date(2025, 4, 24)
        for i in range(min(max(days, 1), 30)):
            d = (start + timedelta(days=i)).isoformat()
            o = round(base + i * 0.25, 2)
            c = round(o + (0.35 if i % 2 == 0 else -0.15), 2)
            h = round(max(o, c) + 0.60, 2)
            l = round(min(o, c) - 0.45, 2)
            vol = 1200000.0 + i * 10000.0
            validate_ohlc_bar(o, h, l, c, vol)
            con.execute(
                """
                INSERT OR REPLACE INTO market_bars
                (symbol, market, trade_date, open, high, low, close, volume, amount, source)
                VALUES (?, 'CN', ?, ?, ?, ?, ?, ?, ?, 'akshare_sina')
                """,
                [sym, d, o, h, l, c, vol, round(vol * c, 2)],
            )
            rows_written += 1

        con.execute(
            """
            INSERT OR REPLACE INTO fundamentals
            (symbol, market, report_date, pe_ttm, pb, roe, revenue, net_income, debt_ratio, metrics_json, source, updated_at)
            VALUES (?, 'CN', '2025-03-31', 15.8, 3.6, 22.4, 36940000000.0, 14860000000.0, 0.24, ?, 'akshare_ths', ?)
            """,
            [sym, json.dumps({"pe_ttm": 15.8, "roe": 22.4}), now],
        )
        run_id = _record_run(con, "akshare_sina", sym, "success", rows_written)
        return {"status": "success", "symbol": sym, "rows_written": rows_written, "run_id": run_id}
    finally:
        con.close()


def ingest_us_stock(
    symbol: str,
    period: str = "1mo",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    sym = (symbol or "").strip().upper()
    con = init_db(db_path)
    try:
        if not sym or "INVALID" in sym or not re.fullmatch(r"[A-Z]{1,6}", sym):
            run_id = _record_run(con, "yfinance", sym or "<empty>", "failed", 0, f"Symbol not found: {sym}")
            return {"status": "failed", "symbol": sym, "rows_written": 0, "error": f"Symbol not found: {sym}", "run_id": run_id}

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        con.execute(
            "INSERT OR REPLACE INTO assets (symbol, market, name, sector, industry, updated_at) VALUES (?, 'US', ?, 'Technology', 'Consumer Tech', ?)",
            [sym, f"{sym} Inc.", now],
        )
        base = 210.0 if sym == "AAPL" else 420.0
        rows_written = 0
        start = date(2025, 4, 24)
        for i in range(20):
            d = (start + timedelta(days=i)).isoformat()
            o = round(base + i * 0.4 + (0.8 if sym == "AAPL" else -0.5) * (i % 3), 2)
            c = round(o + (0.65 if i % 2 == 0 else -0.35), 2)
            h = round(max(o, c) + 1.1, 2)
            l = round(min(o, c) - 0.9, 2)
            vol = 4.5e7 + i * 1e5
            validate_ohlc_bar(o, h, l, c, vol)
            con.execute(
                """
                INSERT OR REPLACE INTO market_bars
                (symbol, market, trade_date, open, high, low, close, volume, amount, source)
                VALUES (?, 'US', ?, ?, ?, ?, ?, ?, ?, 'yfinance')
                """,
                [sym, d, o, h, l, c, vol, round(vol * c, 2)],
            )
            rows_written += 1

        con.execute(
            """
            INSERT OR REPLACE INTO fundamentals
            (symbol, market, report_date, pe_ttm, pb, roe, revenue, net_income, debt_ratio, metrics_json, source, updated_at)
            VALUES (?, 'US', '2025-03-31', 31.2, 42.5, 145.0, 95360000000.0, 24780000000.0, 0.78, ?, 'yfinance', ?)
            """,
            [sym, json.dumps({"pe_ttm": 31.2, "period": period}), now],
        )
        yf_keys = [sym, "services", "revenue", "growth", "US"]
        con.execute(
            """
            INSERT OR REPLACE INTO news_events
            (event_id, symbol, market, sector, published_at, title, summary, url, source, sentiment_label, keywords_json, keys)
            VALUES (?, ?, 'US', 'Technology', '2025-05-10 14:00:00', ?, ?, ?, 'yfinance', 'positive', ?, ?)
            """,
            [
                f"yf_{sym}_001",
                sym,
                f"{sym} posts resilient services revenue growth",
                f"{sym} quarterly revenue grew with expanding services margin.",
                f"https://finance.yahoo.com/quote/{sym}",
                json.dumps(yf_keys),
                yf_keys,
            ],
        )
        run_id = _record_run(con, "yfinance", sym, "success", rows_written + 2)
        return {"status": "success", "symbol": sym, "rows_written": rows_written + 2, "run_id": run_id}
    finally:
        con.close()


def ingest_sec_edgar(
    symbol: str,
    db_path: str | Path | None = None,
    sec_user_agent: str | None = "JEVDBResearch/0.1 (research@example.com)",
    simulate_403: bool = False,
) -> dict[str, Any]:
    sym = (symbol or "").strip().upper()
    con = init_db(db_path)
    try:
        if simulate_403 or not sec_user_agent or "@" not in sec_user_agent:
            msg = "HTTP 403 Forbidden: SEC EDGAR requires descriptive User-Agent header"
            run_id = _record_run(con, "sec_edgar", sym, "failed", 0, msg)
            return {"status": "failed", "symbol": sym, "rows_written": 0, "error_msg": msg, "run_id": run_id}

        cik_map = {"AAPL": "0000320193", "MSFT": "0000789019"}
        if sym not in cik_map:
            msg = f"Unknown SEC ticker: {sym}"
            run_id = _record_run(con, "sec_edgar", sym, "failed", 0, msg)
            return {"status": "failed", "symbol": sym, "rows_written": 0, "error_msg": msg, "run_id": run_id}

        cik = cik_map[sym]
        acc = f"{cik}-25-000057"
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{sym.lower()}-10q.htm"
        con.execute(
            """
            INSERT OR REPLACE INTO edgar_filings
            (cik, symbol, accession_no, form, filing_date, report_date, primary_doc, title, url)
            VALUES (?, ?, ?, '10-Q', '2025-05-02', '2025-03-31', ?, ?, ?)
            """,
            [cik, sym, acc, f"{sym.lower()}-10q.htm", f"{sym} Form 10-Q Q2 FY25", url],
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        con.execute(
            """
            INSERT OR REPLACE INTO fundamentals
            (symbol, market, report_date, pe_ttm, pb, roe, revenue, net_income, debt_ratio, metrics_json, source, updated_at)
            VALUES (?, 'US', '2025-03-31', NULL, NULL, NULL, 95360000000.0, 24780000000.0, 0.78, ?, 'sec_edgar', ?)
            """,
            [sym, json.dumps({"cik": cik, "xbrl_revenue": 95360000000.0}), now],
        )
        run_id = _record_run(con, "sec_edgar", sym, "success", 2)
        return {"status": "success", "symbol": sym, "cik": cik, "rows_written": 2, "run_id": run_id}
    finally:
        con.close()


def ingest_finnhub_news(
    symbol: str | None = None,
    category: str = "general",
    days: int = 7,
    db_path: str | Path | None = None,
    simulate_429: bool = False,
) -> dict[str, Any]:
    con = init_db(db_path)
    try:
        retries = 1 if simulate_429 else 0
        sym = (symbol or "MARKET").strip().upper()
        eid = f"fh_{sym}_{category}_001"
        fh_keys = [sym, "finnhub", category, "US"]
        con.execute(
            """
            INSERT OR REPLACE INTO news_events
            (event_id, symbol, market, sector, published_at, title, summary, url, source, sentiment_label, keywords_json, keys)
            VALUES (?, ?, 'US', 'Technology', '2025-05-12 10:00:00', ?, ?, ?, 'finnhub', 'positive', ?, ?)
            """,
            [
                eid,
                sym,
                f"{sym} Finnhub market update",
                f"Finnhub company news for {sym} over past {days} days.",
                f"https://finnhub.io/news/{sym}",
                json.dumps(fh_keys),
                fh_keys,
            ],
        )
        run_id = _record_run(con, "finnhub", sym, "success", 1)
        return {"status": "success", "symbol": sym, "rows_written": 1, "rate_limit_retries": retries, "run_id": run_id}
    finally:
        con.close()


def ingest_finnhub_earnings(
    from_date: str,
    to_date: str,
    symbol: str | None = None,
    db_path: str | Path | None = None,
    simulate_429: bool = False,
) -> dict[str, Any]:
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_from > d_to:
        raise ValueError(f"Invalid date window: from_date {from_date} > to_date {to_date}")
    row_sym = (symbol or "AAPL").strip().upper()
    con = init_db(db_path)
    try:
        retries = 1 if simulate_429 else 0
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        con.execute(
            """
            INSERT OR REPLACE INTO earnings_calendar
            (symbol, report_date, eps_estimate, eps_actual, revenue_estimate, revenue_actual, source, fetched_at)
            VALUES (?, ?, 1.62, 1.65, 94500000000.0, 95360000000.0, 'finnhub', ?)
            """,
            [row_sym, from_date, now],
        )
        run_id = _record_run(con, "finnhub_earnings", f"{from_date}..{to_date}", "success", 1)
        return {"status": "success", "rows_written": 1, "rate_limit_retries": retries, "run_id": run_id}
    finally:
        con.close()


class ContractSchedulerService:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_results: list[dict[str, Any]] = []

    def trigger_once(self, jobs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        selected = jobs or [
            {"type": "a_share_sector", "kwargs": {"sector": "电力", "days": 3}},
            {"type": "finnhub_news", "kwargs": {"symbol": "AAPL", "days": 7}},
        ]
        results: list[dict[str, Any]] = []
        for job in selected:
            jtype = str(job.get("type") or job.get("kind") or "unnamed_job")
            kw = dict(job.get("kwargs") or {})
            if jtype == "a_share_sector":
                out = ingest_a_share_sector(kw.get("sector", job.get("sector", "电力")), int(kw.get("days", job.get("days", 3))), self.db_path)
            elif jtype == "finnhub_news":
                out = ingest_finnhub_news(kw.get("symbol", job.get("symbol", "AAPL")), days=int(kw.get("days", job.get("days", 7))), db_path=self.db_path)
            elif jtype == "us_stock":
                out = ingest_us_stock(kw.get("symbol", job.get("symbol", "AAPL")), db_path=self.db_path)
            elif jtype == "a_share_stock":
                out = ingest_a_share_stock(kw.get("symbol", job.get("symbol", "000858")), int(kw.get("days", job.get("days", 30))), db_path=self.db_path)
            else:
                con = init_db(self.db_path)
                err_msg = f"No handler registered for job {jtype!r}"
                run_id = _record_run(con, "scheduler", jtype, "error", 0, err_msg)
                con.close()
                results.append({"job": jtype, "status": "error", "rows_written": 0, "run_id": run_id, "error": err_msg})
                continue
            results.append({
                "job": jtype,
                "status": out.get("status", "success"),
                "rows_written": int(out.get("rows_written") or 0),
                "run_id": out.get("run_id", ""),
                "error": None,
                "detail": out,
            })
        self._last_results = results
        return results

    def start_background(self, interval_seconds: float = 60.0) -> None:
        self._stop_event.clear()

        def _loop():
            while not self._stop_event.wait(interval_seconds):
                self.trigger_once()

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def status(self) -> dict[str, Any]:
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "last_results": list(self._last_results),
        }


# ---------------------------------------------------------------------------
# 5. Web Tools (SSRF-Safe HTML -> Markdown & Search)
# ---------------------------------------------------------------------------

def web_search(query: str) -> list[dict[str, str]]:
    if not query or not query.strip():
        raise ValueError("Search query must be non-empty")
    q = query.strip()
    return [
        {
            "title": f"{q} — 市场研报与行业动态",
            "url": "https://finance.sina.com.cn/stock/hyyj/",
            "snippet": f"围绕 {q} 的最新公开行情、行业政策与机构研报摘要。",
        },
        {
            "title": f"{q} — JEVDB Semantic SQL Reference",
            "url": "https://jevdb.org/",
            "snippet": "JEVDB scalable semantic SQL filters and SBF key pruning over DuckDB.",
        },
    ]


def _validate_public_http_url(url: str, allow_loopback_for_test: bool = False) -> str:
    if not url or not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be non-empty")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked non-HTTP scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("URL missing hostname")
    host = parsed.hostname.lower()
    if not allow_loopback_for_test:
        if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
            raise ValueError(f"SSRF blocked loopback/local host: {host}")
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValueError(f"SSRF blocked private/reserved IP: {host}")
        except ValueError as exc:
            if "SSRF blocked" in str(exc):
                raise
            # Hostname: resolve DNS and check all resolved IPs
            try:
                infos = socket.getaddrinfo(host, None)
                for info in infos:
                    resolved_ip = ipaddress.ip_address(info[4][0].split("%")[0])
                    if resolved_ip.is_private or resolved_ip.is_loopback or resolved_ip.is_link_local or resolved_ip.is_reserved:
                        raise ValueError(f"SSRF blocked resolved private IP {resolved_ip} for host {host}")
            except socket.gaierror:
                pass
    return url.strip()


def web_fetch_url(url: str, allow_loopback_for_test: bool = False) -> str:
    safe_url = _validate_public_http_url(url, allow_loopback_for_test=allow_loopback_for_test)
    req = Request(safe_url, headers={"User-Agent": "JEVDBResearchBot/0.1"})
    with urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    # Strip <script> and <style> blocks
    html = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", "", html, flags=re.IGNORECASE)
    html = re.sub(r"<h1[^>]*>(.*?)</h1>", r"\n# \1\n", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n## \1\n", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<p[^>]*>(.*?)</p>", r"\n\1\n", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", html)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# 6. Quant Strategy Registry, Reproducible Backtester & Risk Analytics
# ---------------------------------------------------------------------------

@dataclass
class StrategySpec:
    strategy_id: str
    name: str
    style: str  # "value" | "quant_daily" | "sentiment_hybrid"
    market: str  # "CN" | "US"
    description: str
    params: dict[str, Any] = field(default_factory=dict)
    rules: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = "builtin"

    def __post_init__(self):
        if not self.strategy_id or not self.strategy_id.strip():
            raise ValueError("strategy_id must be non-empty")
        if self.market not in ("CN", "US"):
            raise ValueError(f"Unsupported market: {self.market!r}")
        if self.style not in ("value", "quant_daily", "sentiment_hybrid"):
            raise ValueError(f"Unsupported strategy style: {self.style!r}")
        if not isinstance(self.rules, dict):
            raise ValueError("rules must be a dict")
        unknown_top = set(self.rules.keys()) - {"buy", "sell", "ranking"}
        if unknown_top:
            raise ValueError(f"Unknown top-level rule keys: {sorted(unknown_top)}")


def save_strategy(spec: StrategySpec, strategy_dir: Path) -> StrategySpec:
    strategy_dir.mkdir(parents=True, exist_ok=True)
    path = strategy_dir / f"{spec.strategy_id}.json"
    path.write_text(json.dumps(asdict(spec), ensure_ascii=False, indent=2), encoding="utf-8")
    return spec


def get_strategy(strategy_id: str, strategy_dir: Path) -> StrategySpec:
    path = strategy_dir / f"{strategy_id}.json"
    if not path.exists():
        raise KeyError(f"Strategy not found: {strategy_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return StrategySpec(**data)


def ensure_builtin_strategies(strategy_dir: Path) -> list[StrategySpec]:
    builtins = [
        StrategySpec(
            strategy_id="value_quality_margin_of_safety",
            name="价值质量与安全边际策略",
            style="value",
            market="CN",
            description="基于低PE/PB、高ROE及安全边际折扣的偏中长线价值投资策略",
            params={"max_pe": 25.0, "min_roe": 12.0, "hold_days": 5, "position_weight": 0.4},
            rules={
                "buy": {"field": "zscore_close", "op": "<=", "value": -0.3},
                "sell": {"field": "zscore_close", "op": ">=", "value": 1.2},
                "ranking": {"field": "earnings_yield", "order": "desc"},
            },
            source="builtin",
        ),
        StrategySpec(
            strategy_id="cn_daily_momentum_sentiment",
            name="A股日频动量与语义情绪策略",
            style="quant_daily",
            market="CN",
            description="结合JEVDB公告语义信号与短周期价格动量的A股日频量化策略",
            params={"lookback": 2, "momentum_threshold": 0.002, "position_weight": 0.4},
            rules={
                "buy": {"field": "momentum_return", "op": ">=", "value": 0.002},
                "sell": {"field": "momentum_return", "op": "<", "value": 0.0},
                "ranking": {"field": "momentum_return", "order": "desc"},
            },
            source="builtin",
        ),
        StrategySpec(
            strategy_id="cn_mean_reversion_daily",
            name="A股日频均值回归策略",
            style="quant_daily",
            market="CN",
            description="捕捉超跌反弹的A股日频均值回归策略",
            params={"lookback": 3, "z_entry": -0.01, "position_weight": 0.3},
            rules={
                "buy": {"field": "zscore_close", "op": "<=", "value": -0.8},
                "sell": {"field": "zscore_close", "op": ">=", "value": 0.6},
                "ranking": {"field": "zscore_close", "order": "asc"},
            },
            source="builtin",
        ),
    ]
    for s in builtins:
        if not (strategy_dir / f"{s.strategy_id}.json").exists():
            save_strategy(s, strategy_dir)
    return list_strategies(strategy_dir)


def list_strategies(strategy_dir: Path) -> list[StrategySpec]:
    if not strategy_dir.exists():
        return []
    out = []
    for p in sorted(strategy_dir.glob("*.json")):
        out.append(StrategySpec(**json.loads(p.read_text(encoding="utf-8"))))
    return out


def compute_risk_metrics(equity_curve: list[dict[str, Any]], trades: list[dict[str, Any]] | None = None) -> dict[str, float]:
    if not equity_curve:
        raise ValueError("equity_curve must not be empty")
    equities = [float(row["equity"]) for row in equity_curve]
    initial = equities[0]
    if initial <= 0:
        raise ValueError("Initial equity must be positive")
    final = equities[-1]
    total_return = (final / initial) - 1.0
    n_periods = max(len(equities) - 1, 1)
    cagr = ((final / initial) ** (252.0 / n_periods) - 1.0) if final > 0 and len(equities) > 1 else total_return

    returns = []
    for i in range(1, len(equities)):
        prev = equities[i - 1]
        returns.append((equities[i] / prev - 1.0) if prev > 0 else 0.0)

    if len(returns) >= 1:
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        daily_vol = math.sqrt(var)
        volatility = daily_vol * math.sqrt(252.0)
        sharpe = (mean_r / daily_vol) * math.sqrt(252.0) if daily_vol > 1e-12 else 0.0
        downside = [min(0.0, r) for r in returns]
        down_dev = math.sqrt(sum(d * d for d in downside) / len(downside))
        sortino = (mean_r / down_dev) * math.sqrt(252.0) if down_dev > 1e-12 else (sharpe if mean_r > 0 else 0.0)
    else:
        volatility = 0.0
        sharpe = 0.0
        sortino = 0.0

    peak = equities[0]
    max_dd = 0.0
    cur_dur = 0
    max_dur = 0
    for eq in equities:
        if eq >= peak:
            peak = eq
            cur_dur = 0
        else:
            cur_dur += 1
            max_dur = max(max_dur, cur_dur)
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

    calmar = (cagr / max_dd) if max_dd > 1e-12 else 0.0

    if returns:
        sorted_r = sorted(returns)
        idx = max(0, min(len(sorted_r) - 1, int(math.floor(0.05 * len(sorted_r)))))
        var_95 = sorted_r[idx]
        tail = [r for r in sorted_r if r <= var_95]
        cvar_95 = sum(tail) / len(tail) if tail else var_95
    else:
        var_95 = 0.0
        cvar_95 = 0.0

    pnls = [float(t.get("pnl", 0.0)) for t in (trades or []) if t.get("side") == "SELL"]
    if pnls:
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]
        win_rate = len(wins) / len(pnls)
        profit_factor = (sum(wins) / sum(losses)) if losses else (float(len(wins)) if wins else 0.0)
    else:
        win_rate = 0.0
        profit_factor = 0.0

    return {
        "total_return": round(total_return, 8),
        "cagr": round(cagr, 8),
        "annualized_volatility": round(volatility, 8),
        "sharpe_ratio": round(sharpe, 8),
        "sortino_ratio": round(sortino, 8),
        "calmar_ratio": round(calmar, 8),
        "max_drawdown": round(max_dd, 8),
        "max_drawdown_duration_days": int(max_dur),
        "var_95": round(var_95, 8),
        "cvar_95": round(cvar_95, 8),
        "win_rate": round(win_rate, 8),
        "profit_factor": round(profit_factor, 8),
    }


def compute_portfolio_risk(holdings: list[dict[str, Any]], price_history: dict[str, list[float]]) -> dict[str, Any]:
    warnings: list[str] = []
    norm_holdings: list[dict[str, Any]] = []
    raw_weights: list[tuple[str, float, str]] = []
    for h in holdings or []:
        sym = str(h.get("symbol") or "").strip().upper()
        if not sym:
            continue
        w = float(h.get("weight", h.get("market_value", h.get("quantity", 0.0))))
        if w > 0:
            raw_weights.append((sym, w, str(h.get("sector", "General"))))

    total_w = sum(w for _, w, _ in raw_weights)
    if not raw_weights or total_w <= 0:
        return {
            "symbols": [],
            "weights": {},
            "hhi": 0.0,
            "portfolio_volatility": 0.0,
            "var_95": 0.0,
            "cvar_95": 0.0,
            "correlation_matrix": {},
            "sector_exposure": {},
            "warnings": ["empty_or_zero_portfolio"],
        }

    for sym, w, sec in raw_weights:
        norm_holdings.append({"symbol": sym, "weight": w / total_w, "sector": sec})

    symbols = [h["symbol"] for h in norm_holdings]
    missing_syms = [sym for sym in symbols if sym not in price_history or len(price_history[sym]) < 2]
    for ms in missing_syms:
        warnings.append(f"missing_or_short_price_history:{ms}")

    hhi = sum(h["weight"] ** 2 for h in norm_holdings)
    sector_exposure: dict[str, float] = {}
    for h in norm_holdings:
        sector_exposure[h["sector"]] = round(sector_exposure.get(h["sector"], 0.0) + h["weight"], 6)

    present = [h for h in norm_holdings if h["symbol"] not in missing_syms]
    returns_by_sym: dict[str, list[float]] = {}
    if present:
        min_len = min(len(price_history[h["symbol"]]) for h in present)
        for h in present:
            px = price_history[h["symbol"]][-min_len:]
            returns_by_sym[h["symbol"]] = [(px[i] / px[i - 1] - 1.0) for i in range(1, len(px))]
        t_len = len(returns_by_sym[present[0]["symbol"]])
    else:
        t_len = 0
    for h in norm_holdings:
        if h["symbol"] not in returns_by_sym:
            returns_by_sym[h["symbol"]] = [0.0] * t_len

    if t_len >= 1:
        port_returns = [
            sum(h["weight"] * returns_by_sym[h["symbol"]][t] for h in norm_holdings)
            for t in range(t_len)
        ]
        mean_p = sum(port_returns) / len(port_returns)
        port_var = sum((r - mean_p) ** 2 for r in port_returns) / len(port_returns)
        port_vol = math.sqrt(port_var) * math.sqrt(252.0)

        sorted_pr = sorted(port_returns)
        idx = max(0, min(len(sorted_pr) - 1, int(math.floor(0.05 * len(sorted_pr)))))
        var_95 = sorted_pr[idx]
        tail = [r for r in sorted_pr if r <= var_95]
        cvar_95 = sum(tail) / len(tail) if tail else var_95
    else:
        port_vol = 0.0
        var_95 = 0.0
        cvar_95 = 0.0

    corr_matrix: dict[str, dict[str, float]] = {}
    for s1 in symbols:
        corr_matrix[s1] = {}
        ri = returns_by_sym[s1]
        mi = sum(ri) / len(ri) if ri else 0.0
        si = math.sqrt(sum((x - mi) ** 2 for x in ri)) if ri else 0.0
        for s2 in symbols:
            if s1 == s2:
                corr_matrix[s1][s2] = 1.0
            elif t_len < 2 or s1 in missing_syms or s2 in missing_syms:
                corr_matrix[s1][s2] = 0.0
            else:
                rj = returns_by_sym[s2]
                mj = sum(rj) / len(rj)
                sj = math.sqrt(sum((x - mj) ** 2 for x in rj))
                if si <= 1e-12 or sj <= 1e-12:
                    corr_matrix[s1][s2] = 0.0
                else:
                    cov = sum((ri[k] - mi) * (rj[k] - mj) for k in range(len(ri)))
                    corr_matrix[s1][s2] = round(max(-1.0, min(1.0, cov / (si * sj))), 8)

    return {
        "symbols": symbols,
        "weights": {h["symbol"]: round(h["weight"], 6) for h in norm_holdings},
        "hhi": round(hhi, 6),
        "portfolio_volatility": round(port_vol, 8),
        "var_95": round(var_95, 8),
        "cvar_95": round(cvar_95, 8),
        "correlation_matrix": corr_matrix,
        "sector_exposure": sector_exposure,
        "warnings": warnings,
    }


def run_backtest(
    strategy_id: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
    initial_capital: float = 100000.0,
    seed: int = 42,
    market: str = "CN",
    db_path: str | Path | None = None,
    strategy_dir: Path | None = None,
) -> dict[str, Any]:
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial_capital must be positive and finite")
    if not symbols:
        raise ValueError("symbols must not be empty")
    if strategy_dir is None:
        raise ValueError("strategy_dir is required in contract mode")
    spec = get_strategy(strategy_id, strategy_dir)

    con = init_db(db_path)
    try:
        placeholders = ",".join("?" for _ in symbols)
        rows = con.execute(
            f"""
            SELECT symbol, CAST(trade_date AS VARCHAR), open, high, low, close, volume
            FROM market_bars
            WHERE market = ? AND symbol IN ({placeholders})
              AND trade_date >= ? AND trade_date <= ?
            ORDER BY trade_date ASC, symbol ASC
            """,
            [market, *[s.upper() for s in symbols], start_date, end_date],
        ).fetchall()
    finally:
        con.close()

    if not rows:
        raise ValueError(f"No market_bars found for {symbols} in {start_date}..{end_date}")

    bars_by_date: dict[str, dict[str, dict[str, float]]] = {}
    for sym, d_str, o, h, l, c, v in rows:
        bars_by_date.setdefault(d_str, {})[sym] = {
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(v),
        }

    sorted_dates = sorted(bars_by_date.keys())
    cash = float(initial_capital)
    holdings: dict[str, int] = {s.upper(): 0 for s in symbols}
    avg_cost: dict[str, float] = {s.upper(): 0.0 for s in symbols}
    last_buy_date: dict[str, str] = {}
    prev_closes: dict[str, float] = {}
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    peak = float(initial_capital)
    buy_rule = spec.rules.get("buy")
    never_trigger = buy_rule == {"const": False}

    first_day_avg = sum(bars_by_date[sorted_dates[0]][s]["close"] for s in bars_by_date[sorted_dates[0]]) / len(bars_by_date[sorted_dates[0]])

    for idx, d_str in enumerate(sorted_dates):
        day_bars = bars_by_date[d_str]
        if not never_trigger:
            for sym in sorted(day_bars.keys()):
                bar = day_bars[sym]
                prev_c = prev_closes.get(sym)
                if spec.style == "value":
                    should_buy = idx == 0 or (prev_c is not None and bar["close"] <= prev_c * 1.005 and holdings[sym] == 0)
                    should_sell = idx == len(sorted_dates) - 1 and holdings[sym] > 0 and spec.params.get("liquidate_end", False)
                else:
                    should_buy = (prev_c is None and idx == 0) or (prev_c is not None and bar["open"] >= prev_c * 0.998 and holdings[sym] == 0)
                    should_sell = prev_c is not None and bar["close"] < prev_c * 0.996 and holdings[sym] > 0

                if should_sell and holdings[sym] > 0 and (market != "CN" or last_buy_date.get(sym) != d_str):
                    qty = holdings[sym]
                    px = round(bar["close"] * 0.9995, 2)
                    notional = qty * px
                    fee = max(5.0, round(notional * 0.0003, 2)) + round(notional * 0.00001, 2) + round(notional * 0.0005, 2)
                    proceeds = notional - fee
                    pnl = proceeds - qty * avg_cost[sym]
                    cash = round(cash + proceeds, 2)
                    holdings[sym] = 0
                    trades.append({"date": d_str, "symbol": sym, "side": "SELL", "quantity": qty, "price": px, "fee": fee, "pnl": round(pnl, 2)})
                elif should_buy and holdings[sym] == 0:
                    weight = float(spec.params.get("position_weight", 0.35))
                    px = round(bar["open"] * 1.0005, 2)
                    lot = 100 if market == "CN" else 1
                    target_amt = min(cash * 0.95, initial_capital * weight)
                    qty = int(target_amt // (px * lot)) * lot
                    if qty > 0:
                        notional = qty * px
                        fee = max(5.0, round(notional * 0.0003, 2)) + round(notional * 0.00001, 2)
                        if cash >= notional + fee:
                            cash = round(cash - notional - fee, 2)
                            holdings[sym] = qty
                            avg_cost[sym] = (notional + fee) / qty
                            last_buy_date[sym] = d_str
                            trades.append({"date": d_str, "symbol": sym, "side": "BUY", "quantity": qty, "price": px, "fee": fee, "pnl": 0.0})

        mv = sum(holdings[s] * day_bars[s]["close"] for s in day_bars if s in holdings)
        eq = round(cash + mv, 2)
        peak = max(peak, eq)
        dd = round((peak - eq) / peak if peak > 0 else 0.0, 8)
        cur_avg = sum(day_bars[s]["close"] for s in day_bars) / len(day_bars)
        bench_eq = round(initial_capital * (cur_avg / first_day_avg), 2)
        equity_curve.append({
            "date": d_str,
            "equity": eq,
            "benchmark_equity": bench_eq,
            "drawdown": dd,
            "cash": round(cash, 2),
            "market_value": round(mv, 2),
        })
        for s, bar in day_bars.items():
            prev_closes[s] = bar["close"]

    metrics = compute_risk_metrics(equity_curve, trades)
    repro_payload = {
        "strategy_id": spec.strategy_id,
        "params": spec.params,
        "rules": spec.rules,
        "symbols": sorted(s.upper() for s in symbols),
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": initial_capital,
        "seed": seed,
        "market": market,
        "bars": rows,
    }
    repro_hash = hashlib.sha256(json.dumps(repro_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return {
        "strategy_id": spec.strategy_id,
        "strategy_name": spec.name,
        "style": spec.style,
        "symbols": [s.upper() for s in symbols],
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": initial_capital,
        "equity_curve": equity_curve,
        "trades": trades,
        "metrics": metrics,
        "assumptions": {
            "market": market,
            "slippage_rate": 0.0005,
            "commission_rate": 0.0003,
            "min_commission": 5.0,
            "t_plus_one": market == "CN",
        },
        "reproducibility_hash": repro_hash,
    }


def compare_strategies(
    strategy_ids: list[str],
    symbols: list[str],
    start_date: str,
    end_date: str,
    initial_capital: float = 100000.0,
    market: str = "CN",
    db_path: str | Path | None = None,
    strategy_dir: Path | None = None,
) -> dict[str, Any]:
    results = []
    for sid in strategy_ids:
        results.append(
            run_backtest(
                strategy_id=sid,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                initial_capital=initial_capital,
                market=market,
                db_path=db_path,
                strategy_dir=strategy_dir,
            )
        )
    ranked = sorted(results, key=lambda r: r["metrics"]["sharpe_ratio"], reverse=True)
    return {
        "strategy_ids": strategy_ids,
        "results": {r["strategy_id"]: r for r in results},
        "comparison_table": [
            {
                "strategy_id": r["strategy_id"],
                "style": r["style"],
                "total_return": r["metrics"]["total_return"],
                "sharpe_ratio": r["metrics"]["sharpe_ratio"],
                "max_drawdown": r["metrics"]["max_drawdown"],
                "var_95": r["metrics"]["var_95"],
            }
            for r in results
        ],
        "best_by_sharpe": ranked[0]["strategy_id"] if ranked else None,
    }


# ---------------------------------------------------------------------------
# 7. Persistent Paper-Trading Account & Ledger
# ---------------------------------------------------------------------------

class ContractPaperAccountService:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def _init_schema(self) -> None:
        with self._connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS account_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash REAL NOT NULL,
                    initial_capital REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    current_date TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS position_lots (
                    lot_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    qty INTEGER NOT NULL,
                    cost_basis REAL NOT NULL,
                    buy_date TEXT NOT NULL,
                    last_price REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    limit_price REAL,
                    fill_price REAL,
                    commission REAL NOT NULL,
                    transfer_fee REAL NOT NULL,
                    stamp_duty REAL NOT NULL,
                    accepted INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    strategy_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equity_history (
                    trade_date TEXT PRIMARY KEY,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    total_equity REAL NOT NULL
                );
            """)
            row = con.execute("SELECT id FROM account_state WHERE id = 1").fetchone()
            if row is None:
                today_iso = date.today().isoformat()
                con.execute(
                    "INSERT INTO account_state (id, cash, initial_capital, realized_pnl, current_date) VALUES (1, 100000.0, 100000.0, 0.0, ?)",
                    [today_iso],
                )
                con.execute(
                    "INSERT OR REPLACE INTO equity_history (trade_date, cash, market_value, total_equity) VALUES (?, 100000.0, 0.0, 100000.0)",
                    [today_iso],
                )

    def reset_account(
        self,
        initial_capital: float = 100000.0,
        initial_date: str | None = None,
        start_date: str | None = None,
    ) -> dict[str, Any]:
        if not math.isfinite(initial_capital) or initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        eff_date = initial_date or start_date or date.today().isoformat()
        with self._connect() as con:
            con.execute("DELETE FROM position_lots")
            con.execute("DELETE FROM orders")
            con.execute("DELETE FROM equity_history")
            con.execute(
                "UPDATE account_state SET cash = ?, initial_capital = ?, realized_pnl = 0.0, current_date = ? WHERE id = 1",
                [float(initial_capital), float(initial_capital), eff_date],
            )
            con.execute(
                "INSERT INTO equity_history (trade_date, cash, market_value, total_equity) VALUES (?, ?, 0.0, ?)",
                [eff_date, float(initial_capital), float(initial_capital)],
            )
        return self.get_account_summary()

    def get_account_summary(self, price_overrides: dict[str, float] | None = None) -> dict[str, Any]:
        overrides = price_overrides or {}
        with self._connect() as con:
            st = con.execute('SELECT cash, initial_capital, realized_pnl, "current_date" AS cur_date FROM account_state WHERE id = 1').fetchone()
            lots = con.execute("SELECT symbol, market, qty, cost_basis, buy_date, last_price FROM position_lots WHERE qty > 0").fetchall()

        cur_date = st["cur_date"]
        by_sym: dict[tuple[str, str], dict[str, Any]] = {}
        for lot in lots:
            key = (lot["symbol"], lot["market"])
            px = float(overrides.get(lot["symbol"], lot["last_price"]))
            sellable = lot["qty"] if (lot["market"] != "CN" or lot["buy_date"] < cur_date) else 0
            entry = by_sym.setdefault(key, {
                "symbol": lot["symbol"],
                "market": lot["market"],
                "quantity": 0,
                "sellable_qty": 0,
                "cost_total": 0.0,
                "market_price": px,
            })
            entry["quantity"] += lot["qty"]
            entry["sellable_qty"] += sellable
            entry["cost_total"] += lot["qty"] * lot["cost_basis"]
            entry["market_price"] = px

        positions = []
        mv = 0.0
        unrealized = 0.0
        for item in by_sym.values():
            qty = item["quantity"]
            avg_cost = item["cost_total"] / qty
            mkt_val = round(qty * item["market_price"], 2)
            upnl = round(mkt_val - item["cost_total"], 2)
            mv += mkt_val
            unrealized += upnl
            positions.append({
                "symbol": item["symbol"],
                "market": item["market"],
                "quantity": qty,
                "sellable_qty": item["sellable_qty"],
                "avg_cost": round(avg_cost, 4),
                "market_price": item["market_price"],
                "market_value": mkt_val,
                "unrealized_pnl": upnl,
            })

        cash = round(float(st["cash"]), 2)
        mv = round(mv, 2)
        return {
            "cash": cash,
            "initial_capital": round(float(st["initial_capital"]), 2),
            "market_value": mv,
            "total_equity": round(cash + mv, 2),
            "realized_pnl": round(float(st["realized_pnl"]), 2),
            "unrealized_pnl": round(unrealized, 2),
            "current_date": cur_date,
            "positions": positions,
            "market_rules": {
                "CN": {"t_plus_one": True, "buy_lot": 100, "sell_lot": 1, "price_limit_rate": 0.10, "commission_rate": 0.0003, "min_commission": 5.0, "transfer_fee_rate": 0.00001, "stamp_duty_rate": 0.0005, "slippage_rate": 0.0005},
                "US": {"t_plus_one": False, "buy_lot": 1, "sell_lot": 1, "price_limit_rate": None, "commission_rate": 0.0001, "min_commission": 1.0, "slippage_rate": 0.0005},
            },
        }

    def get_orders(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._connect() as con:
            sql = "SELECT * FROM orders ORDER BY rowid DESC"
            if limit is not None:
                sql += f" LIMIT {int(limit)}"
            return [dict(r) for r in con.execute(sql).fetchall()]

    def get_fills(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._connect() as con:
            sql = "SELECT * FROM orders WHERE accepted = 1 ORDER BY rowid DESC"
            if limit is not None:
                sql += f" LIMIT {int(limit)}"
            return [dict(r) for r in con.execute(sql).fetchall()]

    def get_equity_history(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM equity_history ORDER BY trade_date ASC").fetchall()]
        for r in rows:
            r.setdefault("date", r["trade_date"])
            r.setdefault("equity", r["total_equity"])
        return rows

    def _record_rejection(self, con, trade_date: str, symbol: str, market: str, side: str, order_type: str, quantity: int, limit_price: float | None, reason: str, strategy_id: str | None) -> dict[str, Any]:
        oid = f"ord_{uuid4().hex[:10]}"
        now = datetime.now(timezone.utc).isoformat()
        con.execute(
            """
            INSERT INTO orders
            (order_id, trade_date, symbol, market, side, order_type, quantity, limit_price, fill_price, commission, transfer_fee, stamp_duty, accepted, reason, strategy_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0.0, 0.0, 0.0, 0, ?, ?, ?)
            """,
            [oid, trade_date, symbol, market, side, order_type, quantity, limit_price, reason, strategy_id, now],
        )
        return {"accepted": False, "order_id": oid, "reason": reason}

    def submit_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "market",
        limit_price: float | None = None,
        market: str = "CN",
        trade_date: str | None = None,
        reference_bar: dict[str, float] | None = None,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        sym = (symbol or "").strip().upper()
        side_u = (side or "").strip().upper()
        mkt = (market or "CN").strip().upper()
        otype = (order_type or "market").strip().lower()
        bar = reference_bar or {"open": 130.0, "high": 132.0, "low": 128.0, "close": 130.5, "prev_close": 129.5}

        with self._connect() as con:
            st = con.execute('SELECT cash, realized_pnl, "current_date" AS cur_date FROM account_state WHERE id = 1').fetchone()
            tdate = trade_date or st["cur_date"]

            if side_u not in ("BUY", "SELL"):
                return self._record_rejection(con, tdate, sym, mkt, side_u, otype, int(quantity or 0), limit_price, f"Invalid side {side!r}; expected BUY or SELL", strategy_id)
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
                return self._record_rejection(con, tdate, sym, mkt, side_u, otype, int(quantity or 0), limit_price, "Order quantity must be a positive integer", strategy_id)
            if mkt not in ("CN", "US"):
                return self._record_rejection(con, tdate, sym, mkt, side_u, otype, quantity, limit_price, f"Invalid market {market!r}", strategy_id)
            if otype not in ("market", "limit"):
                return self._record_rejection(con, tdate, sym, mkt, side_u, otype, quantity, limit_price, f"Invalid order_type {order_type!r}", strategy_id)
            if otype == "limit" and (limit_price is None or not math.isfinite(limit_price) or limit_price <= 0):
                return self._record_rejection(con, tdate, sym, mkt, side_u, otype, quantity, limit_price, "limit_price is required and must be positive for limit orders", strategy_id)
            if mkt == "CN" and side_u == "BUY" and quantity % 100 != 0:
                return self._record_rejection(con, tdate, sym, mkt, side_u, otype, quantity, limit_price, "CN A-share BUY quantity must be a multiple of 100-share lot", strategy_id)

            slip = 0.0005
            base_px = float(bar.get("open", bar.get("close", 100.0)))
            if otype == "limit":
                px = float(limit_price)
                if mkt == "CN" and "prev_close" in bar and bar["prev_close"]:
                    pc = float(bar["prev_close"])
                    if px > round(pc * 1.10 + 1e-9, 2) or px < round(pc * 0.90 - 1e-9, 2):
                        return self._record_rejection(con, tdate, sym, mkt, side_u, otype, quantity, limit_price, "CN 10% price limit breach", strategy_id)
                if "low" in bar and "high" in bar:
                    if side_u == "BUY" and px < float(bar["low"]):
                        return self._record_rejection(con, tdate, sym, mkt, side_u, otype, quantity, limit_price, "Limit BUY price below bar low; unfillable", strategy_id)
                    if side_u == "SELL" and px > float(bar["high"]):
                        return self._record_rejection(con, tdate, sym, mkt, side_u, otype, quantity, limit_price, "Limit SELL price above bar high; unfillable", strategy_id)
                fill_price = round(px, 2)
            else:
                raw_fill = base_px * (1.0 + slip if side_u == "BUY" else 1.0 - slip)
                fill_price = round(raw_fill, 2)
                if mkt == "CN" and "prev_close" in bar and bar["prev_close"]:
                    pc = float(bar["prev_close"])
                    if fill_price > round(pc * 1.10 + 1e-9, 2) or fill_price < round(pc * 0.90 - 1e-9, 2):
                        return self._record_rejection(con, tdate, sym, mkt, side_u, otype, quantity, limit_price, "CN 10% price limit breach", strategy_id)

            notional = quantity * fill_price
            if mkt == "CN":
                commission = max(5.0, round(notional * 0.0003, 2))
                transfer_fee = round(notional * 0.00001, 2)
                stamp_duty = round(notional * 0.0005, 2) if side_u == "SELL" else 0.0
            else:
                commission = max(1.0, round(notional * 0.0001, 2))
                transfer_fee = 0.0
                stamp_duty = 0.0
            total_fees = round(commission + transfer_fee + stamp_duty, 2)

            cash = float(st["cash"])
            realized_pnl = float(st["realized_pnl"])

            if side_u == "BUY":
                total_cost = round(notional + total_fees, 2)
                if cash + 1e-9 < total_cost:
                    return self._record_rejection(con, tdate, sym, mkt, side_u, otype, quantity, limit_price, f"Insufficient cash/funds: need {total_cost:.2f}, have {cash:.2f}", strategy_id)
                new_cash = round(cash - total_cost, 2)
                con.execute("UPDATE account_state SET cash = ? WHERE id = 1", [new_cash])
                lot_id = f"lot_{uuid4().hex[:10]}"
                con.execute(
                    "INSERT INTO position_lots (lot_id, symbol, market, qty, cost_basis, buy_date, last_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [lot_id, sym, mkt, quantity, total_cost / quantity, tdate, float(bar.get("close", fill_price))],
                )
            else:
                all_lots = con.execute(
                    "SELECT lot_id, qty, cost_basis, buy_date FROM position_lots WHERE symbol = ? AND market = ? AND qty > 0 ORDER BY buy_date ASC",
                    [sym, mkt],
                ).fetchall()
                total_held = sum(r["qty"] for r in all_lots)
                if total_held < quantity:
                    return self._record_rejection(con, tdate, sym, mkt, side_u, otype, quantity, limit_price, f"Insufficient position/holdings: held {total_held}, requested {quantity}", strategy_id)
                sellable_lots = [r for r in all_lots if mkt != "CN" or r["buy_date"] < tdate]
                sellable_qty = sum(r["qty"] for r in sellable_lots)
                if sellable_qty < quantity:
                    return self._record_rejection(con, tdate, sym, mkt, side_u, otype, quantity, limit_price, f"CN T+1 rule: sellable_qty={sellable_qty} < {quantity} bought on {tdate}", strategy_id)

                rem = quantity
                cost_sold = 0.0
                for lot in sellable_lots:
                    if rem <= 0:
                        break
                    take = min(rem, lot["qty"])
                    cost_sold += take * lot["cost_basis"]
                    if take == lot["qty"]:
                        con.execute("DELETE FROM position_lots WHERE lot_id = ?", [lot["lot_id"]])
                    else:
                        con.execute("UPDATE position_lots SET qty = qty - ? WHERE lot_id = ?", [take, lot["lot_id"]])
                    rem -= take

                net_proceeds = round(notional - total_fees, 2)
                trade_pnl = round(net_proceeds - cost_sold, 2)
                new_cash = round(cash + net_proceeds, 2)
                new_realized = round(realized_pnl + trade_pnl, 2)
                con.execute("UPDATE account_state SET cash = ?, realized_pnl = ? WHERE id = 1", [new_cash, new_realized])

            oid = f"ord_{uuid4().hex[:10]}"
            now = datetime.now(timezone.utc).isoformat()
            con.execute(
                """
                INSERT INTO orders
                (order_id, trade_date, symbol, market, side, order_type, quantity, limit_price, fill_price, commission, transfer_fee, stamp_duty, accepted, reason, strategy_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'filled', ?, ?)
                """,
                [oid, tdate, sym, mkt, side_u, otype, quantity, limit_price, fill_price, commission, transfer_fee, stamp_duty, strategy_id, now],
            )
            return {
                "accepted": True,
                "order_id": oid,
                "symbol": sym,
                "market": mkt,
                "side": side_u,
                "quantity": quantity,
                "fill_price": fill_price,
                "commission": commission,
                "transfer_fee": transfer_fee,
                "stamp_duty": stamp_duty,
                "cash_after": new_cash,
                "strategy_id": strategy_id,
            }

    def advance_day(self, new_date: str, mark_prices: dict[str, float] | None = None) -> dict[str, Any]:
        marks = mark_prices or {}
        with self._connect() as con:
            con.execute("UPDATE account_state SET current_date = ? WHERE id = 1", [new_date])
            for sym, px in marks.items():
                con.execute("UPDATE position_lots SET last_price = ? WHERE symbol = ?", [float(px), sym.upper()])
        summary = self.get_account_summary(price_overrides=marks)
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO equity_history (trade_date, cash, market_value, total_equity) VALUES (?, ?, ?, ?)",
                [new_date, summary["cash"], summary["market_value"], summary["total_equity"]],
            )
        return self.get_account_summary(price_overrides=marks)


# ---------------------------------------------------------------------------
# 8. Verifier, Simulated Pi CLI & Config Contract
# ---------------------------------------------------------------------------

def verify_answer_grounding(answer: str, tool_calls: list[dict[str, Any]], sources: list[str]) -> dict[str, Any]:
    trace_blob = json.dumps(tool_calls, ensure_ascii=False)
    # Extract multi-digit/decimal numbers from answer
    nums_in_answer = re.findall(r"\b\d+(?:\.\d+)?\b", answer or "")
    ungrounded = []
    for num in nums_in_answer:
        if len(num) >= 2 and num not in trace_blob:
            # Allow common small integer markers like 3天/100
            if float(num) > 10 and "." in num:
                ungrounded.append(num)
    has_sources = len(sources) > 0
    grounded = has_sources and len(ungrounded) == 0 and len(tool_calls) > 0
    return {
        "grounded": grounded,
        "passed": grounded,
        "verified_sources": list(sources),
        "ungrounded_numbers": ungrounded,
    }


def contract_run_research_query(prompt: str, db_path: Path, ledger_db_path: Path, strategy_dir: Path) -> dict[str, Any]:
    if not prompt or not prompt.strip():
        raise ValueError("Prompt must be non-empty")
    t0 = time.perf_counter()
    tool_calls = []

    if "电力" in prompt:
        sec_res = ingest_a_share_sector("电力", days=3, db_path=db_path)
        tool_calls.append({
            "name": "fetch_market_data",
            "args": {"sector": "电力", "days": 3},
            "result_summary": f"rows_written={sec_res['rows_written']}, source={sec_res['source']}",
            "duration_ms": 18,
        })
        sem_res = hybrid_semantic_query({"filters": {"sector": "电力"}, "condition": "Positive industry momentum"}, db_path=db_path)
        tool_calls.append({
            "name": "jevdb_semantic_query",
            "args": {"filters": {"sector": "电力"}},
            "result_summary": f"matched={len(sem_res['rows'])}, change_pct=1.85%, leader=600900(29.45)",
            "duration_ms": 14,
        })
        sr_res = web_search("最近3天 A股 电力行业 表现")
        tool_calls.append({
            "name": "web_search",
            "args": {"query": "最近3天 A股 电力行业 表现"},
            "result_summary": f"hits={len(sr_res)}, url={sr_res[0]['url']}",
            "duration_ms": 11,
        })
        sources = ["akshare_ths", "jevdb_hybrid", sr_res[0]["url"]]
        answer = (
            "最近3天A股电力行业整体走强，近3日板块涨幅1.85%，成交额312亿元。"
            "龙头股长江电力(600900)最新价29.45元(+2.18%)，华能国际(600011)7.82元(+1.56%)，中国核电(601985)9.64元(+1.92%)。"
            "经JEVDB语义检索与公开研报交叉核验，水电来水改善与绿电消纳支撑行业盈利预期。"
        )
    elif "AAPL" in prompt.upper() or "EDGAR" in prompt.upper() or "FINNHUB" in prompt.upper():
        ingest_us_stock("AAPL", db_path=db_path)
        ingest_sec_edgar("AAPL", db_path=db_path)
        ingest_finnhub_news("AAPL", db_path=db_path)
        sem_res = hybrid_semantic_query({"filters": {"symbol": "AAPL"}, "condition": "Earnings update"}, db_path=db_path)
        tool_calls.extend([
            {"name": "query_stored_market_data", "args": {"symbol": "AAPL"}, "result_summary": "yfinance bars + SEC EDGAR CIK 0000320193 10-Q revenue=95.36B", "duration_ms": 15},
            {"name": "jevdb_semantic_query", "args": {"filters": {"symbol": "AAPL"}}, "result_summary": f"rows={len(sem_res['rows'])}, sources={sem_res['sources']}", "duration_ms": 12},
        ])
        sources = ["yfinance", "sec_edgar", "finnhub"]
        answer = (
            "交叉核验 yfinance 行情、SEC EDGAR (CIK 0000320193 Form 10-Q) 与 Finnhub 新闻/财报日历："
            "AAPL 季度营收 95.36B 美元，净利润 24.78B 美元，PE_TTM 31.2，EPS 1.65 美元超出预期 1.62 美元；需关注高估值与供应链风险。"
        )
    else:
        con = init_db(db_path)
        load_static_baseline(con)
        con.close()
        ingest_a_share_stock("000858", days=10, db_path=db_path)
        sem_res = hybrid_semantic_query({"filters": {"symbol": "000858"}, "condition": "Positive fundamentals"}, db_path=db_path)
        ensure_builtin_strategies(strategy_dir)
        bt = run_backtest("value_quality_margin_of_safety", ["000858", "601318"], "2025-04-24", "2025-05-15", db_path=db_path, strategy_dir=strategy_dir)
        sr_res = web_search("000858 五粮液 估值 ROE 安全边际")
        tool_calls.extend([
            {"name": "query_stored_market_data", "args": {"symbol": "000858"}, "result_summary": "000858 PE=15.8, PB=3.6, ROE=22.4%, revenue=369.40亿元", "duration_ms": 12},
            {"name": "jevdb_semantic_query", "args": {"symbol": "000858"}, "result_summary": f"rows={len(sem_res['rows'])}", "duration_ms": 10},
            {"name": "run_backtest_tool", "args": {"strategy_id": "value_quality_margin_of_safety"}, "result_summary": f"total_return={bt['metrics']['total_return']}", "duration_ms": 16},
            {"name": "web_search", "args": {"query": "000858 五粮液 估值"}, "result_summary": sr_res[0]["url"], "duration_ms": 8},
        ])
        sources = ["static_baseline", "akshare_ths", sr_res[0]["url"]]
        answer = (
            "综合基本面与价值投资分析：五粮液(000858) PE_TTM 15.8、PB 3.6、ROE 22.4%，一季度营业收入 369.40亿元(+6.05%)，具备较高盈利质量与安全边际；"
            "对比中国平安(601318)估值稳健，价值策略回测已记录风险收益指标。"
        )

    verification = verify_answer_grounding(answer, tool_calls, sources)
    dur_ms = max(1, int((time.perf_counter() - t0) * 1000))
    return {
        "prompt": prompt,
        "tool_calls": tool_calls,
        "verification": verification,
        "answer": answer,
        "sources": sources,
        "duration_ms": dur_ms,
    }


def contract_pi_metadata() -> dict[str, Any]:
    return {
        "pi_version": "1.6.1",
        "mcp_json": {
            "mcpServers": {
                "jevdb-stock-research": {
                    "command": sys.executable,
                    "args": ["-m", "stock_research.mcp_server.server"],
                }
            }
        },
        "skills": [
            {"name": "stock-research", "description": "Multi-turn equity research workflow using JEVDB semantic SQL and web cross-verification."},
            {"name": "cross-verify", "description": "Cross-check numeric facts and claims across primary filings, market bars, and web sources."},
            {"name": "value-investing", "description": "Long-term fundamental valuation, ROE, cash flow, and margin-of-safety assessment."},
            {"name": "quant-strategy", "description": "Daily-frequency A-share and US quantitative strategy formulation, backtesting, and risk analytics."},
        ],
    }


# ---------------------------------------------------------------------------
# 9. Contract Web HTTP Server + 4-View SPA HTML
# ---------------------------------------------------------------------------

CONTRACT_SPA_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <link rel="icon" href="data:," />
  <title>JEVDB Stock Research & Paper-Trading Prototype</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 16px; background: #0f172a; color: #e2e8f0; }
    .nav { display: flex; gap: 8px; margin-bottom: 16px; }
    .nav button { padding: 8px 14px; background: #1e293b; color: #e2e8f0; border: 1px solid #334155; cursor: pointer; }
    .panel { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 14px; margin-bottom: 16px; }
    input, button { padding: 8px; border-radius: 4px; }
    .err { color: #f87171; }
    svg, canvas { display: block; background: #0b1120; border-radius: 6px; }
  </style>
</head>
<body>
  <h1>JEVDB Stock Research &amp; Paper-Trading Prototype</h1>
  <div class="nav">
    <button type="button" data-tab="agent">Agent 工作台</button>
    <button type="button" data-tab="market">行情与基本面</button>
    <button type="button" data-tab="quant">策略回测与风险对比</button>
    <button type="button" data-tab="paper">模拟持仓与盈亏</button>
  </div>

  <section class="panel" id="view-agent">
    <h2>1. Agent 投研工作台 (用户需求 → 工具调用 → 结果展示)</h2>
    <div style="display:flex;gap:8px;margin-bottom:8px;">
      <input data-testid="agent-input" id="agent-input" type="text" style="width:420px" placeholder="输入投研问题，例如：我想知道最近3天a股电力行业的情况" />
      <button data-testid="agent-submit" id="agent-submit" type="button">提交研究</button>
    </div>
    <div id="agent-validation" class="err"></div>
    <div data-testid="tool-calls-panel" id="tool-calls-panel" style="min-height:32px;margin:8px 0;">等待调用工具...</div>
    <div data-testid="agent-answer" id="agent-answer" style="min-height:32px;">等待回答...</div>
  </section>

  <section class="panel" id="view-market">
    <h2>2. 行情与基本面图表</h2>
    <div data-testid="market-chart" id="market-chart" style="width:640px;height:220px;">
      <svg width="640" height="220" viewBox="0 0 640 220">
        <polyline fill="none" stroke="#38bdf8" stroke-width="2.5" points="20,180 120,160 220,150 320,120 420,105 520,80 610,60" />
      </svg>
    </div>
    <div id="fundamentals-summary"></div>
  </section>

  <section class="panel" id="view-quant">
    <h2>3. 多策略回测曲线与风险对比</h2>
    <div data-testid="backtest-chart" id="backtest-chart" style="width:640px;height:220px;">
      <svg width="640" height="220" viewBox="0 0 640 220">
        <polyline fill="none" stroke="#34d399" stroke-width="2.5" points="20,170 150,155 300,130 450,110 610,75" />
        <polyline fill="none" stroke="#f472b6" stroke-width="2" points="20,170 150,165 300,145 450,138 610,112" />
      </svg>
    </div>
    <div id="compare-summary"></div>
  </section>

  <section class="panel" id="view-paper">
    <h2>4. 模拟持仓与盈亏视图</h2>
    <div data-testid="paper-holdings" id="paper-holdings" style="margin-bottom:10px;">加载持仓中...</div>
    <div data-testid="paper-pnl-chart" id="paper-pnl-chart" style="width:640px;height:200px;">
      <svg width="640" height="200" viewBox="0 0 640 200">
        <polyline fill="none" stroke="#facc15" stroke-width="2.5" points="20,140 200,135 400,120 610,95" />
      </svg>
    </div>
  </section>

  <script>
    async function refreshDashboard() {
      const mRes = await fetch('/api/market/bars?symbol=000858&market=CN');
      const mData = await mRes.json();
      document.getElementById('fundamentals-summary').textContent = 'Bars loaded: ' + (mData.bars ? mData.bars.length : 0);

      const pRes = await fetch('/api/paper/account');
      const pData = await pRes.json();
      const posTxt = (pData.positions && pData.positions.length)
        ? pData.positions.map(p => p.symbol + ' x' + p.quantity + ' (sellable:' + p.sellable_qty + ')').join(' | ')
        : '空仓 (Cash: ' + pData.cash + ')';
      document.getElementById('paper-holdings').textContent = 'Cash: ' + pData.cash + ' | Equity: ' + pData.total_equity + ' | ' + posTxt;

      const sRes = await fetch('/api/quant/strategies');
      const sData = await sRes.json();
      document.getElementById('compare-summary').textContent = 'Strategies: ' + (sData.strategies ? sData.strategies.length : 0);
    }

    document.getElementById('agent-submit').addEventListener('click', async () => {
      const input = document.getElementById('agent-input');
      const val = (input.value || '').trim();
      const errEl = document.getElementById('agent-validation');
      if (!val) {
        errEl.textContent = '请输入有效的投研问题后再提交。';
        return;
      }
      errEl.textContent = '';
      document.getElementById('tool-calls-panel').textContent = '正在执行工具链...';
      document.getElementById('agent-answer').textContent = '正在核验结果...';
      const resp = await fetch('/api/agent/query', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({prompt: val})
      });
      const data = await resp.json();
      document.getElementById('tool-calls-panel').textContent =
        (data.tool_calls || []).map(t => t.name + ': ' + t.result_summary).join(' -> ');
      document.getElementById('agent-answer').textContent = data.answer || '';
    });

    refreshDashboard().catch(() => {});
  </script>
</body>
</html>
"""


class ContractWebServer:
    def __init__(self, db_path: Path, ledger_db_path: Path, strategy_dir: Path):
        self.db_path = db_path
        self.ledger_db_path = ledger_db_path
        self.strategy_dir = strategy_dir
        self.ledger = ContractPaperAccountService(ledger_db_path)
        ensure_builtin_strategies(self.strategy_dir)
        con = init_db(self.db_path)
        load_static_baseline(con)
        con.close()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    def start(self) -> int:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, code: int, payload: Any):
                body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(n) if n > 0 else b"{}"
                return json.loads(raw.decode("utf-8"))

            def do_GET(self):
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                if parsed.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                elif parsed.path == "/":
                    html = CONTRACT_SPA_HTML.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                elif parsed.path == "/api/health":
                    self._send_json(200, {"status": "ok", "jevdb_version": "0.1.0", "duckdb_version": "1.4.3"})
                elif parsed.path == "/api/market/bars":
                    sym = (qs.get("symbol", ["000858"])[0]).upper()
                    mkt = (qs.get("market", ["CN"])[0]).upper()
                    con = init_db(app.db_path)
                    rows = con.execute(
                        "SELECT symbol, market, CAST(trade_date AS VARCHAR) AS trade_date, open, high, low, close, volume FROM market_bars WHERE symbol=? AND market=? ORDER BY trade_date ASC",
                        [sym, mkt],
                    ).fetchall()
                    con.close()
                    bars = [{"symbol": r[0], "market": r[1], "trade_date": r[2], "open": r[3], "high": r[4], "low": r[5], "close": r[6], "volume": r[7]} for r in rows]
                    self._send_json(200, {"symbol": sym, "market": mkt, "bars": bars})
                elif parsed.path == "/api/market/sector":
                    name = qs.get("name", ["电力"])[0]
                    con = init_db(app.db_path)
                    snaps = con.execute("SELECT sector_name, CAST(trade_date AS VARCHAR), change_pct, leader_symbol FROM sector_snapshots WHERE sector_name=? ORDER BY trade_date DESC", [name]).fetchall()
                    cons = con.execute("SELECT symbol, name, price, change_pct, pe, pb FROM sector_constituents WHERE sector_name=?", [name]).fetchall()
                    con.close()
                    self._send_json(200, {
                        "sector": name,
                        "snapshots": [{"sector_name": r[0], "trade_date": r[1], "change_pct": r[2], "leader_symbol": r[3]} for r in snaps],
                        "constituents": [{"symbol": r[0], "name": r[1], "price": r[2], "change_pct": r[3], "pe": r[4], "pb": r[5]} for r in cons],
                    })
                elif parsed.path == "/api/market/fundamentals":
                    sym = (qs.get("symbol", ["000858"])[0]).upper()
                    con = init_db(app.db_path)
                    rows = con.execute("SELECT symbol, market, CAST(report_date AS VARCHAR), pe_ttm, pb, roe, revenue, net_income, source FROM fundamentals WHERE symbol=?", [sym]).fetchall()
                    con.close()
                    self._send_json(200, {"symbol": sym, "fundamentals": [{"symbol": r[0], "market": r[1], "report_date": r[2], "pe_ttm": r[3], "pb": r[4], "roe": r[5], "revenue": r[6], "net_income": r[7], "source": r[8]} for r in rows]})
                elif parsed.path == "/api/market/news":
                    sym = qs.get("symbol", [None])[0]
                    sec = qs.get("sector", [None])[0]
                    con = init_db(app.db_path)
                    if sym:
                        rows = con.execute("SELECT event_id, symbol, sector, CAST(published_at AS VARCHAR), title, summary, source FROM news_events WHERE symbol=? ORDER BY published_at DESC", [sym.upper()]).fetchall()
                    elif sec:
                        rows = con.execute("SELECT event_id, symbol, sector, CAST(published_at AS VARCHAR), title, summary, source FROM news_events WHERE sector=? ORDER BY published_at DESC", [sec]).fetchall()
                    else:
                        rows = con.execute("SELECT event_id, symbol, sector, CAST(published_at AS VARCHAR), title, summary, source FROM news_events ORDER BY published_at DESC").fetchall()
                    con.close()
                    self._send_json(200, {"news": [{"event_id": r[0], "symbol": r[1], "sector": r[2], "published_at": r[3], "title": r[4], "summary": r[5], "source": r[6]} for r in rows]})
                elif parsed.path == "/api/quant/strategies":
                    self._send_json(200, {"strategies": [asdict(s) for s in list_strategies(app.strategy_dir)]})
                elif parsed.path == "/api/paper/account":
                    self._send_json(200, app.ledger.get_account_summary())
                elif parsed.path == "/api/agent/stream":
                    prompt = (qs.get("prompt", [""])[0]).strip()
                    if not prompt:
                        self._send_json(400, {"detail": "prompt must be non-empty"})
                        return
                    res = contract_run_research_query(prompt, app.db_path, app.ledger_db_path, app.strategy_dir)
                    events = [
                        ("status", {"stage": "started", "prompt": prompt}),
                    ]
                    for tc in res["tool_calls"]:
                        events.append(("tool_start", {"name": tc["name"], "args": tc["args"]}))
                        events.append(("tool_end", tc))
                    events.append(("verification", res["verification"]))
                    events.append(("final_answer", {"answer": res["answer"], "sources": res["sources"]}))
                    sse_chunks = []
                    for ev_name, payload in events:
                        sse_chunks.append(f"event: {ev_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n")
                    body = "".join(sse_chunks).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._send_json(404, {"detail": "Not found"})

            def do_POST(self):
                parsed = urlparse(self.path)
                try:
                    body = self._read_json()
                except Exception:
                    self._send_json(422, {"detail": "Invalid JSON body"})
                    return

                if parsed.path == "/api/agent/query":
                    prompt = str(body.get("prompt") or "").strip()
                    if not prompt:
                        self._send_json(422, {"detail": "prompt is required and must be non-empty"})
                        return
                    res = contract_run_research_query(prompt, app.db_path, app.ledger_db_path, app.strategy_dir)
                    self._send_json(200, res)
                elif parsed.path == "/api/market/ingest":
                    kind = body.get("kind", "a_share_stock")
                    if kind == "a_share_sector":
                        self._send_json(200, ingest_a_share_sector(body.get("sector", "电力"), int(body.get("days", 30)), app.db_path))
                    elif kind == "us_stock":
                        self._send_json(200, ingest_us_stock(body.get("symbol", "AAPL"), db_path=app.db_path))
                    else:
                        self._send_json(200, ingest_a_share_stock(body.get("symbol", "000858"), int(body.get("days", 30)), app.db_path))
                elif parsed.path == "/api/quant/strategies":
                    try:
                        spec = StrategySpec(**body)
                        save_strategy(spec, app.strategy_dir)
                        self._send_json(200, asdict(spec))
                    except Exception as exc:
                        self._send_json(422, {"detail": str(exc)})
                elif parsed.path == "/api/quant/backtest":
                    try:
                        res = run_backtest(
                            strategy_id=body["strategy_id"],
                            symbols=body.get("symbols", ["000858", "601318"]),
                            start_date=body.get("start_date", "2025-04-24"),
                            end_date=body.get("end_date", "2025-05-15"),
                            initial_capital=float(body.get("initial_capital", 100000.0)),
                            seed=int(body.get("seed", 42)),
                            market=body.get("market", "CN"),
                            db_path=app.db_path,
                            strategy_dir=app.strategy_dir,
                        )
                        self._send_json(200, res)
                    except KeyError as exc:
                        self._send_json(404, {"detail": str(exc)})
                    except Exception as exc:
                        self._send_json(422, {"detail": str(exc)})
                elif parsed.path == "/api/quant/compare":
                    try:
                        res = compare_strategies(
                            strategy_ids=body.get("strategy_ids", ["value_quality_margin_of_safety", "cn_daily_momentum_sentiment"]),
                            symbols=body.get("symbols", ["000858", "601318"]),
                            start_date=body.get("start_date", "2025-04-24"),
                            end_date=body.get("end_date", "2025-05-15"),
                            initial_capital=float(body.get("initial_capital", 100000.0)),
                            market=body.get("market", "CN"),
                            db_path=app.db_path,
                            strategy_dir=app.strategy_dir,
                        )
                        self._send_json(200, res)
                    except Exception as exc:
                        self._send_json(422, {"detail": str(exc)})
                elif parsed.path == "/api/quant/portfolio-risk":
                    try:
                        res = compute_portfolio_risk(body["holdings"], body["price_history"])
                        self._send_json(200, res)
                    except Exception as exc:
                        self._send_json(422, {"detail": str(exc)})
                elif parsed.path == "/api/paper/order":
                    if "symbol" not in body or "side" not in body or "quantity" not in body:
                        self._send_json(422, {"detail": "symbol, side, and quantity are required"})
                        return
                    res = app.ledger.submit_order(
                        symbol=body["symbol"],
                        side=body["side"],
                        quantity=body["quantity"],
                        order_type=body.get("order_type", "market"),
                        limit_price=body.get("limit_price"),
                        market=body.get("market", "CN"),
                        trade_date=body.get("trade_date"),
                        reference_bar=body.get("reference_bar"),
                        strategy_id=body.get("strategy_id"),
                    )
                    self._send_json(200 if res["accepted"] else 400, res)
                elif parsed.path == "/api/paper/advance-day":
                    res = app.ledger.advance_day(body["new_date"], body.get("mark_prices"))
                    self._send_json(200, res)
                elif parsed.path == "/api/paper/reset":
                    res = app.ledger.reset_account(float(body.get("initial_capital", 100000.0)), body.get("initial_date"), body.get("start_date"))
                    self._send_json(200, res)
                else:
                    self._send_json(404, {"detail": "Not found"})

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self.port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


# ---------------------------------------------------------------------------
# 10. Contract MCP Stdio JSON-RPC 2.0 Server
# ---------------------------------------------------------------------------

def run_contract_mcp_stdio(db_path: Path, ledger_db_path: Path, strategy_dir: Path) -> None:
    con = init_db(db_path)
    load_static_baseline(con)
    con.close()
    ensure_builtin_strategies(strategy_dir)
    ledger = ContractPaperAccountService(ledger_db_path)

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        req = json.loads(line)
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if method == "notifications/initialized":
            continue
        elif method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "jevdb-stock-research-contract", "version": "0.1.0"},
                    "capabilities": {"tools": {}},
                },
            }
        elif method == "tools/list":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {"name": name, "description": f"Contract tool {name}", "inputSchema": {"type": "object", "properties": {}}}
                        for name in MCP_CONTRACT_TOOLS
                    ]
                },
            }
        elif method == "tools/call":
            tname = params.get("name")
            targs = params.get("arguments") or {}
            try:
                if tname not in MCP_CONTRACT_TOOLS:
                    raise KeyError(f"Unknown MCP tool: {tname}")
                if tname == "jevdb_semantic_query":
                    payload = hybrid_semantic_query(targs, db_path=db_path)
                elif tname == "fetch_market_data":
                    market = targs.get("market", "CN")
                    if market == "US":
                        payload = ingest_us_stock(targs.get("symbol", "AAPL"), db_path=db_path)
                    elif targs.get("sector"):
                        payload = ingest_a_share_sector(targs["sector"], int(targs.get("days", 30)), db_path=db_path)
                    else:
                        payload = ingest_a_share_stock(targs.get("symbol", "000858"), int(targs.get("days", 30)), db_path=db_path)
                elif tname == "query_stored_market_data":
                    sym = (targs.get("symbol") or "000858").upper()
                    c = init_db(db_path)
                    bars = c.execute("SELECT symbol, CAST(trade_date AS VARCHAR), close FROM market_bars WHERE symbol=? ORDER BY trade_date", [sym]).fetchall()
                    c.close()
                    payload = {"symbol": sym, "bars": [{"symbol": b[0], "trade_date": b[1], "close": b[2]} for b in bars]}
                elif tname == "web_search":
                    payload = {"results": web_search(targs.get("query", ""))}
                elif tname == "web_fetch_url":
                    payload = {"markdown": web_fetch_url(targs.get("url", ""))}
                elif tname == "list_quant_strategies":
                    payload = {"strategies": [asdict(s) for s in list_strategies(strategy_dir)]}
                elif tname == "save_quant_strategy":
                    spec = StrategySpec(**targs)
                    save_strategy(spec, strategy_dir)
                    payload = asdict(spec)
                elif tname == "run_backtest_tool":
                    payload = run_backtest(
                        strategy_id=targs["strategy_id"],
                        symbols=targs.get("symbols", ["000858", "601318"]),
                        start_date=targs.get("start_date", "2025-04-24"),
                        end_date=targs.get("end_date", "2025-05-15"),
                        initial_capital=float(targs.get("initial_capital", 100000.0)),
                        market=targs.get("market", "CN"),
                        db_path=db_path,
                        strategy_dir=strategy_dir,
                    )
                elif tname == "compare_strategies_tool":
                    payload = compare_strategies(
                        strategy_ids=targs["strategy_ids"],
                        symbols=targs.get("symbols", ["000858", "601318"]),
                        start_date=targs.get("start_date", "2025-04-24"),
                        end_date=targs.get("end_date", "2025-05-15"),
                        initial_capital=float(targs.get("initial_capital", 100000.0)),
                        market=targs.get("market", "CN"),
                        db_path=db_path,
                        strategy_dir=strategy_dir,
                    )
                elif tname == "analyze_portfolio_risk":
                    payload = compute_portfolio_risk(targs["holdings"], targs["price_history"])
                elif tname == "paper_account_status":
                    payload = ledger.get_account_summary()
                elif tname == "submit_paper_order":
                    payload = ledger.submit_order(**targs)
                    if not payload["accepted"]:
                        raise ValueError(payload["reason"])
                else:
                    raise KeyError(f"Unhandled tool: {tname}")

                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "isError": False,
                        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
                    },
                }
            except Exception as exc:
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": json.dumps({"error": str(exc)}, ensure_ascii=False)}],
                    },
                }
        else:
            resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def run_contract_cli(argv: list[str], db_path: Path, ledger_db_path: Path, strategy_dir: Path) -> int:
    parser = argparse.ArgumentParser(prog="stock_research.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ask = sub.add_parser("ask")
    p_ask.add_argument("prompt")
    p_ask.add_argument("--json", action="store_true")

    p_ing = sub.add_parser("ingest")
    p_ing.add_argument("--symbol", default="000858")
    p_ing.add_argument("--sector", default=None)
    p_ing.add_argument("--days", type=int, default=5)

    p_bt = sub.add_parser("backtest")
    p_bt.add_argument("--strategy-id", default="cn_daily_momentum_sentiment")
    p_bt.add_argument("--symbols", default="000858,601318")
    p_bt.add_argument("--start-date", default="2025-04-24")
    p_bt.add_argument("--end-date", default="2025-05-15")

    p_pap = sub.add_parser("paper")
    p_pap.add_argument("action", nargs="?", default="status")

    p_srv = sub.add_parser("serve")
    p_srv.add_argument("--port", type=int, default=8000)

    ns = parser.parse_args(argv)
    if ns.cmd == "ask":
        res = contract_run_research_query(ns.prompt, db_path, ledger_db_path, strategy_dir)
        print(json.dumps(res, ensure_ascii=False) if ns.json else res["answer"])
    elif ns.cmd == "ingest":
        out = ingest_a_share_sector(ns.sector, ns.days, db_path) if ns.sector else ingest_a_share_stock(ns.symbol, ns.days, db_path)
        print(json.dumps(out, ensure_ascii=False))
    elif ns.cmd == "backtest":
        ensure_builtin_strategies(strategy_dir)
        con = init_db(db_path)
        load_static_baseline(con)
        con.close()
        out = run_backtest(ns.strategy_id, ns.symbols.split(","), ns.start_date, ns.end_date, db_path=db_path, strategy_dir=strategy_dir)
        print(json.dumps(out, ensure_ascii=False, default=str))
    elif ns.cmd == "paper":
        svc = ContractPaperAccountService(ledger_db_path)
        print(json.dumps(svc.get_account_summary(), ensure_ascii=False, default=str))
    elif ns.cmd == "serve":
        print(f"Contract serve port={ns.port}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mcp-stdio", action="store_true")
    parser.add_argument("--cli", action="store_true")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--ledger-db-path", required=True)
    parser.add_argument("--strategy-dir", required=True)
    args, rest = parser.parse_known_args()
    if args.mcp_stdio:
        run_contract_mcp_stdio(Path(args.db_path), Path(args.ledger_db_path), Path(args.strategy_dir))
    elif args.cli:
        raise SystemExit(run_contract_cli(rest, Path(args.db_path), Path(args.ledger_db_path), Path(args.strategy_dir)))
