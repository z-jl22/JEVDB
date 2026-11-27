"""M4 tests: FastAPI REST + SSE backend, concurrent DuckDB safety, and Playwright UI E2E."""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from stock_research.api.app import create_app
from stock_research.storage.schema import db_session

api_module = importlib.import_module("stock_research.api.app")

SCRATCH_TMP_ROOT = Path(
    "/home/tao/.gemini/antigravity/scratch/teamwork_tmp/049e943f-af5a-4f84-870b-79191be7fdea"
)
CHROMIUM_BIN = "/usr/bin/chromium"


@pytest.fixture()
def isolated_api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    rt = tmp_path / "runtime"
    rt.mkdir(parents=True, exist_ok=True)
    db_path = rt / "market_research.duckdb"
    ledger_path = rt / "paper_ledger.duckdb"
    strat_dir = rt / "strategies"
    strat_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(rt))
    monkeypatch.setenv("STOCK_RESEARCH_CACHE_DIR", str(rt / "cache"))
    monkeypatch.setenv("STOCK_RESEARCH_DB_PATH", str(db_path))
    monkeypatch.setenv("STOCK_RESEARCH_LEDGER_DB_PATH", str(ledger_path))
    monkeypatch.setenv("STOCK_RESEARCH_STRATEGY_DIR", str(strat_dir))
    monkeypatch.setenv("JEVDB_MARKET_DB", str(db_path))
    monkeypatch.setenv("JEVDB_PAPER_LEDGER_DB", str(ledger_path))
    return {
        "runtime": rt,
        "db_path": db_path,
        "ledger_path": ledger_path,
        "strategy_dir": strat_dir,
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _parse_sse_events(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        ev_name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if data_lines:
            events.append((ev_name, json.loads("\n".join(data_lines))))
    return events


# ---------------------------------------------------------------------------
# 1. Health, Static SPA Serving & Error Routing
# ---------------------------------------------------------------------------


def test_health_favicon_and_spa_routes(isolated_api_env: dict[str, Path]) -> None:
    app = create_app()
    with TestClient(app) as client:
        r_health = client.get("/api/health")
        assert r_health.status_code == 200
        h = r_health.json()
        assert h["status"] == "ok"
        assert h["jevdb_version"] == "0.1.0"
        assert h["duckdb_version"] == "1.4.3"

        r_fav = client.get("/favicon.ico")
        assert r_fav.status_code == 204

        r_root = client.get("/")
        assert r_root.status_code == 200
        assert "text/html" in r_root.headers.get("content-type", "")
        assert "id=\"root\"" in r_root.text or "id='root'" in r_root.text

        r_api_404 = client.get("/api/does_not_exist")
        assert r_api_404.status_code == 404


# ---------------------------------------------------------------------------
# 2. Market Data Endpoints, Deduplication & Offline Mocked Ingest
# ---------------------------------------------------------------------------


def test_market_bars_unknown_sector_dedup_fundamentals_news(
    isolated_api_env: dict[str, Path],
) -> None:
    app = create_app()
    with TestClient(app) as client:
        # Seed duplicate sector snapshots & constituents from two sources + fundamentals + news
        with db_session(isolated_api_env["db_path"]) as con:
            con.execute(
                """
                INSERT INTO sector_snapshots VALUES
                ('CN', '电力', 'BK0428', DATE '2025-05-15', 2.10, 1100000, 95000000, '600900', '{}', 'akshare_sina'),
                ('CN', '电力', 'BK0428', DATE '2025-05-15', 2.35, 1200000, 98000000, '600900', '{}', 'akshare_ths')
                """
            )
            con.execute(
                """
                INSERT INTO sector_constituents VALUES
                ('电力', '600900', '长江电力', 29.50, 1.80, 18.2, 2.5, 7200e8, TIMESTAMP '2025-05-15 09:30:00', 'akshare_ths'),
                ('电力', '600900', '长江电力', 29.88, 2.15, 18.4, 2.52, 7280e8, TIMESTAMP '2025-05-15 15:00:00', 'akshare_sina'),
                ('电力', '600011', '华能国际', 7.45, 0.95, 14.1, 1.3, 1100e8, TIMESTAMP '2025-05-15 15:00:00', 'akshare_ths')
                """
            )
            con.execute(
                """
                INSERT INTO fundamentals VALUES
                ('000858', 'CN', DATE '2025-03-31', 16.5, 3.8, 0.23, 320e8, 120e8, 0.25, '{}', 'akshare_em', TIMESTAMP '2025-05-15 10:00:00')
                """
            )

        r_bars = client.get("/api/market/bars", params={"symbol": "000858", "market": "CN", "days": 30})
        assert r_bars.status_code == 200
        bars = r_bars.json()["bars"]
        assert len(bars) >= 5
        dates = [b["date"] for b in bars]
        assert dates == sorted(set(dates))

        # Unknown symbol must return 200 with [] (never 500)
        r_unk = client.get("/api/market/bars", params={"symbol": "UNKNOWN_999", "market": "CN"})
        assert r_unk.status_code == 200
        assert r_unk.json()["bars"] == []

        # Deduplicated sector snapshots & constituents
        r_sec = client.get("/api/market/sector", params={"name": "电力"})
        assert r_sec.status_code == 200
        sec_data = r_sec.json()
        assert len(sec_data["snapshots"]) == 1
        assert sec_data["snapshots"][0]["source"] == "akshare_ths"
        assert sec_data["snapshots"][0]["change_pct"] == pytest.approx(2.35)
        assert len(sec_data["constituents"]) == 2
        cj = [c for c in sec_data["constituents"] if c["symbol"] == "600900"][0]
        assert cj["price"] == pytest.approx(29.88)
        assert cj["source"] == "akshare_sina"

        r_fund = client.get("/api/market/fundamentals", params={"symbol": "000858", "market": "CN"})
        assert r_fund.status_code == 200
        assert r_fund.json()["fundamentals"][0]["pe_ttm"] == pytest.approx(16.5)

        r_news = client.get("/api/market/news", params={"symbol": "000858", "limit": 20})
        assert r_news.status_code == 200
        news_items = r_news.json()["news"]
        assert isinstance(news_items, list) and len(news_items) >= 1


def test_market_ingest_dispatch_and_validation(
    isolated_api_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_sector(**kwargs: Any) -> dict[str, Any]:
        calls.append(("a_share_sector", kwargs))
        return {"status": "ok", "rows_written": 12, "sector": kwargs["sector"]}

    def _fake_cn(**kwargs: Any) -> dict[str, Any]:
        calls.append(("a_share_stock", kwargs))
        return {"status": "ok", "rows_written": 20, "symbol": kwargs["symbol"]}

    def _fake_us(**kwargs: Any) -> dict[str, Any]:
        calls.append(("us_stock", kwargs))
        return {"status": "ok", "rows_written": 15, "symbol": kwargs["symbol"]}

    def _fake_edgar(**kwargs: Any) -> dict[str, Any]:
        calls.append(("sec_edgar", kwargs))
        return {"status": "ok", "rows_written": 5, "symbol": kwargs["symbol"]}

    def _fake_fh_news(**kwargs: Any) -> dict[str, Any]:
        calls.append(("finnhub_news", kwargs))
        return {"status": "ok", "rows_written": 8}

    def _fake_fh_earn(**kwargs: Any) -> dict[str, Any]:
        calls.append(("finnhub_earnings", kwargs))
        return {"status": "ok", "rows_written": 4}

    monkeypatch.setattr(api_module, "ingest_a_share_sector", _fake_sector)
    monkeypatch.setattr(api_module, "ingest_a_share_stock", _fake_cn)
    monkeypatch.setattr(api_module, "ingest_us_stock", _fake_us)
    monkeypatch.setattr(api_module, "ingest_sec_edgar", _fake_edgar)
    monkeypatch.setattr(api_module, "ingest_finnhub_news", _fake_fh_news)
    monkeypatch.setattr(api_module, "ingest_finnhub_earnings", _fake_fh_earn)

    app = create_app()
    with TestClient(app) as client:
        for body in (
            {"kind": "a_share_sector", "sector": "电力", "days": 5},
            {"kind": "a_share_stock", "symbol": "000858", "days": 10},
            {"kind": "us_stock", "symbol": "AAPL", "period": "1mo"},
            {"kind": "sec_edgar", "symbol": "AAPL"},
            {"kind": "finnhub_news", "symbol": "AAPL", "days": 7},
            {"kind": "finnhub_earnings", "from_date": "2025-05-01", "to_date": "2025-05-15"},
        ):
            res = client.post("/api/market/ingest", json=body)
            assert res.status_code == 200, res.text
            assert res.json()["status"] == "ok"

        assert [c[0] for c in calls] == [
            "a_share_sector",
            "a_share_stock",
            "us_stock",
            "sec_edgar",
            "finnhub_news",
            "finnhub_earnings",
        ]

        # Missing symbol or unknown kind -> 422
        assert client.post("/api/market/ingest", json={"kind": "a_share_stock"}).status_code == 422
        assert client.post("/api/market/ingest", json={"kind": "unknown_source"}).status_code == 422


# ---------------------------------------------------------------------------
# 3. Quant Strategy CRUD, Backtest, Compare & Portfolio Risk
# ---------------------------------------------------------------------------


def test_quant_strategies_backtest_compare_and_risk(
    isolated_api_env: dict[str, Path],
) -> None:
    app = create_app()
    with TestClient(app) as client:
        r_list = client.get("/api/quant/strategies")
        assert r_list.status_code == 200
        ids = {s["strategy_id"] for s in r_list.json()["strategies"]}
        assert {"value_quality_margin_of_safety", "cn_daily_momentum_sentiment"}.issubset(ids)

        new_strat = {
            "strategy_id": "m4_test_mom",
            "name": "M4 Test Momentum",
            "style": "quant_daily",
            "market": "CN",
            "description": "M4 API test strategy",
            "params": {"position_weight": 0.3},
            "rules": {
                "buy": {"field": "momentum_return", "op": ">", "value": 0.0},
                "sell": {"field": "momentum_return", "op": "<", "value": 0.0},
            },
            "source": "agent",
        }
        r_create = client.post("/api/quant/strategies", json=new_strat)
        assert r_create.status_code == 200
        assert r_create.json()["strategy_id"] == "m4_test_mom"

        # Invalid strategy spec -> 422 (bad id, null fields, empty strings, non-strings)
        r_bad_strat = client.post("/api/quant/strategies", json={"strategy_id": "bad id!", "name": "Bad"})
        assert r_bad_strat.status_code == 422
        assert (
            client.post(
                "/api/quant/strategies",
                json={"strategy_id": None, "name": None, "style": None},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/quant/strategies",
                json={"strategy_id": "   ", "name": "Empty ID", "style": "value"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/quant/strategies",
                json={"strategy_id": 123, "name": "Numeric ID", "style": "value"},
            ).status_code
            == 422
        )

        # NaN / Infinity JSON payloads must return 422 cleanly (never 500)
        r_nan_bt = client.post(
            "/api/quant/backtest",
            content=b'{"strategy_id":"cn_daily_momentum_sentiment","symbols":["000858"],"start_date":"2025-04-24","end_date":"2025-05-15","initial_capital":NaN}',
            headers={"Content-Type": "application/json"},
        )
        assert r_nan_bt.status_code == 422
        assert isinstance(r_nan_bt.json().get("detail"), list)

        r_inf_reset = client.post(
            "/api/paper/reset",
            content=b'{"initial_capital":Infinity}',
            headers={"Content-Type": "application/json"},
        )
        assert r_inf_reset.status_code == 422
        assert isinstance(r_inf_reset.json().get("detail"), list)

        r_nan_order = client.post(
            "/api/paper/order",
            content=b'{"symbol":"000858","side":"BUY","quantity":100,"order_type":"limit","limit_price":NaN}',
            headers={"Content-Type": "application/json"},
        )
        assert r_nan_order.status_code == 422

        # Deterministic backtest
        bt_payload = {
            "strategy_id": "cn_daily_momentum_sentiment",
            "symbols": ["000858", "601318"],
            "start_date": "2025-04-24",
            "end_date": "2025-05-15",
            "initial_capital": 100000.0,
        }
        bt1 = client.post("/api/quant/backtest", json=bt_payload)
        bt2 = client.post("/api/quant/backtest", json=bt_payload)
        assert bt1.status_code == 200 and bt2.status_code == 200
        assert bt1.json()["reproducibility_hash"] == bt2.json()["reproducibility_hash"]
        assert len(bt1.json()["equity_curve"]) > 0

        # Error mapping: unknown strategy -> 404, invalid capital/symbols -> 422
        assert (
            client.post(
                "/api/quant/backtest",
                json={**bt_payload, "strategy_id": "missing_strat_xyz"},
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/quant/backtest",
                json={**bt_payload, "initial_capital": -100.0},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/quant/backtest",
                json={**bt_payload, "symbols": []},
            ).status_code
            == 422
        )

        # Multi-strategy comparison
        r_cmp = client.post(
            "/api/quant/compare",
            json={
                "strategy_ids": ["value_quality_margin_of_safety", "m4_test_mom"],
                "symbols": ["000858", "601318"],
                "start_date": "2025-04-24",
                "end_date": "2025-05-15",
            },
        )
        assert r_cmp.status_code == 200
        cmp_data = r_cmp.json()
        assert len(cmp_data["results"]) == 2
        assert set(cmp_data["results"].keys()) == {
            "value_quality_margin_of_safety",
            "m4_test_mom",
        }
        assert len(cmp_data["aligned_equity_curves"]) > 0
        assert "value_quality_margin_of_safety" in cmp_data["aligned_equity_curves"][0]
        assert "m4_test_mom" in cmp_data["aligned_equity_curves"][0]

        # Portfolio risk (explicit price_history & DB-backed price_history=None)
        r_risk1 = client.post(
            "/api/quant/portfolio-risk",
            json={
                "holdings": [
                    {"symbol": "000858", "weight": 0.5, "sector": "消费"},
                    {"symbol": "601318", "weight": 0.5, "sector": "金融"},
                ],
                "price_history": {
                    "000858": [129.0, 130.0, 130.85, 131.5],
                    "601318": [50.0, 50.5, 51.2, 50.9],
                },
            },
        )
        assert r_risk1.status_code == 200
        assert r_risk1.json()["hhi"] == pytest.approx(0.5, abs=0.02)

        r_risk2 = client.post(
            "/api/quant/portfolio-risk",
            json={
                "holdings": [
                    {"symbol": "000858", "weight": 0.6},
                    {"symbol": "601318", "weight": 0.4},
                ],
            },
        )
        assert r_risk2.status_code == 200
        assert r_risk2.json()["hhi"] == pytest.approx(0.52, abs=0.02)

        # Invalid holding weight -> 422; empty holdings=[] preserves warning behavior (200)
        assert (
            client.post(
                "/api/quant/portfolio-risk",
                json={"holdings": [{"symbol": "000858", "weight": "bad"}]},
            ).status_code
            == 422
        )
        r_risk_empty = client.post("/api/quant/portfolio-risk", json={"holdings": []})
        assert r_risk_empty.status_code == 200
        assert "empty_or_zero_portfolio" in r_risk_empty.json().get("warnings", [])


def test_baseline_seeded_even_when_dynamic_bars_exist(
    isolated_api_env: dict[str, Path],
) -> None:
    """Pre-existing dynamic ingestion (e.g. CLI ask before web start) must not suppress static_baseline seeding."""
    with db_session(isolated_api_env["db_path"]) as con:
        con.execute(
            """
            INSERT INTO market_bars VALUES
            ('600900', 'CN', DATE '2026-11-26', 29.0, 29.5, 28.9, 29.4, 10000, 294000, 'akshare_tx')
            """
        )

    app = create_app()
    with TestClient(app) as client:
        r_cmp = client.post(
            "/api/quant/compare",
            json={
                "strategy_ids": ["value_quality_margin_of_safety", "cn_daily_momentum_sentiment"],
                "symbols": ["000858", "601318"],
                "start_date": "2025-04-24",
                "end_date": "2025-05-15",
                "initial_capital": 100000,
                "market": "CN",
            },
        )
        assert r_cmp.status_code == 200, r_cmp.text
        assert len(r_cmp.json()["results"]) == 2


# ---------------------------------------------------------------------------
# 4. Paper-Trading Lifecycle & Rule Enforcement
# ---------------------------------------------------------------------------


def test_paper_trading_lifecycle_t1_and_validation(
    isolated_api_env: dict[str, Path],
) -> None:
    app = create_app()
    with TestClient(app) as client:
        r_reset = client.post("/api/paper/reset", json={"initial_capital": 100000.0})
        assert r_reset.status_code == 200
        d1 = r_reset.json()["current_date"]
        d2 = (date.fromisoformat(d1) + timedelta(days=1)).isoformat()

        # Missing required fields / overflow quantity -> 422
        assert client.post("/api/paper/order", json={}).status_code == 422
        assert (
            client.post(
                "/api/paper/order",
                json={"symbol": "000858", "side": "BUY", "quantity": 3_000_000_000},
            ).status_code
            == 422
        )

        # Invalid CN buy lot size (50 shares) -> rejected cleanly preserving cash
        r_bad_lot = client.post(
            "/api/paper/order",
            json={"symbol": "000858", "side": "BUY", "quantity": 50, "market": "CN"},
        )
        assert r_bad_lot.status_code == 200
        assert r_bad_lot.json()["accepted"] is False

        acc_after_rej = client.get("/api/paper/account").json()
        assert acc_after_rej["cash"] == pytest.approx(100000.0)
        assert acc_after_rej["positions"] == []

        # Valid CN buy with reference_bar=None (auto-resolved from market_bars)
        r_buy = client.post(
            "/api/paper/order",
            json={"symbol": "000858", "side": "BUY", "quantity": 100, "market": "CN", "trade_date": d1},
        )
        assert r_buy.status_code == 200
        assert r_buy.json()["accepted"] is True

        # Same-day T+1 sell rejected
        r_t1_sell = client.post(
            "/api/paper/order",
            json={"symbol": "000858", "side": "SELL", "quantity": 100, "market": "CN", "trade_date": d1},
        )
        assert r_t1_sell.status_code == 200
        assert r_t1_sell.json()["accepted"] is False

        # Advance day unlocks sellable_qty
        r_adv = client.post(
            "/api/paper/advance-day",
            json={"new_date": d2, "mark_prices": {"000858": 132.5}},
        )
        assert r_adv.status_code == 200
        assert r_adv.json()["positions"][0]["sellable_qty"] == 100

        # Next day sell succeeds with stamp duty
        r_sell = client.post(
            "/api/paper/order",
            json={
                "symbol": "000858",
                "side": "SELL",
                "quantity": 100,
                "market": "CN",
                "trade_date": d2,
                "reference_bar": {"open": 132.0, "high": 133.5, "low": 131.0, "close": 132.5, "prev_close": 130.85},
            },
        )
        assert r_sell.status_code == 200
        assert r_sell.json()["accepted"] is True
        assert r_sell.json()["stamp_duty"] > 0

        acc_final = client.get("/api/paper/account").json()
        assert len(acc_final["orders"]) >= 4
        assert len(acc_final["fills"]) == 2
        assert len(acc_final["equity_history"]) >= 2


# ---------------------------------------------------------------------------
# 5. Agent Query & SSE Stream
# ---------------------------------------------------------------------------


def test_agent_query_and_sse_stream_events_and_errors(
    isolated_api_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(
        prompt: str,
        mode: str = "auto",
        session_id: str | None = None,
        on_event: Any = None,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        if on_event is not None:
            on_event({"type": "status", "stage": "started", "prompt": prompt, "mode": mode})
            on_event({"type": "tool_start", "tool_call_id": "tc1", "name": "query_stored_market_data", "args": {"symbol": "000858"}})
            on_event({"type": "tool_end", "tool_call_id": "tc1", "name": "query_stored_market_data", "args": {"symbol": "000858"}, "result_summary": "15 bars", "duration_ms": 12, "is_error": False})
            on_event({"type": "verification", "grounded": True, "passed": True, "ungrounded_numbers": [], "warnings": []})
            on_event({"type": "final_answer", "answer": "000858 五粮液近期收盘约 130.85 元，来源 market_bars。", "sources": ["duckdb:market_bars"], "duration_ms": 25})
        return {
            "prompt": prompt,
            "tool_calls": [
                {
                    "tool_call_id": "tc1",
                    "name": "query_stored_market_data",
                    "args": {"symbol": "000858"},
                    "result_summary": "15 bars",
                    "duration_ms": 12,
                }
            ],
            "verification": {"grounded": True, "passed": True, "ungrounded_numbers": []},
            "answer": "000858 五粮液近期收盘约 130.85 元，来源 market_bars。",
            "sources": ["duckdb:market_bars"],
            "duration_ms": 25,
        }

    monkeypatch.setattr(api_module, "run_research_query", _fake_run)

    app = create_app()
    with TestClient(app) as client:
        # Empty prompts -> 422
        assert client.post("/api/agent/query", json={"prompt": "   "}).status_code == 422
        assert client.post("/api/agent/query", json={}).status_code == 422
        assert client.get("/api/agent/stream", params={"prompt": "   "}).status_code == 422

        # Valid POST query
        r_q = client.post("/api/agent/query", json={"prompt": "分析000858"})
        assert r_q.status_code == 200
        q_json = r_q.json()
        assert len(q_json["tool_calls"]) == 1
        assert "000858" in q_json["answer"]

        # Valid GET SSE stream
        r_sse = client.get("/api/agent/stream", params={"prompt": "分析000858"})
        assert r_sse.status_code == 200
        assert "text/event-stream" in r_sse.headers.get("content-type", "")
        parsed = _parse_sse_events(r_sse.text)
        ev_names = [name for name, _ in parsed]
        assert ev_names == ["status", "tool_start", "tool_end", "verification", "final_answer"]
        assert parsed[-1][1]["sources"] == ["duckdb:market_bars"]

        # Error mappings: RuntimeError -> 503, TimeoutError -> 504
        def _raise_runtime(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("DEEPSEEK_API_KEY is missing")

        monkeypatch.setattr(api_module, "run_research_query", _raise_runtime)
        r_503 = client.post("/api/agent/query", json={"prompt": "test"})
        assert r_503.status_code == 503
        assert "DEEPSEEK_API_KEY" in r_503.json()["detail"]

        r_sse_err = client.get("/api/agent/stream", params={"prompt": "test"})
        assert r_sse_err.status_code == 200
        err_events = _parse_sse_events(r_sse_err.text)
        assert err_events[-1][0] == "error"
        assert "DEEPSEEK_API_KEY" in err_events[-1][1]["detail"]

        def _raise_timeout(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise TimeoutError("Pi agent timed out")

        monkeypatch.setattr(api_module, "run_research_query", _raise_timeout)
        assert client.post("/api/agent/query", json={"prompt": "test"}).status_code == 504


# ---------------------------------------------------------------------------
# 6. Concurrent DuckDB Access Stress Check
# ---------------------------------------------------------------------------


def test_concurrent_requests_no_duckdb_lock_conflict(
    isolated_api_env: dict[str, Path],
) -> None:
    app = create_app()
    with TestClient(app) as client:
        def _hit(idx: int) -> int:
            choice = idx % 4
            if choice == 0:
                return client.get("/api/market/bars", params={"symbol": "000858", "market": "CN"}).status_code
            if choice == 1:
                return client.get("/api/quant/strategies").status_code
            if choice == 2:
                return client.post(
                    "/api/quant/backtest",
                    json={
                        "strategy_id": "cn_daily_momentum_sentiment",
                        "symbols": ["000858", "601318"],
                        "start_date": "2025-04-24",
                        "end_date": "2025-05-15",
                    },
                ).status_code
            return client.get("/api/paper/account").status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(_hit, range(24)))
        assert all(code == 200 for code in codes), codes


# ---------------------------------------------------------------------------
# 7. Headless Chromium Playwright End-to-End UI Verification
# ---------------------------------------------------------------------------


def test_playwright_ui_all_views_validation_sse_and_reload_persistence(
    isolated_api_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist_index = Path(__file__).resolve().parents[1] / "web" / "dist" / "index.html"
    assert dist_index.is_file(), f"Built frontend missing at {dist_index}"
    assert Path(CHROMIUM_BIN).exists(), f"Chromium missing at {CHROMIUM_BIN}"

    def _fake_agent(
        prompt: str,
        mode: str = "auto",
        session_id: str | None = None,
        on_event: Any = None,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        if on_event is not None:
            on_event({"type": "status", "stage": "started", "prompt": prompt})
            on_event({"type": "tool_start", "tool_call_id": "pw1", "name": "query_stored_market_data", "args": {"sector": "电力"}})
            on_event({"type": "tool_end", "tool_call_id": "pw1", "name": "query_stored_market_data", "args": {"sector": "电力"}, "result_summary": "电力板块数据就绪", "duration_ms": 10, "is_error": False})
            on_event({"type": "verification", "grounded": True, "passed": True, "ungrounded_numbers": []})
            md_answer = (
                f"## 「{prompt}」核验分析\n\n"
                "**电力板块**近期走势稳健，核心标的如下：\n\n"
                "| 标的 | 说明 |\n|---|---|\n| 600900 | 长江电力 |\n"
            )
            on_event({"type": "final_answer", "answer": md_answer, "sources": ["duckdb:market_bars"], "duration_ms": 18})
        return {
            "prompt": prompt,
            "tool_calls": [{"tool_call_id": "pw1", "name": "query_stored_market_data", "args": {"sector": "电力"}, "result_summary": "电力板块数据就绪", "duration_ms": 10}],
            "verification": {"grounded": True, "passed": True},
            "answer": (
                f"## 「{prompt}」核验分析\n\n"
                "**电力板块**近期走势稳健，核心标的如下：\n\n"
                "| 标的 | 说明 |\n|---|---|\n| 600900 | 长江电力 |\n"
            ),
            "sources": ["duckdb:market_bars"],
            "duration_ms": 18,
        }

    monkeypatch.setattr(api_module, "run_research_query", _fake_agent)

    port = _free_port()
    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15.0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=1.0) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError("Live test uvicorn server failed to start")

    short_tmp = SCRATCH_TMP_ROOT.parent / f"pm4_{os.getpid()}"
    short_tmp.mkdir(parents=True, exist_ok=True)
    driver_script = r"""
import json, sys
from playwright.sync_api import sync_playwright

cfg = json.loads(sys.stdin.read())
base_url = cfg["base_url"]
console_errors, page_errors, server_errors, agent_reqs = [], [], [], []

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=cfg["chromium"],
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.on("response", lambda r: server_errors.append({"url": r.url, "status": r.status}) if r.status >= 500 else None)
    page.on("request", lambda r: agent_reqs.append(r.url) if "/api/agent/" in r.url else None)

    page.goto(base_url + "/", wait_until="networkidle", timeout=20000)
    page.locator("[data-testid='agent-input']").wait_for(state="visible", timeout=15000)

    # 1. Inspect all 8 testids and chart canvas dimensions on initial load
    selectors = [
        "[data-testid='agent-input']",
        "[data-testid='agent-submit']",
        "[data-testid='tool-calls-panel']",
        "[data-testid='agent-answer']",
        "[data-testid='market-chart']",
        "[data-testid='backtest-chart']",
        "[data-testid='paper-holdings']",
        "[data-testid='paper-pnl-chart']",
    ]
    views = {}
    for s in selectors:
        loc = page.locator(s).first
        box = loc.bounding_box()
        chart_rect = page.evaluate(
            "(sel) => { const r = document.querySelector(sel); const c = r && r.querySelector('canvas, svg'); if (!c) return null; const rc = c.getBoundingClientRect(); return {width: rc.width, height: rc.height}; }",
            s,
        )
        views[s] = {"visible": loc.is_visible(), "box": box, "chart": chart_rect}

    # 2. Empty prompt validation MUST block /api/agent/* requests
    page.locator("[data-testid='agent-input']").fill("   ")
    page.locator("[data-testid='agent-submit']").click()
    val_text = page.locator("[data-testid='agent-validation']").inner_text().strip()
    reqs_after_empty = list(agent_reqs)

    # 3. Real SSE research query flow in UI
    page.locator("[data-testid='agent-input']").fill("最近3天电力行业")
    page.locator("[data-testid='agent-submit']").click()
    page.wait_for_function(
        "() => { const el = document.querySelector('[data-testid=\"tool-calls-panel\"]'); return el && !['等待调用工具...', '正在执行工具链...'].includes(el.innerText.trim()); }",
        timeout=15000,
    )
    page.wait_for_function(
        "() => { const el = document.querySelector('[data-testid=\"agent-answer\"]'); return el && !['等待回答...', '正在核验结果...'].includes(el.innerText.trim()); }",
        timeout=15000,
    )
    tool_text = page.locator("[data-testid='tool-calls-panel']").inner_text().strip()
    answer_text = page.locator("[data-testid='agent-answer']").inner_text().strip()
    answer_md_tags = page.evaluate(
        "() => ({ h2: document.querySelectorAll('[data-testid=\"agent-answer\"] h2').length, strong: document.querySelectorAll('[data-testid=\"agent-answer\"] strong').length, table: document.querySelectorAll('[data-testid=\"agent-answer\"] table').length })"
    )

    # 4. Reload and confirm paper holdings persistence
    page.reload(wait_until="networkidle", timeout=20000)
    page.wait_for_function(
        "() => { const el = document.querySelector('[data-testid=\"paper-holdings\"]'); return el && el.innerText.trim().includes('000858'); }",
        timeout=15000,
    )
    holdings_text = page.locator("[data-testid='paper-holdings']").inner_text().strip()

    browser.close()

print(json.dumps({
    "views": views,
    "val_text": val_text,
    "reqs_after_empty": reqs_after_empty,
    "tool_text": tool_text,
    "answer_text": answer_text,
    "answer_md_tags": answer_md_tags,
    "holdings_text": holdings_text,
    "console_errors": console_errors,
    "page_errors": page_errors,
    "server_errors": server_errors,
}, ensure_ascii=False))
"""
    try:
        # Seed a paper order via HTTP before launching browser
        with urllib.request.urlopen(
            urllib.request.Request(
                f"{base_url}/api/paper/reset",
                data=json.dumps({"initial_capital": 100000.0}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        ) as r:
            d1 = json.loads(r.read().decode())["current_date"]

        with urllib.request.urlopen(
            urllib.request.Request(
                f"{base_url}/api/paper/order",
                data=json.dumps(
                    {
                        "symbol": "000858",
                        "side": "BUY",
                        "quantity": 100,
                        "market": "CN",
                        "trade_date": d1,
                        "reference_bar": {
                            "open": 130.11,
                            "high": 131.66,
                            "low": 129.90,
                            "close": 130.85,
                            "prev_close": 129.05,
                        },
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        ) as r:
            assert json.loads(r.read().decode())["accepted"] is True

        env = os.environ.copy()
        env["TMPDIR"] = str(short_tmp)
        proc = subprocess.run(
            [sys.executable, "-c", driver_script],
            input=json.dumps({"base_url": base_url, "chromium": CHROMIUM_BIN}),
            text=True,
            capture_output=True,
            env=env,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, f"Playwright driver failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
        out = json.loads(proc.stdout)

        assert out["console_errors"] == []
        assert out["page_errors"] == []
        assert out["server_errors"] == []
        for sel, info in out["views"].items():
            assert info["visible"] is True, f"{sel} not visible"
        for chart_sel in (
            "[data-testid='market-chart']",
            "[data-testid='backtest-chart']",
            "[data-testid='paper-pnl-chart']",
        ):
            chart = out["views"][chart_sel]["chart"]
            assert chart is not None and chart["width"] > 0 and chart["height"] > 0

        assert out["val_text"]
        assert out["reqs_after_empty"] == []
        assert "query_stored_market_data" in out["tool_text"]
        assert "电力" in out["answer_text"]
        assert "##" not in out["answer_text"] and "**" not in out["answer_text"]
        assert out["answer_md_tags"]["h2"] >= 1
        assert out["answer_md_tags"]["strong"] >= 1
        assert out["answer_md_tags"]["table"] >= 1
        assert "000858" in out["holdings_text"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        for child in short_tmp.glob("*"):
            if child.is_dir():
                import shutil
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        short_tmp.rmdir()


def test_playwright_paper_trading_ui_order_validation_t1_and_reload(
    isolated_api_env: dict[str, Path],
) -> None:
    dist_index = Path(__file__).resolve().parents[1] / "web" / "dist" / "index.html"
    assert dist_index.is_file(), f"Built frontend missing at {dist_index}"
    assert Path(CHROMIUM_BIN).exists(), f"Chromium missing at {CHROMIUM_BIN}"

    port = _free_port()
    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15.0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=1.0) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError("Live test uvicorn server failed to start")

    short_tmp = SCRATCH_TMP_ROOT.parent / f"pm4p_{os.getpid()}"
    short_tmp.mkdir(parents=True, exist_ok=True)
    driver_script = r"""
import json, sys
from playwright.sync_api import sync_playwright

cfg = json.loads(sys.stdin.read())
base_url = cfg["base_url"]
console_errors, page_errors, server_errors, order_reqs = [], [], [], []

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=cfg["chromium"],
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))
    page.on("response", lambda r: server_errors.append({"url": r.url, "status": r.status}) if r.status >= 500 else None)
    page.on("request", lambda r: order_reqs.append(r.url) if "/api/paper/order" in r.url else None)

    page.goto(base_url + "/", wait_until="networkidle", timeout=20000)
    page.locator("[data-tab='paper']").click()

    paper_sec = page.locator("#section-paper")
    qty_input = paper_sec.locator("input[aria-label='下单数量']")
    qty_input.wait_for(state="visible", timeout=15000)

    # 1. Default quantity 100 MUST pass HTML5 validity (no stepMismatch)
    default_validity = qty_input.evaluate(
        "el => ({value: el.value, min: el.min, step: el.step, valid: el.checkValidity(), stepMismatch: el.validity.stepMismatch})"
    )

    # 2. Odd-lot CN BUY 50 must reach handleOrder and render visible .err banner without firing /api/paper/order
    qty_input.fill("50")
    paper_sec.locator("button:has-text('提交模拟订单')").click()
    err_50 = paper_sec.locator(".err").inner_text().strip()
    reqs_after_50 = list(order_reqs)

    # 3. Valid CN BUY 100 (000858) must hit /api/paper/order, display 成交成功, and populate paper-holdings
    qty_input.fill("100")
    paper_sec.locator("button:has-text('提交模拟订单')").click()
    paper_sec.locator(".ok-banner").wait_for(state="visible", timeout=15000)
    ok_100 = paper_sec.locator(".ok-banner").inner_text().strip()
    page.wait_for_function(
        "() => { const el = document.querySelector('[data-testid=\"paper-holdings\"]'); return el && el.innerText.includes('000858'); }",
        timeout=15000,
    )
    reqs_after_100 = list(order_reqs)

    # 4. Same-day SELL 100 must be rejected by T+1 rule with 订单被规则拦截 banner
    paper_sec.locator("select[aria-label='买卖方向']").select_option("SELL")
    paper_sec.locator("button:has-text('提交模拟订单')").click()
    paper_sec.locator(".err").wait_for(state="visible", timeout=15000)
    err_t1 = paper_sec.locator(".err").inner_text().strip()

    # 5. Advance trading day and reload page to verify persistence (quantity=100, sellable_qty=100)
    paper_sec.locator("button:has-text('推进下一交易日')").click()
    paper_sec.locator(".ok-banner").wait_for(state="visible", timeout=15000)
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('[data-testid=\"paper-holdings\"] tbody tr')).some(tr => { const c = Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim()); return c[0] === '000858' && c[2] === '100' && c[3] === '100'; })",
        timeout=15000,
    )

    page.reload(wait_until="networkidle", timeout=20000)
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('[data-testid=\"paper-holdings\"] tbody tr')).some(tr => { const c = Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim()); return c[0] === '000858' && c[2] === '100' && c[3] === '100'; })",
        timeout=15000,
    )
    persisted_row = page.evaluate(
        "() => { const tr = Array.from(document.querySelectorAll('[data-testid=\"paper-holdings\"] tbody tr')).find(r => r.querySelector('td') && r.querySelector('td').innerText.trim() === '000858'); return tr ? Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim()) : null; }"
    )

    browser.close()

print(json.dumps({
    "default_validity": default_validity,
    "err_50": err_50,
    "reqs_after_50": reqs_after_50,
    "ok_100": ok_100,
    "reqs_after_100": reqs_after_100,
    "err_t1": err_t1,
    "persisted_row": persisted_row,
    "console_errors": console_errors,
    "page_errors": page_errors,
    "server_errors": server_errors,
}, ensure_ascii=False))
"""
    try:
        env = os.environ.copy()
        env["TMPDIR"] = str(short_tmp)
        proc = subprocess.run(
            [sys.executable, "-c", driver_script],
            input=json.dumps({"base_url": base_url, "chromium": CHROMIUM_BIN}),
            text=True,
            capture_output=True,
            env=env,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, f"Playwright driver failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
        out = json.loads(proc.stdout)

        assert out["console_errors"] == []
        assert out["page_errors"] == []
        assert out["server_errors"] == []
        assert out["default_validity"]["valid"] is True
        assert out["default_validity"]["stepMismatch"] is False
        assert "100 股" in out["err_50"]
        assert out["reqs_after_50"] == []
        assert "成交成功" in out["ok_100"]
        assert len(out["reqs_after_100"]) == 1
        assert "订单被规则拦截" in out["err_t1"]
        assert out["persisted_row"] is not None
        assert out["persisted_row"][0] == "000858"
        assert out["persisted_row"][2] == "100"
        assert out["persisted_row"][3] == "100"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        for child in short_tmp.glob("*"):
            if child.is_dir():
                import shutil
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        short_tmp.rmdir()


def test_quant_custom_date_range_autofetch_and_risk_window(
    isolated_api_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ingest_calls: list[dict[str, Any]] = []

    def _fake_cn_ingest(**kwargs: Any) -> dict[str, Any]:
        ingest_calls.append(kwargs)
        sym = str(kwargs["symbol"])
        db_p = kwargs["db_path"]
        rows = [
            (sym, "CN", date(2026, 1, 5), 100.0, 102.0, 99.0, 101.0, 100000.0, 10100000.0, "akshare_tx"),
            (sym, "CN", date(2026, 1, 6), 101.0, 105.0, 100.0, 104.0, 120000.0, 12480000.0, "akshare_tx"),
            (sym, "CN", date(2026, 1, 7), 104.0, 106.0, 101.0, 102.0, 110000.0, 11220000.0, "akshare_tx"),
            (sym, "CN", date(2026, 1, 8), 102.0, 108.0, 102.0, 107.0, 150000.0, 16050000.0, "akshare_tx"),
            (sym, "CN", date(2026, 1, 9), 107.0, 110.0, 105.0, 109.0, 140000.0, 15260000.0, "akshare_tx"),
        ]
        with db_session(db_p, init=True) as con:
            con.executemany(
                """
                INSERT INTO market_bars (
                    symbol, market, trade_date, open, high, low, close, volume, amount, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, market, trade_date, source) DO NOTHING
                """,
                rows,
            )
        return {"status": "success", "bars": len(rows), "symbol": sym}

    monkeypatch.setattr(api_module, "ingest_a_share_stock", _fake_cn_ingest)

    app = create_app()
    with TestClient(app) as client:
        # 1. Existing static baseline window must NOT trigger network auto_fetch
        r_base = client.post(
            "/api/quant/compare",
            json={
                "strategy_ids": ["value_quality_margin_of_safety", "cn_daily_momentum_sentiment"],
                "symbols": ["000858", "601318"],
                "start_date": "2025-04-24",
                "end_date": "2025-05-15",
                "initial_capital": 100000.0,
                "market": "CN",
                "auto_fetch": True,
            },
        )
        assert r_base.status_code == 200, r_base.text
        assert ingest_calls == []

        # 2. Missing custom window WITH auto_fetch=True fetches bars & runs backtest
        r_custom = client.post(
            "/api/quant/compare",
            json={
                "strategy_ids": ["value_quality_margin_of_safety", "cn_daily_momentum_sentiment"],
                "symbols": ["000858", "601318"],
                "start_date": "2026-01-05",
                "end_date": "2026-01-09",
                "initial_capital": 100000.0,
                "market": "CN",
                "auto_fetch": True,
            },
        )
        assert r_custom.status_code == 200, r_custom.text
        body = r_custom.json()
        assert body["start_date"] == "2026-01-05"
        assert body["end_date"] == "2026-01-09"
        assert sorted(body.get("auto_fetched_symbols", [])) == ["000858", "601318"]
        assert len(ingest_calls) == 2
        assert all(c["start_date"] == "2026-01-05" and c["end_date"] == "2026-01-09" for c in ingest_calls)

        # 3. Portfolio risk respects explicit start_date / end_date and rejects inverted range
        r_bad = client.post(
            "/api/quant/portfolio-risk",
            json={
                "holdings": [{"symbol": "000858", "weight": 0.5}, {"symbol": "601318", "weight": 0.5}],
                "start_date": "2026-01-09",
                "end_date": "2026-01-05",
            },
        )
        assert r_bad.status_code == 422

        r_risk_2025 = client.post(
            "/api/quant/portfolio-risk",
            json={
                "holdings": [{"symbol": "000858", "weight": 0.5}, {"symbol": "601318", "weight": 0.5}],
                "start_date": "2025-04-24",
                "end_date": "2025-05-15",
            },
        )
        r_risk_2026 = client.post(
            "/api/quant/portfolio-risk",
            json={
                "holdings": [{"symbol": "000858", "weight": 0.5}, {"symbol": "601318", "weight": 0.5}],
                "start_date": "2026-01-05",
                "end_date": "2026-01-09",
            },
        )
        assert r_risk_2025.status_code == 200 and r_risk_2026.status_code == 200
        assert r_risk_2025.json()["portfolio_volatility"] != pytest.approx(
            r_risk_2026.json()["portfolio_volatility"], rel=1e-4
        )
