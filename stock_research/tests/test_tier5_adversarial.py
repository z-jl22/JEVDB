"""Tier 5 adversarial regression & boundary tests covering Categories A–F."""

from __future__ import annotations

import importlib
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from stock_research import cli
from stock_research.agent.verifier import _classify_number_grounding, verify_answer_grounding
from stock_research.api.app import create_app
from stock_research.config import get_settings
from stock_research.mcp_server import server as mcp_srv
from stock_research.mcp_server import web_tools
from stock_research.quant.backtest import compare_strategies, load_bars_from_duckdb, run_backtest
from stock_research.quant.ledger import PaperAccountService
from stock_research.quant.risk import compute_portfolio_risk
from stock_research.quant.strategies import (
    StrategySpec,
    evaluate_strategy_signals,
)
from stock_research.storage.ingest import akshare_cn, finnhub_us, sec_edgar, yfinance_us
from stock_research.storage.jevdb_bridge import hybrid_semantic_query
from stock_research.storage.schema import connect_db, db_session, ensure_baseline_seeded
from stock_research.tests.e2e.conftest import PlaywrightRunner

CHROMIUM_BIN = "/usr/bin/chromium"
SCRATCH_TMP_BASE = Path("/home/tao/.gemini/antigravity/scratch/teamwork_tmp")


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
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
    monkeypatch.setenv("JEVDB_STRATEGY_DIR", str(strat_dir))
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()  # type: ignore[attr-defined]
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


# ---------------------------------------------------------------------------
# Category A: Shared Baseline Seeding, MCP Query & CLI Parity
# ---------------------------------------------------------------------------


def test_shared_baseline_seeding_mcp_and_cli_when_dynamic_bars_exist(
    isolated_env: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = isolated_env["db_path"]
    # Pre-seed dynamic bars & news before any static_baseline seeding
    with db_session(db_path) as con:
        con.execute(
            """
            INSERT INTO market_bars VALUES
            ('600900', 'CN', DATE '2026-11-25', 29.0, 29.5, 28.9, 29.4, 10000, 294000, 'akshare_tx'),
            ('000858', 'CN', DATE '2026-11-25', 135.0, 136.0, 134.0, 135.5, 20000, 2710000, 'akshare_tx')
            """
        )
        con.execute(
            """
            INSERT INTO news_events VALUES
            ('dyn_1', '600900', 'CN', '电力', TIMESTAMP '2026-11-25 10:00:00',
             'Dynamic news', 'Dynamic summary', '', 'akshare_em', NULL, '["CN","600900"]', ['CN', '600900'])
            """
        )

    bt = mcp_srv.run_backtest_tool(
        "cn_daily_momentum_sentiment",
        ["000858", "601318"],
        "2025-04-24",
        "2025-05-15",
    )
    assert len(bt["equity_curve"]) >= 5

    cmp_res = mcp_srv.compare_strategies_tool(
        ["value_quality_margin_of_safety", "cn_daily_momentum_sentiment"],
        ["000858", "601318"],
        "2025-04-24",
        "2025-05-15",
    )
    assert set(cmp_res["results"].keys()) == {
        "value_quality_margin_of_safety",
        "cn_daily_momentum_sentiment",
    }

    q = mcp_srv.query_stored_market_data(table="auto", symbol="000858")
    assert len(q["market_bars"]) >= 5
    assert len(q["news_events"]) >= 1

    risk = mcp_srv.analyze_portfolio_risk(
        holdings=[{"symbol": "000858", "weight": 0.5}, {"symbol": "601318", "weight": 0.5}],
        price_history=None,
    )
    assert risk["portfolio_volatility"] > 0.0
    assert not any("missing_or_short_price_history" in w for w in risk.get("warnings", []))

    rc = cli.main(
        [
            "backtest",
            "--strategy-id",
            "cn_daily_momentum_sentiment",
            "--symbols",
            "000858,601318",
            "--start-date",
            "2025-04-24",
            "--end-date",
            "2026-12-01",
        ]
    )
    assert rc == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert len(cli_out["equity_curve"]) >= 5


def test_empty_db_cli_lookup_and_mcp_submit_paper_order_seed_baseline(
    isolated_env: dict[str, Path],
) -> None:
    # Brand new empty DB must auto-seed static_baseline for both CLI and MCP paper order
    bar = cli._lookup_latest_bar("000858", "CN")
    assert bar is not None
    assert bar["close"] > 0

    # Reset ledger to isolated path and submit via MCP without reference_bar
    PaperAccountService(isolated_env["ledger_path"])
    order_res = mcp_srv.submit_paper_order(
        symbol="000858",
        side="BUY",
        quantity=100,
        market="CN",
    )
    assert order_res["accepted"] is True


def test_mcp_query_stored_us_auto_and_compact_compare_list(
    isolated_env: dict[str, Path],
) -> None:
    with db_session(isolated_env["db_path"]) as con:
        ensure_baseline_seeded(con)
        con.execute(
            """
            INSERT INTO edgar_filings VALUES
            ('0000320193', 'AAPL', '0000320193-26-000001', '10-K', DATE '2026-11-01', DATE '2026-09-30', 'aapl.htm', 'AAPL 10-K', 'https://www.sec.gov/')
            """
        )
        con.execute(
            """
            INSERT INTO earnings_calendar VALUES
            ('AAPL', DATE '2026-11-02', 1.60, 1.64, 94000000000, 94930000000, 'finnhub', TIMESTAMP '2026-11-03 00:00:00')
            """
        )

    res = mcp_srv.query_stored_market_data(table="auto", symbol="AAPL")
    assert "edgar_filings" in res and len(res["edgar_filings"]) == 1
    assert "earnings_calendar" in res and len(res["earnings_calendar"]) == 1

    # 400-day comparison payload with list-typed aligned curves must compact strictly <= max_bytes
    aligned_eq = [
        {"date": f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "s1": 100000.0 + i, "s2": 100000.0 + i * 2}
        for i in range(400)
    ]
    aligned_dd = [
        {"date": f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}", "s1": -0.01 * (i % 5), "s2": -0.02 * (i % 5)}
        for i in range(400)
    ]
    big_payload = {
        "strategy_ids": ["s1", "s2"],
        "aligned_equity_curves": aligned_eq,
        "aligned_drawdown_curves": aligned_dd,
        "benchmark_curve": [{"date": pt["date"], "equity": 100000.0 + i} for i, pt in enumerate(aligned_eq)],
        "results": {
            "s1": {"equity_curve": aligned_eq, "trades": [{"idx": i} for i in range(50)], "metrics": {"sharpe_ratio": 1.1}},
            "s2": {"equity_curve": aligned_eq, "trades": [{"idx": i} for i in range(50)], "metrics": {"sharpe_ratio": 0.9}},
        },
        "ranking": [{"strategy_id": "s1", "sharpe_ratio": 1.1}],
    }
    raw_len = len(json.dumps(big_payload, ensure_ascii=False))
    assert raw_len > 16000
    compacted = mcp_srv._compact_backtest_payload(big_payload, max_bytes=16000)
    compacted_len = len(json.dumps(compacted, ensure_ascii=False))
    assert compacted_len <= 16000
    assert len(compacted["aligned_equity_curves"]) <= 12
    assert len(compacted["aligned_drawdown_curves"]) <= 12


# ---------------------------------------------------------------------------
# Category B: Web UI / API Validation & Playwright PaperView Verification
# ---------------------------------------------------------------------------


def test_api_strict_validation_boundaries(isolated_env: dict[str, Path]) -> None:
    with db_session(isolated_env["db_path"]) as con:
        ensure_baseline_seeded(con)
        con.execute(
            """
            INSERT INTO market_bars VALUES
            ('600900', 'CN', DATE '2026-11-25', 29.0, 29.5, 28.9, 29.4, 10000, 294000, 'akshare_tx')
            """
        )

    app = create_app()
    with TestClient(app) as client:
        # Whitespace-only symbol -> 422
        assert client.get("/api/market/bars", params={"symbol": "   "}).status_code == 422
        assert client.get("/api/market/fundamentals", params={"symbol": "   "}).status_code == 422

        # Explicit empty/whitespace ?name= -> 422; omitted ?name -> 200 default "电力"
        assert client.get("/api/market/sector", params={"name": ""}).status_code == 422
        assert client.get("/api/market/sector", params={"name": "   "}).status_code == 422
        r_default_sec = client.get("/api/market/sector")
        assert r_default_sec.status_code == 200
        assert r_default_sec.json()["sector"] == "电力"

        # CN symbol prefix normalization when market omitted or CN
        r_sh = client.get("/api/market/bars", params={"symbol": "sh600900"})
        assert r_sh.status_code == 200
        assert r_sh.json()["symbol"] == "600900"
        assert r_sh.json()["market"] == "CN"
        assert len(r_sh.json()["bars"]) == 1

        r_sh_f = client.get("/api/market/fundamentals", params={"symbol": "sh600900"})
        assert r_sh_f.status_code == 200
        assert r_sh_f.json()["symbol"] == "600900"
        assert r_sh_f.json()["market"] == "CN"

        # Boolean rejection on numeric Pydantic fields -> 422
        assert (
            client.post(
                "/api/paper/advance-day",
                json={"new_date": "2026-12-01", "mark_prices": {"000858": True}},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/paper/order",
                json={"symbol": "000858", "side": "BUY", "quantity": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/paper/order",
                json={"symbol": "000858", "side": "BUY", "quantity": 100, "order_type": "limit", "limit_price": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/paper/order",
                json={"symbol": "000858", "side": "BUY", "quantity": 100, "price_limit_rate": True},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/quant/backtest",
                json={
                    "strategy_id": "cn_daily_momentum_sentiment",
                    "symbols": ["000858"],
                    "start_date": "2025-04-24",
                    "end_date": "2025-05-15",
                    "initial_capital": True,
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/quant/compare",
                json={
                    "strategy_ids": ["cn_daily_momentum_sentiment"],
                    "symbols": ["000858"],
                    "start_date": "2025-04-24",
                    "end_date": "2025-05-15",
                    "initial_capital": True,
                },
            ).status_code
            == 422
        )
        assert client.post("/api/paper/reset", json={"initial_capital": True}).status_code == 422
        assert client.post("/api/quant/portfolio-risk", json={"holdings": [], "days": True}).status_code == 422
        assert client.post("/api/quant/portfolio-risk", json={"holdings": [], "confidence": True}).status_code == 422

        # POST /api/quant/strategies rejects empty market/source and non-dict params/rules
        base_strat = {"strategy_id": "t5_bad", "name": "T5", "style": "value"}
        assert client.post("/api/quant/strategies", json={**base_strat, "market": ""}).status_code == 422
        assert client.post("/api/quant/strategies", json={**base_strat, "market": "   "}).status_code == 422
        assert client.post("/api/quant/strategies", json={**base_strat, "source": ""}).status_code == 422
        assert client.post("/api/quant/strategies", json={**base_strat, "params": [["a", 1]]}).status_code == 422
        assert client.post("/api/quant/strategies", json={**base_strat, "rules": [["buy", {"const": True}]]}).status_code == 422

    # Direct StrategySpec.from_dict validation parity
    with pytest.raises(ValueError):
        StrategySpec.from_dict({"strategy_id": "t5_x", "name": "T5", "style": "value", "market": ""})
    with pytest.raises(ValueError):
        StrategySpec.from_dict({"strategy_id": "t5_x", "name": "T5", "style": "value", "params": [["a", 1]]})


def test_playwright_paper_view_renders_mark_price_reject_reason_and_422_detail(
    isolated_env: dict[str, Path],
) -> None:
    dist_index = Path(__file__).resolve().parents[1] / "web" / "dist" / "index.html"
    assert dist_index.is_file(), f"Missing built frontend at {dist_index}"
    assert Path(CHROMIUM_BIN).exists(), f"Chromium missing at {CHROMIUM_BIN}"

    port = _free_port()
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=1.0) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError("Uvicorn test server failed to start")

    SCRATCH_TMP_BASE.mkdir(parents=True, exist_ok=True)
    short_tmp = SCRATCH_TMP_BASE / f"pt5_{os.getpid()}"
    short_tmp.mkdir(parents=True, exist_ok=True)

    driver_script = r"""
import json, sys
from playwright.sync_api import sync_playwright

cfg = json.loads(sys.stdin.read())
base_url = cfg["base_url"]
console_errors, page_errors, server_errors = [], [], []

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

    page.goto(base_url + "/", wait_until="networkidle", timeout=20000)
    page.locator("[data-tab='paper']").click()
    paper_sec = page.locator("#section-paper")
    paper_sec.locator("input[aria-label='下单数量']").wait_for(state="visible", timeout=15000)

    # 1. Submit CN BUY 100 -> check mark_price column ("现价", index 5) is numeric, not "—"
    paper_sec.locator("button:has-text('提交模拟订单')").click()
    paper_sec.locator(".ok-banner").wait_for(state="visible", timeout=15000)
    page.wait_for_function(
        "() => document.querySelectorAll('[data-testid=\"paper-holdings\"] tbody tr').length > 0",
        timeout=15000,
    )
    holding_cells = page.evaluate(
        "() => Array.from(document.querySelectorAll('[data-testid=\"paper-holdings\"] tbody tr td')).map(td => td.innerText.trim())"
    )

    # 2. Submit same-day CN SELL 100 -> Orders table row must show REJECTED + CN T+1 reason (NOT "已成交")
    paper_sec.locator("select[aria-label='买卖方向']").select_option("SELL")
    paper_sec.locator("button:has-text('提交模拟订单')").click()
    paper_sec.locator(".err").wait_for(state="visible", timeout=15000)
    t1_banner = paper_sec.locator(".err").inner_text().strip()
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('#section-paper table.data-table')).some(t => t.innerText.includes('REJECTED'))",
        timeout=15000,
    )
    order_rows = page.evaluate(
        "() => {"
        "  const tables = Array.from(document.querySelectorAll('#section-paper table.data-table'));"
        "  const ordTbl = tables.find(t => t.innerText.includes('说明'));"
        "  return ordTbl ? Array.from(ordTbl.querySelectorAll('tbody tr')).map(tr => Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim())) : [];"
        "}"
    )

    # 3. Trigger real FastAPI array 422 detail in UI (advance-day with short new_date) and assert formatted message (no [object Object])
    adv_input = paper_sec.locator("input[aria-label='推进交易日']")
    adv_input.evaluate("el => { el.type = 'text'; }")
    adv_input.fill("2025")
    paper_sec.locator("button:has-text('推进下一交易日')").click()
    page.wait_for_function(
        "() => { const el = document.querySelector('#section-paper .err'); return el && el.innerText.includes('new_date'); }",
        timeout=15000,
    )
    err_422_text = paper_sec.locator(".err").inner_text().strip()

    browser.close()

print(json.dumps({
    "holding_cells": holding_cells,
    "t1_banner": t1_banner,
    "order_rows": order_rows,
    "err_422_text": err_422_text,
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
        assert proc.returncode == 0, f"Playwright failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
        out = json.loads(proc.stdout)
        assert out["page_errors"] == []
        assert out["server_errors"] == []
        # Note: browser logs 422 network error on intentional 422 test; verify zero 5xx and zero page errors
        assert out["holding_cells"][0] == "000858"
        mark_price_txt = out["holding_cells"][5]
        assert mark_price_txt != "—"
        assert float(mark_price_txt.replace(",", "")) > 0

        rejected_rows = [r for r in out["order_rows"] if len(r) >= 6 and r[4] == "REJECTED"]
        assert len(rejected_rows) >= 1
        assert "T+1" in rejected_rows[0][5]
        assert rejected_rows[0][5] != "已成交"

        assert "[object Object]" not in out["err_422_text"]
        assert "new_date" in out["err_422_text"]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        shutil.rmtree(short_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Category C: Quant, Backtest & Ledger Edge Cases
# ---------------------------------------------------------------------------


def test_ledger_huge_quantity_rejection_and_int32_migration(tmp_path: Path) -> None:
    import duckdb

    # 1. Pre-existing DuckDB created with legacy INTEGER columns must migrate safely
    legacy_db = tmp_path / "legacy_ledger.duckdb"
    con = duckdb.connect(str(legacy_db))
    con.execute(
        """
        CREATE TABLE account_meta (
            account_id VARCHAR PRIMARY KEY, initial_capital DOUBLE, cash DOUBLE,
            realized_pnl DOUBLE, current_date VARCHAR, created_at VARCHAR, updated_at VARCHAR
        );
        INSERT INTO account_meta VALUES ('default', 100000.0, 100000.0, 0.0, '2026-11-26', '2026-11-26T00:00:00Z', '2026-11-26T00:00:00Z');
        CREATE TABLE position_lots (
            lot_id VARCHAR PRIMARY KEY, symbol VARCHAR, market VARCHAR, quantity INTEGER,
            remaining_qty INTEGER, buy_date VARCHAR, avg_cost DOUBLE, total_cost DOUBLE,
            strategy_id VARCHAR, created_at VARCHAR, updated_at VARCHAR
        );
        CREATE TABLE orders (
            order_id VARCHAR PRIMARY KEY, seq_no INTEGER, trade_date VARCHAR, symbol VARCHAR,
            market VARCHAR, side VARCHAR, order_type VARCHAR, requested_qty INTEGER,
            filled_qty INTEGER, limit_price DOUBLE, fill_price DOUBLE, status VARCHAR,
            reject_code VARCHAR, reject_reason VARCHAR, strategy_id VARCHAR, created_at VARCHAR
        );
        CREATE TABLE fills (
            fill_id VARCHAR PRIMARY KEY, order_id VARCHAR, seq_no INTEGER, trade_date VARCHAR,
            symbol VARCHAR, market VARCHAR, side VARCHAR, quantity INTEGER,
            reference_price DOUBLE, fill_price DOUBLE, notional DOUBLE, slippage_cost DOUBLE,
            commission DOUBLE, transfer_fee DOUBLE, stamp_duty DOUBLE, total_fee DOUBLE,
            net_cash_delta DOUBLE, realized_pnl DOUBLE, cash_after DOUBLE, created_at VARCHAR
        );
        CREATE TABLE equity_snapshots (
            snapshot_date VARCHAR PRIMARY KEY, cash DOUBLE, market_value DOUBLE,
            total_equity DOUBLE, realized_pnl DOUBLE, unrealized_pnl DOUBLE,
            positions_json VARCHAR, updated_at VARCHAR
        );
        CREATE TABLE mark_prices (
            symbol VARCHAR, market VARCHAR, price_date VARCHAR, close_price DOUBLE,
            prev_close DOUBLE, PRIMARY KEY (symbol, market)
        );
        """
    )
    con.close()

    bar_ref = {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "prev_close": 10.0}
    svc = PaperAccountService(legacy_db)
    for huge_qty in (10**12, -(10**12), 10**30):
        res = svc.submit_order("000858", "BUY", huge_qty, market="CN", reference_bar=bar_ref)
        assert res["accepted"] is False
        assert res["status"] == "REJECTED"

    # Also test bool initial_capital rejection
    with pytest.raises(ValueError):
        PaperAccountService(":memory:", initial_capital=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        svc.reset_account(initial_capital=True)  # type: ignore[arg-type]


def test_backtest_validates_rules_override_and_null_news_published_at(
    isolated_env: dict[str, Path],
) -> None:
    with db_session(isolated_env["db_path"]) as con:
        ensure_baseline_seeded(con)
        con.execute(
            """
            INSERT INTO news_events VALUES
            ('null_pub_1', '000858', 'CN', '', NULL, 'Null date news', 'Summary text', '', 'test_src', NULL, '["CN"]', ['CN'])
            """
        )
        res = run_backtest(
            "cn_daily_momentum_sentiment",
            ["000858"],
            "2025-04-24",
            "2025-05-15",
            con=con,
        )
        assert len(res.equity_curve) >= 5

        always_buy = StrategySpec(
            strategy_id="t5_always_buy",
            name="Always Buy",
            style="quant_daily",
            market="CN",
            rules={"buy": {"const": True}, "sell": {"const": False}},
        )
        for bad_override in (
            {"price_limit_rate": float("nan")},
            {"price_limit_rate": -0.5},
            {"price_limit_rate": 1.2},
            {"price_limit_rate": True},
            {"tick_size": 0.0},
            {"buy_lot": 0},
            {"sell_lot": 0},
            {"t_plus": -1},
            {"slippage_rate": float("nan")},
            {"max_participation_rate": 0.0},
            {"unknown_rule_field": 1},
        ):
            with pytest.raises(ValueError):
                run_backtest(
                    always_buy,
                    ["000858"],
                    "2025-04-24",
                    "2025-05-15",
                    rules_override={"000858": bad_override},
                    con=con,
                )


def test_strategies_rejects_mutated_unhashable_ranking_field() -> None:
    spec = StrategySpec(
        strategy_id="t5_mut_rank",
        name="Mutated Rank",
        style="quant_daily",
        market="CN",
        rules={"buy": {"const": False}, "ranking": {"field": "close", "order": "desc"}},
    )
    spec.rules["ranking"]["field"] = ["close"]  # type: ignore[index]
    with pytest.raises(ValueError):
        evaluate_strategy_signals(spec, {"close": 10.0})


# ---------------------------------------------------------------------------
# Category D: Verifier Linear Time on Single-Line Equations
# ---------------------------------------------------------------------------


def test_verifier_800_operand_single_line_linear_time() -> None:
    tc_ungrounded = [{"name": "query_stored_market_data", "args": {}, "result_summary": "close=133.67", "duration_ms": 5}]
    ans_200 = "长等式：" + " + ".join(["1.00 亿元"] * 200) + " = 200.00 亿元。来源：static_baseline"
    ans_800 = "长等式：" + " + ".join(["1.00 亿元"] * 800) + " = 800.00 亿元。来源：static_baseline"

    def _measure_median(
        ans_text: str, tc_list: list[dict[str, Any]], runs: int = 5
    ) -> tuple[dict[str, Any], float]:
        res_out = verify_answer_grounding(ans_text, tc_list, ["static_baseline"])
        samples: list[float] = []
        for _ in range(runs):
            t_start = time.perf_counter()
            res_out = verify_answer_grounding(ans_text, tc_list, ["static_baseline"])
            samples.append(time.perf_counter() - t_start)
        samples.sort()
        return res_out, samples[len(samples) // 2]

    res_200, el_200 = _measure_median(ans_200, tc_ungrounded)
    assert el_200 < 0.10, f"N=200 median took {el_200:.4f}s"
    assert res_200["grounded"] is False

    res_800, el_800 = _measure_median(ans_800, tc_ungrounded)
    assert el_800 < 0.18, f"N=800 median took {el_800:.4f}s (must be < 0.18s)"
    assert el_800 <= max(0.12, el_200 * 6.5), f"Super-linear scaling: N=200 {el_200:.4f}s vs N=800 {el_800:.4f}s"
    assert res_800["grounded"] is False

    # Distinct mixed-yi operands (N=200 vs N=800) must also scale linearly
    tc_100_yi = [
        {
            "name": "query_stored_market_data",
            "args": {},
            "result_summary": ", ".join(f"v{i}={100 + i}.{i % 10}1e8" for i in range(0, 200, 2)),
            "duration_ms": 5,
        }
    ]
    ans_mix_200 = (
        "长等式："
        + " + ".join(
            [f"{100 + i}.{i % 10}1" for i in range(0, 200, 2)]
            + [f"{500 + i}.{i % 10}7" for i in range(1, 200, 2)]
        )
        + " = 99999.99 亿元。来源：static_baseline"
    )
    tc_400_yi = [
        {
            "name": "query_stored_market_data",
            "args": {},
            "result_summary": ", ".join(f"v{i}={100 + i}.{i % 10}1e8" for i in range(0, 800, 2)),
            "duration_ms": 5,
        }
    ]
    ans_mix_800 = (
        "长等式："
        + " + ".join(
            [f"{100 + i}.{i % 10}1" for i in range(0, 800, 2)]
            + [f"{500 + i}.{i % 10}7" for i in range(1, 800, 2)]
        )
        + " = 999999.99 亿元。来源：static_baseline"
    )
    rm_200, elm_200 = _measure_median(ans_mix_200, tc_100_yi)
    assert elm_200 < 0.10, f"mixed_yi N=200 median took {elm_200:.4f}s"
    assert rm_200["grounded"] is False

    rm_800, elm_800 = _measure_median(ans_mix_800, tc_400_yi)
    assert elm_800 < 0.25, f"mixed_yi N=800 median took {elm_800:.4f}s (must be < 0.25s)"
    assert elm_800 <= max(0.18, elm_200 * 6.5), f"Super-linear mixed_yi scaling: N=200 {elm_200:.4f}s vs N=800 {elm_800:.4f}s"
    assert rm_800["grounded"] is False

    # Preserve exact explicit equation sum semantics with >= 20 operands & markdown bold
    tc_sum = [
        {
            "name": "query_stored_market_data",
            "args": {},
            "result_summary": "a=10.5, b=20.5, c=30.5, d=40.5, " + ", ".join(f"x{i}={i + 100}.25" for i in range(20)),
            "duration_ms": 5,
        }
    ]
    ok_sum = verify_answer_grounding(
        "前20项与合计：" + " + ".join(f"{i + 100}.25" for i in range(20)) + "；分项 10.5 + 20.5 + 30.5 + 40.5 = **102.0** 元。来源：static_baseline",
        tc_sum,
        ["static_baseline"],
    )
    assert ok_sum["grounded"] is True

    bad_sum = verify_answer_grounding(
        "分项 10.5 + 20.5 + 30.5 + 40.5 = **999.0** 元。来源：static_baseline",
        tc_sum,
        ["static_baseline"],
    )
    assert bad_sum["grounded"] is False
    assert "999.0" in bad_sum["ungrounded_numbers"]


def test_verifier_numbered_list_and_decimal_lines_enforce_grounding() -> None:
    tc = [{"name": "query_stored_market_data", "args": {}, "result_summary": "close=133.67", "is_error": False}]
    sources = ["static_baseline"]

    for prefix in ("1. ", "### 2. ", "2、", "1) ", "101、", "1 . "):
        ans = f"{prefix}五粮液目标价为 999999.99 元，上涨空间 888.88%"
        res = verify_answer_grounding(ans, tc, sources)
        assert res["grounded"] is False, f"Expected grounded=False for {ans!r}, got {res}"
        assert "999999.99" in res["ungrounded_numbers"]
        assert "888.88" in res["ungrounded_numbers"]
        assert "101" not in res["ungrounded_numbers"]

    dec_res = verify_answer_grounding("10.5 + 20.5 = 999.99 元", tc, sources)
    assert dec_res["grounded"] is False, dec_res
    for tok in ("10.5", "20.5", "999.99"):
        assert tok in dec_res["ungrounded_numbers"]

    enum_res = verify_answer_grounding("150、200、300元", tc, sources)
    assert enum_res["grounded"] is False, enum_res
    assert "150" in enum_res["ungrounded_numbers"]

    for good_prefix in ("1. ", "### 2. ", "2、", "1) ", "101、", "101) "):
        good_ans = f"{good_prefix}收盘133.67元"
        good_res = verify_answer_grounding(good_ans, tc, sources)
        assert good_res["grounded"] is True, f"Expected grounded=True for {good_ans!r}, got {good_res}"
        assert good_res["ungrounded_numbers"] == []

    # Direct _classify_number_grounding fallback (line_info=None) must match fast-path semantics
    line_bad = "1. 目标价 999999.99 元"
    idx_bad = line_bad.index("999999.99")
    status_bad, _ = _classify_number_grounding(
        "999999.99",
        answer=line_bad,
        match_start=idx_bad,
        match_end=idx_bad + len("999999.99"),
        evidence_strs={"133.67"},
        working_floats=[133.67],
        derived_scaled_floats=[],
    )
    assert status_bad == "ungrounded"

    line_marker = "101、收盘133.67元"
    status_marker, _ = _classify_number_grounding(
        "101",
        answer=line_marker,
        match_start=0,
        match_end=3,
        evidence_strs={"133.67"},
        working_floats=[133.67],
        derived_scaled_floats=[],
    )
    assert status_marker == "trivial"

    line_dec = "10.5 + 20.5 = 999.99"
    status_dec, _ = _classify_number_grounding(
        "10.5",
        answer=line_dec,
        match_start=0,
        match_end=4,
        evidence_strs={"133.67"},
        working_floats=[133.67],
        derived_scaled_floats=[],
    )
    assert status_dec == "ungrounded"


# ---------------------------------------------------------------------------
# Category E: Ingestion, Deduplication & Storage Bridge
# ---------------------------------------------------------------------------


def test_mcp_and_cli_deduplicate_cross_source_market_bars_for_price_limit(
    isolated_env: dict[str, Path],
) -> None:
    with db_session(isolated_env["db_path"]) as con:
        ensure_baseline_seeded(con)
        con.execute(
            """
            INSERT INTO market_bars VALUES
            ('000858', 'CN', DATE '2026-11-24', 99.0, 101.0, 98.5, 100.0, 10000, 1000000, 'akshare_tx'),
            ('000858', 'CN', DATE '2026-11-25', 110.0, 110.0, 109.0, 110.0, 12000, 1320000, 'akshare_tx'),
            ('000858', 'CN', DATE '2026-11-25', 110.0, 110.0, 109.0, 110.0, 12000, 1320000, 'akshare_sina')
            """
        )
        con.execute(
            """
            INSERT INTO sector_snapshots VALUES
            ('CN', '电力', 'BK0428', DATE '2026-11-25', 1.5, 1000, 2000, '600900', '{}', 'akshare_ths_summary'),
            ('CN', '电力', 'BK0428', DATE '2026-11-25', 1.4, 1000, 2000, '600900', '{}', 'akshare_ths_index')
            """
        )

    latest = cli._lookup_latest_bar("000858", "CN")
    assert latest is not None
    assert latest["close"] == pytest.approx(110.0)
    assert latest["prev_close"] == pytest.approx(100.0)

    PaperAccountService(isolated_env["ledger_path"], initial_date="2026-11-25")
    with pytest.raises(ToolError, match="price_limit_exceeded"):
        mcp_srv.submit_paper_order(
            symbol="000858",
            side="BUY",
            quantity=100,
            market="CN",
            trade_date="2026-11-25",
        )

    bars_q = mcp_srv.query_stored_market_data(
        table="market_bars",
        symbol="000858",
        start_date="2026-11-24",
        end_date="2026-11-25",
    )
    assert len(bars_q["rows"]) == 2
    assert bars_q["rows"][0]["source"] == "akshare_tx"

    sec_q = mcp_srv.query_stored_market_data(
        table="sector_snapshots",
        sector="电力",
        start_date="2026-11-25",
        end_date="2026-11-25",
    )
    assert len(sec_q["rows"]) == 1
    assert sec_q["rows"][0]["source"] == "akshare_ths_summary"


def test_finnhub_earnings_rejects_reversed_dates_and_logs_error_run(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = isolated_env["db_path"]

    with pytest.raises(ValueError, match="FINNHUB_API_KEY"):
        finnhub_us.ingest_finnhub_earnings("2026-11-01", "2026-11-10", db_path=db_path, api_key="")

    http_calls: list[Any] = []
    monkeypatch.setattr(finnhub_us, "_finnhub_get", lambda *a, **kw: http_calls.append(a))

    with pytest.raises(ValueError, match="from_date"):
        finnhub_us.ingest_finnhub_earnings("2026-12-01", "2026-11-01", db_path=db_path, api_key="k")
    assert http_calls == []

    with db_session(db_path) as con:
        err_rows = con.execute(
            "SELECT status, error_msg FROM ingestion_runs WHERE source = 'finnhub_earnings' ORDER BY finished_at ASC"
        ).fetchall()
    assert len(err_rows) == 2
    assert all(r[0] == "error" for r in err_rows)


def test_sec_edgar_logs_error_run_on_exception(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = isolated_env["db_path"]
    with pytest.raises(ValueError, match="empty"):
        sec_edgar.ingest_sec_edgar("", db_path=db_path)

    monkeypatch.setattr(sec_edgar, "resolve_cik", lambda *a, **kw: (_ for _ in ()).throw(ValueError("Unknown SEC ticker: BAD")))
    with pytest.raises(ValueError, match="Unknown SEC ticker"):
        sec_edgar.ingest_sec_edgar("BAD", db_path=db_path)

    with db_session(db_path) as con:
        rows = con.execute(
            "SELECT status, error_msg FROM ingestion_runs WHERE source = 'sec_edgar'"
        ).fetchall()
    assert len(rows) == 2
    assert all(r[0] == "error" for r in rows)


def test_yfinance_and_akshare_skip_empty_assets_upsert(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = isolated_env["db_path"]

    class _EmptyTicker:
        info: dict[str, Any] = {}
        balance_sheet = pd.DataFrame()
        quarterly_balance_sheet = pd.DataFrame()
        income_stmt = pd.DataFrame()
        quarterly_income_stmt = pd.DataFrame()
        news: list[Any] = []

        def history(self, **_kw: Any) -> pd.DataFrame:
            return pd.DataFrame()

    monkeypatch.setattr(yfinance_us.yf, "Ticker", lambda _sym: _EmptyTicker())
    res_us = yfinance_us.ingest_us_stock("INVALID_SYM_999999", db_path=db_path)
    assert res_us["rows_written"] == 0

    monkeypatch.setattr(akshare_cn, "_fetch_a_share_bars", lambda *_a, **_kw: ([], "", "empty bars"))
    monkeypatch.setattr(akshare_cn, "_fetch_a_share_fundamentals", lambda *_a, **_kw: ([], None, "empty fund"))
    monkeypatch.setattr(akshare_cn, "_fetch_stock_news_em", lambda *_a, **_kw: ([], "empty news"))
    res_cn = akshare_cn.ingest_a_share_stock("600999", db_path=db_path)
    assert res_cn["rows_written"] == 0

    with db_session(db_path) as con:
        cnt = con.execute(
            "SELECT COUNT(*) FROM assets WHERE symbol IN ('INVALID_SYM_999999', '600999')"
        ).fetchone()[0]
    assert cnt == 0


def test_jevdb_bridge_stratifies_static_baseline_under_dynamic_flood(
    isolated_env: dict[str, Path],
) -> None:
    db_path = isolated_env["db_path"]
    with db_session(db_path) as con:
        ensure_baseline_seeded(con)
        dyn_rows = [
            (
                f"dyn_flood_{i}",
                "000858",
                "CN",
                "消费",
                datetime(2026, 11, 25, 10, i % 60, 0),
                f"Dynamic Flood Title {i}",
                f"Dynamic Flood Summary {i}",
                "",
                "akshare_em",
                None,
                json.dumps(["CN", "000858"]),
                ["CN", "000858"],
            )
            for i in range(60)
        ]
        con.executemany(
            "INSERT INTO news_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            dyn_rows,
        )

    with mcp_srv._OfflineSemanticEvalServer() as mock_srv:
        res = hybrid_semantic_query(
            {
                "condition": "新闻是否与000858或电力相关",
                "op": "holds",
                "target": "news_events",
                "backend": "llm",
                "endpoint": mock_srv.url,
                "api_key": "offline-mock",
                "filters": {"limit": 20},
            },
            db_path=db_path,
        )
    assert len(res["rows"]) == 20
    assert "static_baseline" in res["sources"]
    static_rows = [r for r in res["rows"] if r["source"] == "static_baseline"]
    assert len(static_rows) >= 2


# ---------------------------------------------------------------------------
# Category F: Docs, Web Tools & E2E Infra
# ---------------------------------------------------------------------------


def test_design_md_lists_companion_mit_frontend_packages() -> None:
    design_path = Path(__file__).resolve().parents[2] / "docs_project" / "DESIGN.md"
    txt = design_path.read_text(encoding="utf-8")
    for pkg in ("react-dom", "@vitejs/plugin-react", "@types/react", "@types/react-dom"):
        assert pkg in txt, f"{pkg} missing from DESIGN.md"


def test_playwright_runner_handles_explicit_none_timeout(tmp_path: Path) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    html = b"<!doctype html><html><head><title>T5 Timeout None</title></head><body>OK</body></html>"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args: Any) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    try:
        runner = PlaywrightRunner(tmp_path)
        res = runner.run_steps(
            [
                {"action": "goto", "url": f"http://127.0.0.1:{srv.server_address[1]}/", "timeout": None},
                {"action": "title", "key": "title"},
            ]
        )
        assert res["results"]["title"] == "T5 Timeout None"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_web_tools_invalid_port_raises_ssrf_error() -> None:
    for bad_url in ("http://example.com:99999/", "http://example.com:abc/"):
        with pytest.raises(web_tools.SSRFError, match="Invalid URL port"):
            web_tools._validate_and_resolve_url(bad_url)


# ---------------------------------------------------------------------------
# Category G: Round 4 White-box Hardening Regressions (Findings 1–5)
# ---------------------------------------------------------------------------


def test_verifier_trailing_period_enforced_and_versions_excluded() -> None:
    tc = [{"name": "query_stored_market_data", "args": {}, "result_summary": "close=133.67", "is_error": False}]
    sources = ["static_baseline"]

    cases = [
        ("Target price is 999999.", "999999"),
        ("Target price is 123.45.", "123.45"),
        ("Target price is 9,999.", "9,999"),
        ("Target price is 999999. Next sentence.", "999999"),
        ("Cost $999.99.", "999.99"),
        ("1. Item 123.45.", "123.45"),
    ]
    for ans, expected_tok in cases:
        res = verify_answer_grounding(ans, tc, sources)
        assert res["grounded"] is False, f"Expected grounded=False for {ans!r}, got {res}"
        assert expected_tok in res["ungrounded_numbers"], f"{expected_tok!r} missing for {ans!r}: {res}"

    for ok_ans in (
        "Release v1.2.3 and protocol 1.2.3 report close 133.67.",
        "1. 收盘133.67元。",
    ):
        ok_res = verify_answer_grounding(ok_ans, tc, sources)
        assert ok_res["grounded"] is True, f"Expected grounded=True for {ok_ans!r}, got {ok_res}"
        assert ok_res["ungrounded_numbers"] == []


def test_mock_semantic_query_does_not_poison_llm_cache(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    db_path = isolated_env["db_path"]
    with db_session(db_path) as con:
        ensure_baseline_seeded(con)

    # Clear DEEPSEEK_API_KEY so backend='auto' also resolves to 'mock'
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    if hasattr(get_settings, "cache_clear"):
        get_settings.cache_clear()  # type: ignore[attr-defined]

    mock_explicit = mcp_srv.jevdb_semantic_query(
        condition="Is this bullish?",
        op="holds",
        target="news_events",
        backend="mock",
        limit=2,
    )
    assert mock_explicit["backend"] == "mock"
    assert len(mock_explicit["rows"]) == 2

    mock_auto = mcp_srv.jevdb_semantic_query(
        condition="Is this positive catalyst?",
        op="holds",
        target="news_events",
        backend="auto",
        limit=2,
    )
    assert mock_auto["backend"] == "mock"

    with db_session(db_path) as con:
        backends = {
            r[0]
            for r in con.execute("SELECT DISTINCT backend FROM semantic_decisions_cache").fetchall()
        }
    assert backends == {"mock"}, f"Expected only 'mock' cache rows, got {backends}"

    llm_calls = 0

    class _AllFalseLLMHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            nonlocal llm_calls
            llm_calls += 1
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw.decode("utf-8"))
            msgs = body.get("messages") or []
            user_msg = next((str(m.get("content") or "") for m in msgs if m.get("role") == "user"), "{}")
            items = (json.loads(user_msg) or {}).get("items") or [{}]
            rf = body.get("response_format") or {}
            items_schema = (
                (rf.get("json_schema") or {})
                .get("schema", {})
                .get("properties", {})
                .get("answers", {})
                .get("items", {})
            )
            item_type = items_schema.get("type")
            if item_type == "object":
                answers: list[Any] = [{"id": i, "verdict": False} for i in range(len(items))]
            elif item_type in ("number", "integer"):
                answers = [0.0 for _ in items]
            else:
                answers = [False for _ in items]
            payload = {
                "id": "llm-false",
                "object": "chat.completion",
                "model": body.get("model") or "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps({"answers": answers})},
                        "finish_reason": "stop",
                    }
                ],
            }
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _AllFalseLLMHandler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    try:
        llm_res = hybrid_semantic_query(
            {
                "condition": "Is this bullish?",
                "op": "holds",
                "target": "news_events",
                "backend": "llm",
                "endpoint": f"http://127.0.0.1:{srv.server_address[1]}/v1",
                "api_key": "sk-test-real-llm",
                "filters": {"limit": 2},
            },
            db_path=db_path,
        )
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2.0)

    assert llm_calls > 0, "Real LLM endpoint was bypassed by mock cache!"
    assert llm_res["cache_hits"] == 0
    assert llm_res["llm_evaluated"] == 2
    assert all(r["semantic_result"] is False for r in llm_res["rows"])


def test_verifier_markdown_table_header_linear_scaling() -> None:
    prefix = "\n".join(f"前文说明行 {i}：参考数据。" for i in range(400))
    tc = [{"name": "query_stored_market_data", "args": {}, "result_summary": "close=133.67", "duration_ms": 5}]

    def _make_table(n_rows: int) -> str:
        header = "| 序号 | 目标价(元) | 收盘价(元) |\n| --- | --- | --- |"
        rows = [f"| {i + 1} | {1000 + i}.25 | 133.67 |" for i in range(n_rows)]
        return prefix + "\n" + header + "\n" + "\n".join(rows) + "\n来源：static_baseline"

    ans_200 = _make_table(200)
    ans_800 = _make_table(800)

    def _median_time(ans_text: str, runs: int = 5) -> tuple[dict[str, Any], float]:
        res_out = verify_answer_grounding(ans_text, tc, ["static_baseline"])
        samples: list[float] = []
        for _ in range(runs):
            t0 = time.perf_counter()
            res_out = verify_answer_grounding(ans_text, tc, ["static_baseline"])
            samples.append(time.perf_counter() - t0)
        samples.sort()
        return res_out, samples[len(samples) // 2]

    r_200, el_200 = _median_time(ans_200)
    assert r_200["grounded"] is False
    assert el_200 < 0.25, f"Table N=200 median took {el_200:.4f}s"

    r_800, el_800 = _median_time(ans_800)
    assert r_800["grounded"] is False
    assert el_800 < 0.50, f"Table N=800 median took {el_800:.4f}s (must be < 0.50s)"
    assert el_800 <= max(0.25, el_200 * 5.0), f"Super-linear table scaling: N=200 {el_200:.4f}s vs N=800 {el_800:.4f}s"


def test_api_paper_order_uses_trade_date_and_normalizes_cn_symbol(
    isolated_env: dict[str, Path],
) -> None:
    db_path = isolated_env["db_path"]
    with db_session(db_path) as con:
        ensure_baseline_seeded(con)
        con.execute(
            """
            INSERT INTO market_bars VALUES
            ('000858', 'CN', DATE '2026-11-25', 500.0, 510.0, 495.0, 505.0, 10000, 5050000, 'akshare_tx'),
            ('600900', 'CN', DATE '2025-05-14', 28.0, 28.5, 27.8, 28.0, 10000, 280000, 'akshare_tx'),
            ('600900', 'CN', DATE '2025-05-15', 29.0, 29.5, 28.9, 29.2, 12000, 350400, 'akshare_tx')
            """
        )

    PaperAccountService(isolated_env["ledger_path"], initial_date="2025-05-15")
    client = TestClient(create_app())

    # Whitespace-only symbol must fail validation with 422
    r_ws = client.post(
        "/api/paper/order",
        json={"symbol": "   ", "side": "BUY", "quantity": 100, "market": "CN", "trade_date": "2025-05-15"},
    )
    assert r_ws.status_code == 422

    # Historical trade_date=2025-05-15 must use the 2025-05-15 bar (~130), not 2026-11-25 (500.0)
    r_hist = client.post(
        "/api/paper/order",
        json={"symbol": "000858", "side": "BUY", "quantity": 100, "market": "CN", "trade_date": "2025-05-15"},
    )
    assert r_hist.status_code == 200
    body_hist = r_hist.json()
    assert body_hist["accepted"] is True, body_hist
    assert body_hist["fill_price"] < 200.0

    # CN prefix sh600900 must normalize to 600900 and execute against 600900 bar
    r_cn = client.post(
        "/api/paper/order",
        json={"symbol": "sh600900", "side": "BUY", "quantity": 100, "market": "CN", "trade_date": "2025-05-15"},
    )
    assert r_cn.status_code == 200
    body_cn = r_cn.json()
    assert body_cn["accepted"] is True, body_cn
    assert body_cn["symbol"] == "600900"
    assert 28.0 <= body_cn["fill_price"] <= 30.0


def test_yfinance_parse_news_timestamp_guards_bad_epochs(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad_ts in (float("nan"), float("inf"), float("-inf"), 1e18, -10.0, "not-a-date", "NaT"):
        dt = yfinance_us._parse_news_timestamp(bad_ts)
        assert isinstance(dt, datetime)
        assert dt.tzinfo is None

    good_dt = yfinance_us._parse_news_timestamp(1700000000)
    assert good_dt == datetime(2023, 11, 14, 22, 13, 20)

    class _MixedNewsTicker:
        info = {"shortName": "Apple Inc.", "sector": "Technology"}
        balance_sheet = pd.DataFrame()
        quarterly_balance_sheet = pd.DataFrame()
        income_stmt = pd.DataFrame()
        quarterly_income_stmt = pd.DataFrame()
        news = [
            {
                "id": "bad-ts-news",
                "content": {
                    "title": "Malformed Timestamp Story",
                    "summary": "Has NaN timestamp",
                    "pubDate": float("nan"),
                    "canonicalUrl": {"url": "https://example.com/bad-ts"},
                    "provider": {"displayName": "TestFeed"},
                },
            },
            {
                "id": "good-ts-news",
                "content": {
                    "title": "Valid Timestamp Story",
                    "summary": "Should still be ingested",
                    "pubDate": "2026-11-25T10:00:00Z",
                    "canonicalUrl": {"url": "https://example.com/good-ts"},
                    "provider": {"displayName": "TestFeed"},
                },
            },
        ]

        def history(self, **_kw: Any) -> pd.DataFrame:
            return pd.DataFrame()

    monkeypatch.setattr(yfinance_us.yf, "Ticker", lambda _sym: _MixedNewsTicker())
    res = yfinance_us.ingest_us_stock("AAPL", db_path=isolated_env["db_path"])
    assert res["rows_written"] >= 2
    with db_session(isolated_env["db_path"]) as con:
        titles = {
            r[0]
            for r in con.execute(
                "SELECT title FROM news_events WHERE symbol = 'AAPL' AND source LIKE 'yfinance:%'"
            ).fetchall()
        }
    assert "Malformed Timestamp Story" in titles
    assert "Valid Timestamp Story" in titles


# ---------------------------------------------------------------------------
# Category H: Round 5 White-box Hardening Regressions (Findings 1–3)
# ---------------------------------------------------------------------------


def test_cn_ticker_normalization_parity_across_rest_mcp_cli_and_backtest(
    isolated_env: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = isolated_env["db_path"]
    with db_session(db_path) as con:
        ensure_baseline_seeded(con)
        con.execute(
            """
            INSERT INTO market_bars VALUES
            ('600900', 'CN', DATE '2025-05-14', 28.0, 28.5, 27.8, 28.0, 10000, 280000, 'akshare_tx'),
            ('600900', 'CN', DATE '2025-05-15', 29.0, 29.5, 28.9, 29.2, 12000, 350400, 'akshare_tx'),
            ('AAPL', 'US', DATE '2025-05-14', 200.0, 202.0, 199.0, 201.0, 50000, 10050000, 'yfinance'),
            ('AAPL', 'US', DATE '2025-05-15', 201.0, 204.0, 200.5, 203.0, 55000, 11165000, 'yfinance')
            """
        )
        con.execute(
            """
            INSERT INTO fundamentals VALUES
            ('600900', 'CN', DATE '2025-03-31', 18.5, 2.4, 15.2, 12000000000, 2800000000, 0.55, '{}', 'akshare_em', TIMESTAMP '2025-05-15 10:00:00')
            """
        )
        con.execute(
            """
            INSERT INTO news_events VALUES
            ('news_600900_r5', '600900', 'CN', '电力', TIMESTAMP '2025-05-15 09:30:00',
             '长江电力发电量稳步增长', '水电来水改善带动业绩增长', 'https://example.com/600900', 'akshare_em', 'positive', '["CN","600900"]', ['CN','600900'])
            """
        )

    PaperAccountService(isolated_env["ledger_path"], initial_date="2025-05-15")
    client = TestClient(create_app())

    # 1. REST parity: bars, fundamentals, news, backtest, compare, portfolio-risk
    r_bars = client.get("/api/market/bars", params={"symbol": "sh600900", "market": "CN"})
    assert r_bars.status_code == 200
    assert r_bars.json()["symbol"] == "600900"
    assert len(r_bars.json()["bars"]) == 2

    r_fund = client.get("/api/market/fundamentals", params={"symbol": "600900.SH", "market": "CN"})
    assert r_fund.status_code == 200
    assert r_fund.json()["symbol"] == "600900"
    assert len(r_fund.json()["fundamentals"]) == 1

    r_news = client.get("/api/market/news", params={"symbol": "sh600900"})
    assert r_news.status_code == 200
    assert len(r_news.json()["news"]) == 1
    assert r_news.json()["news"][0]["symbol"] == "600900"

    r_bt = client.post(
        "/api/quant/backtest",
        json={
            "strategy_id": "cn_daily_momentum_sentiment",
            "symbols": ["sh600900"],
            "start_date": "2025-05-14",
            "end_date": "2025-05-15",
            "market": "CN",
        },
    )
    assert r_bt.status_code == 200, r_bt.text
    assert r_bt.json()["symbols"] == ["600900"]

    r_cmp = client.post(
        "/api/quant/compare",
        json={
            "strategy_ids": ["cn_daily_momentum_sentiment", "value_quality_margin_of_safety"],
            "symbols": ["600900.SH"],
            "start_date": "2025-05-14",
            "end_date": "2025-05-15",
            "market": "CN",
        },
    )
    assert r_cmp.status_code == 200, r_cmp.text
    assert r_cmp.json()["symbols"] == ["600900"]

    r_risk = client.post(
        "/api/quant/portfolio-risk",
        json={"holdings": [{"symbol": "sh600900", "weight": 0.5}, {"symbol": "sz000858", "weight": 0.5}]},
    )
    assert r_risk.status_code == 200, r_risk.text
    risk_body = r_risk.json()
    assert sorted(risk_body["weights"].keys()) == ["000858", "600900"]
    assert not any("missing_or_short_price_history" in w for w in risk_body["warnings"]), risk_body

    # 2. Direct backtest module parity
    direct_bars = load_bars_from_duckdb(["sh600900"], market="CN", db_path=db_path)
    assert len(direct_bars) == 2
    assert {b["symbol"] for b in direct_bars} == {"600900"}
    direct_bt = run_backtest(
        "cn_daily_momentum_sentiment",
        ["600900.sh"],
        "2025-05-14",
        "2025-05-15",
        market="CN",
        db_path=db_path,
    )
    assert direct_bt.symbols == ["600900"]

    # 3. MCP parity
    mcp_q = mcp_srv.query_stored_market_data(table="auto", symbol="sh600900", market="CN")
    assert mcp_q["symbol"] == "600900"
    assert len(mcp_q["market_bars"]) == 2
    assert len(mcp_q["fundamentals"]) == 1
    assert len(mcp_q["news_events"]) == 1

    mcp_bt = mcp_srv.run_backtest_tool(
        strategy_id="cn_daily_momentum_sentiment",
        symbols=["sh600900"],
        start_date="2025-05-14",
        end_date="2025-05-15",
        market="CN",
    )
    assert mcp_bt["symbols"] == ["600900"]

    mcp_cmp = mcp_srv.compare_strategies_tool(
        strategy_ids=["cn_daily_momentum_sentiment", "value_quality_margin_of_safety"],
        symbols=["600900.SH"],
        start_date="2025-05-14",
        end_date="2025-05-15",
        market="CN",
    )
    assert mcp_cmp["symbols"] == ["600900"]

    mcp_risk = mcp_srv.analyze_portfolio_risk(
        holdings=[{"symbol": "sh600900", "weight": 0.5}, {"symbol": "000858.SZ", "weight": 0.5}],
    )
    assert sorted(mcp_risk["weights"].keys()) == ["000858", "600900"]
    assert not any("missing_or_short_price_history" in w for w in mcp_risk["warnings"]), mcp_risk

    # 4. CLI parity
    cli_bar = cli._lookup_latest_bar("sh600900", "CN", trade_date="2025-05-15")
    assert cli_bar is not None and cli_bar["close"] == pytest.approx(29.2)

    capsys.readouterr()
    rc_bt = cli.main(
        [
            "backtest",
            "--strategy-id",
            "cn_daily_momentum_sentiment",
            "--symbols",
            "sh600900",
            "--start-date",
            "2025-05-14",
            "--end-date",
            "2025-05-15",
            "--market",
            "CN",
        ]
    )
    assert rc_bt == 0
    cli_bt_out = json.loads(capsys.readouterr().out)
    assert cli_bt_out["symbols"] == ["600900"]

    # 5. Unified ledger position ('600900', never split '600900' vs 'SH600900') across REST + MCP + CLI
    r_order = client.post(
        "/api/paper/order",
        json={"symbol": "sh600900", "side": "BUY", "quantity": 100, "market": "CN", "trade_date": "2025-05-15"},
    )
    assert r_order.status_code == 200 and r_order.json()["accepted"] is True
    assert r_order.json()["symbol"] == "600900"

    mcp_order = mcp_srv.submit_paper_order(
        symbol="600900.SH",
        side="BUY",
        quantity=100,
        market="CN",
        trade_date="2025-05-15",
    )
    assert mcp_order["accepted"] is True
    assert mcp_order["symbol"] == "600900"

    mcp_order_ref = mcp_srv.submit_paper_order(
        symbol="sh600900",
        side="BUY",
        quantity=100,
        market="CN",
        trade_date="2025-05-15",
        reference_bar={"open": 29.0, "high": 29.5, "low": 28.9, "close": 29.2, "prev_close": 28.0},
    )
    assert mcp_order_ref["accepted"] is True
    assert mcp_order_ref["symbol"] == "600900"

    capsys.readouterr()
    rc_paper = cli.main(
        ["paper", "buy", "--symbol", "SH600900", "--quantity", "100", "--market", "CN", "--date", "2025-05-15"]
    )
    assert rc_paper == 0
    cli_paper_out = json.loads(capsys.readouterr().out)
    assert cli_paper_out["accepted"] is True
    assert cli_paper_out["symbol"] == "600900"

    acct = client.get("/api/paper/account").json()
    pos_pairs = [(p["symbol"], p["quantity"]) for p in acct["positions"]]
    assert pos_pairs == [("600900", 400)], f"Expected unified 600900 position, got {pos_pairs}"

    # 6. US alphabetic tickers untouched & invalid/empty rejection preserved
    r_us = client.get("/api/market/bars", params={"symbol": "aapl", "market": "US"})
    assert r_us.status_code == 200
    assert r_us.json()["symbol"] == "AAPL"
    assert len(r_us.json()["bars"]) == 2
    assert load_bars_from_duckdb(["sh600900"], market="US", db_path=db_path) == []

    with pytest.raises(ToolError):
        mcp_srv.submit_paper_order(symbol="   ", side="BUY", quantity=100, market="CN")
    with pytest.raises(ValueError, match="non-empty"):
        run_backtest("cn_daily_momentum_sentiment", ["   "], "2025-05-14", "2025-05-15")


def test_akshare_to_timestamp_guards_nan_nat_and_ingests_non_null_news(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for bad in (float("nan"), float("inf"), float("-inf"), pd.NaT, "nan", "NaN", "NaT", None, True, "bad-date"):
        dt = akshare_cn._to_timestamp(bad)
        assert isinstance(dt, datetime)
        assert not pd.isna(dt)
        assert dt.tzinfo is None

    assert akshare_cn._to_timestamp("2025-05-15 09:30:00") == datetime(2025, 5, 15, 9, 30, 0)
    assert akshare_cn._to_timestamp(datetime(2025, 5, 15, 9, 30, 0)) == datetime(2025, 5, 15, 9, 30, 0)

    monkeypatch.setattr(
        akshare_cn,
        "_fetch_a_share_bars",
        lambda *_a, **_kw: ([], "akshare_tx", None),
    )
    monkeypatch.setattr(
        akshare_cn,
        "_fetch_a_share_fundamentals",
        lambda *_a, **_kw: ([], None, None),
    )
    monkeypatch.setattr(
        akshare_cn.ak,
        "stock_news_em",
        lambda **_kw: pd.DataFrame(
            [
                {
                    "新闻标题": "长江电力发布分红公告",
                    "新闻内容": "分红方案稳健",
                    "发布时间": float("nan"),
                    "文章来源": "测试新闻",
                    "新闻链接": "https://example.com/news-nan",
                },
                {
                    "新闻标题": "长江电力装机容量提升",
                    "新闻内容": "水电发电量增长",
                    "发布时间": pd.NaT,
                    "文章来源": "测试新闻",
                    "新闻链接": "https://example.com/news-nat",
                },
            ]
        ),
    )
    res = akshare_cn.ingest_a_share_stock("600900", db_path=isolated_env["db_path"], force=True)
    assert res["rows_written"] >= 2
    with db_session(isolated_env["db_path"]) as con:
        rows = con.execute(
            "SELECT title, published_at FROM news_events WHERE symbol = '600900' AND source LIKE 'akshare_em:%'"
        ).fetchall()
    assert len(rows) == 2
    assert all(r[1] is not None for r in rows), rows


def test_verifier_wrapped_line_trailing_period_enforced() -> None:
    tc = [{"name": "query_stored_market_data", "args": {}, "result_summary": "close=133.67", "is_error": False}]
    sources = ["static_baseline"]

    for bad_ans in (
        "Target price is\n999999.",
        "Target price is\n999999..",
        "Target price is\n999999.   ",
    ):
        res = verify_answer_grounding(bad_ans, tc, sources)
        assert res["grounded"] is False, f"Expected grounded=False for {bad_ans!r}, got {res}"
        assert "999999" in res["ungrounded_numbers"], res

    for ok_ans in (
        "1. 收盘133.67元",
        "### 2. 收盘133.67元",
        "2、收盘133.67元",
        "1) 收盘133.67元",
        "101、收盘133.67元",
    ):
        ok_res = verify_answer_grounding(ok_ans, tc, sources)
        assert ok_res["grounded"] is True, f"Expected grounded=True for {ok_ans!r}, got {ok_res}"
        assert ok_res["ungrounded_numbers"] == []

    enum_res = verify_answer_grounding("150、200、300元", tc, sources)
    assert enum_res["grounded"] is False
    assert "150" in enum_res["ungrounded_numbers"]


# ---------------------------------------------------------------------------
# Category I: Round 7 White-box Hardening Regressions (Findings 1–3)
# ---------------------------------------------------------------------------


def test_cn_normalization_in_ledger_advance_day_overrides_and_backtest_context(
    isolated_env: dict[str, Path],
) -> None:
    svc = PaperAccountService(isolated_env["ledger_path"], initial_date="2025-05-15")
    buy_cn = svc.submit_order(
        symbol="sh600900",
        side="BUY",
        quantity=100,
        market="CN",
        trade_date="2025-05-15",
        reference_bar={"open": 29.0, "high": 29.5, "low": 28.9, "close": 29.2, "prev_close": 28.0},
    )
    assert buy_cn["accepted"] is True and buy_cn["symbol"] == "600900"

    buy_us = svc.submit_order(
        symbol="AAPL",
        side="BUY",
        quantity=2,
        market="US",
        trade_date="2025-05-15",
        reference_bar={"open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0},
    )
    assert buy_us["accepted"] is True and buy_us["symbol"] == "AAPL"

    ov1 = svc.get_account_summary(price_overrides={"sh600900": 35.0, "AAPL": 210.0})
    pos_ov1 = {p["symbol"]: p for p in ov1["positions"]}
    assert pos_ov1["600900"]["mark_price"] == pytest.approx(35.0)
    assert pos_ov1["AAPL"]["mark_price"] == pytest.approx(210.0)

    ov2 = svc.get_account_summary(price_overrides={"600900.SH": 36.0})
    pos_ov2 = {p["symbol"]: p for p in ov2["positions"]}
    assert pos_ov2["600900"]["mark_price"] == pytest.approx(36.0)

    adv1 = svc.advance_day("2025-05-16", mark_prices={"sh600900": 35.0, "AAPL": 215.0})
    pos_adv1 = {p["symbol"]: p for p in adv1["positions"]}
    assert pos_adv1["600900"]["mark_price"] == pytest.approx(35.0)
    assert pos_adv1["AAPL"]["mark_price"] == pytest.approx(215.0)

    with svc._connect(write=False) as con:
        mark_keys = sorted(
            (r[0], r[1]) for r in con.execute("SELECT symbol, market FROM mark_prices").fetchall()
        )
    assert mark_keys == [("600900", "CN"), ("AAPL", "US")], f"Orphaned mark_prices rows: {mark_keys}"

    client = TestClient(create_app())
    r_adv = client.post(
        "/api/paper/advance-day",
        json={"new_date": "2025-05-17", "mark_prices": {"600900.SH": 36.0}},
    )
    assert r_adv.status_code == 200, r_adv.text
    pos_api = {p["symbol"]: p for p in r_adv.json()["positions"]}
    assert pos_api["600900"]["mark_price"] == pytest.approx(36.0)
    assert pos_api["AAPL"]["mark_price"] == pytest.approx(215.0)

    with svc._connect(write=False) as con:
        mark_keys_after = sorted(
            (r[0], r[1]) for r in con.execute("SELECT symbol, market FROM mark_prices").fetchall()
        )
    assert mark_keys_after == [("600900", "CN"), ("AAPL", "US")], mark_keys_after

    mcp_status = mcp_srv.paper_account_status(price_overrides={"600900.SH": 37.0})
    pos_mcp = {p["symbol"]: p for p in mcp_status["positions"]}
    assert pos_mcp["600900"]["mark_price"] == pytest.approx(37.0)
    assert pos_mcp["AAPL"]["mark_price"] == pytest.approx(215.0)

    # Backtester explicit fundamentals & news with prefixed/suffixed CN symbols
    spec = StrategySpec(
        strategy_id="t5_r7_cn_fund_news",
        name="T5 R7 CN Fund+News",
        style="sentiment_hybrid",
        market="CN",
        description="Requires fundamentals + positive sentiment",
        params={"sentiment_lookback_days": 5},
        rules={
            "buy": {
                "and": [
                    {"field": "has_fundamentals", "op": "==", "value": True},
                    {"field": "pe_ttm", "op": "<=", "value": 20.0},
                    {"field": "has_news", "op": "==", "value": True},
                    {"field": "sentiment_score", "op": ">", "value": 0.0},
                ]
            },
            "sell": {"const": False},
        },
        source="user",
    )
    bars = [
        {"date": "2025-05-13", "symbol": "sh600900", "open": 28.0, "high": 28.5, "low": 27.8, "close": 28.2, "volume": 1_000_000},
        {"date": "2025-05-14", "symbol": "600900.SH", "open": 28.3, "high": 28.8, "low": 28.1, "close": 28.6, "volume": 1_100_000},
        {"date": "2025-05-15", "symbol": "600900", "open": 28.7, "high": 29.2, "low": 28.5, "close": 29.0, "volume": 1_200_000},
    ]
    res_canon = run_backtest(
        spec,
        ["600900"],
        "2025-05-13",
        "2025-05-15",
        market="CN",
        bars=bars,
        fundamentals=[{"symbol": "600900", "report_date": "2025-03-31", "pe_ttm": 15.0}],
        news=[{"symbol": "600900", "published_at": "2025-05-13", "sentiment_label": "positive"}],
    )
    res_prefixed = run_backtest(
        spec,
        ["sh600900"],
        "2025-05-13",
        "2025-05-15",
        market="CN",
        bars=bars,
        fundamentals=[{"symbol": "sh600900", "report_date": "2025-03-31", "pe_ttm": 15.0}],
        news=[{"symbol": "600900.SH", "published_at": "2025-05-13", "sentiment_label": "positive"}],
    )
    res_dict_fund = run_backtest(
        spec,
        ["600900.SH"],
        "2025-05-13",
        "2025-05-15",
        market="CN",
        bars=bars,
        fundamentals={"600900.SH": [{"report_date": "2025-03-31", "pe_ttm": 15.0}]},
        news=[{"symbol": "sh600900", "published_at": "2025-05-13", "sentiment_label": "positive"}],
    )
    assert len(res_canon.trades) > 0
    assert res_prefixed.trades == res_canon.trades
    assert res_prefixed.reproducibility_hash == res_canon.reproducibility_hash
    assert res_dict_fund.trades == res_canon.trades
    assert res_dict_fund.reproducibility_hash == res_canon.reproducibility_hash


def test_akshare_and_yfinance_to_date_guards_nat_nan_and_skips_bad_rows(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad_vals = (
        pd.NaT,
        float("nan"),
        float("inf"),
        float("-inf"),
        "nan",
        "NaN",
        "NaT",
        "none",
        "null",
        "inf",
        "-inf",
        "",
        None,
        True,
        "bad-date",
    )
    for bad in bad_vals:
        assert akshare_cn._to_date(bad) is None, f"akshare_cn._to_date({bad!r}) did not return None"
        assert yfinance_us._to_date(bad) is None, f"yfinance_us._to_date({bad!r}) did not return None"

    for good in ("2025-05-15", pd.Timestamp("2025-05-15"), datetime(2025, 5, 15, 9, 30), date(2025, 5, 15)):
        assert akshare_cn._to_date(good) == date(2025, 5, 15)
        assert yfinance_us._to_date(good) == date(2025, 5, 15)
    assert akshare_cn._to_date("20250515") == date(2025, 5, 15)

    # AkShare bar ingestion skips pd.NaT row instead of crashing DuckDB NOT NULL
    monkeypatch.setattr(
        akshare_cn.ak,
        "stock_zh_a_hist_tx",
        lambda **_kw: pd.DataFrame(
            [
                {"date": pd.NaT, "open": 28.0, "high": 28.5, "low": 27.8, "close": 28.2, "volume": 1000},
                {"date": "2025-05-15", "open": 29.0, "high": 29.5, "low": 28.9, "close": 29.2, "volume": 1200},
            ]
        ),
    )
    cn_res = akshare_cn.ingest_a_share_stock(
        "600900",
        db_path=isolated_env["db_path"],
        include_fundamentals=False,
        include_news=False,
        force=True,
    )
    assert cn_res["bars"] == 1
    with db_session(isolated_env["db_path"]) as con:
        cn_dates = con.execute(
            "SELECT trade_date FROM market_bars WHERE symbol = '600900' AND market = 'CN'"
        ).fetchall()
    assert cn_dates == [(date(2025, 5, 15),)]

    # yfinance ingestion skips pd.NaT in DatetimeIndex and statement columns cleanly
    class _NatTicker:
        info = {"shortName": "Apple Inc.", "sector": "Technology"}
        balance_sheet = pd.DataFrame(
            {
                pd.NaT: [100.0, 40.0],
                pd.Timestamp("2025-03-31"): [200.0, 80.0],
            },
            index=["Total Assets", "Total Liabilities Net Minority Interest"],
        )
        quarterly_balance_sheet = pd.DataFrame()
        financials = pd.DataFrame(
            {
                pd.NaT: [50.0, 10.0],
                pd.Timestamp("2025-03-31"): [100.0, 25.0],
            },
            index=["Total Revenue", "Net Income"],
        )
        quarterly_financials = pd.DataFrame()
        news: list[dict[str, Any]] = []

        def history(self, **_kw: Any) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "Open": [200.0, 201.0],
                    "High": [202.0, 203.0],
                    "Low": [199.0, 200.0],
                    "Close": [201.0, 202.0],
                    "Volume": [50000.0, 60000.0],
                },
                index=pd.DatetimeIndex([pd.NaT, pd.Timestamp("2025-05-15")]),
            )

    monkeypatch.setattr(yfinance_us.yf, "Ticker", lambda _sym: _NatTicker())
    us_res = yfinance_us.ingest_us_stock("AAPL", db_path=isolated_env["db_path"])
    assert us_res["bars"] == 1
    assert us_res["fundamentals"] == 1
    with db_session(isolated_env["db_path"]) as con:
        us_dates = con.execute(
            "SELECT trade_date FROM market_bars WHERE symbol = 'AAPL' AND market = 'US'"
        ).fetchall()
        fund_dates = con.execute(
            "SELECT report_date FROM fundamentals WHERE symbol = 'AAPL' AND market = 'US'"
        ).fetchall()
    assert us_dates == [(date(2025, 5, 15),)]
    assert fund_dates == [(date(2025, 3, 31),)]


def test_backtest_handles_empty_report_date_and_nonfinite_fundamentals() -> None:
    spec = StrategySpec(
        strategy_id="t5_r7_empty_repdate",
        name="T5 R7 Empty Report Date",
        style="value",
        market="CN",
        description="Handles empty report_date and malformed optional fundamental fields",
        params={},
        rules={
            "buy": {
                "and": [
                    {"field": "has_fundamentals", "op": "==", "value": True},
                    {"field": "pe_ttm", "op": "<=", "value": 20.0},
                ]
            },
            "sell": {"const": False},
        },
        source="user",
    )
    bars = [
        {"date": "2025-05-14", "symbol": "600900", "open": 28.0, "high": 28.5, "low": 27.8, "close": 28.2, "volume": 1_000_000},
        {"date": "2025-05-15", "symbol": "600900", "open": 28.3, "high": 28.9, "low": 28.1, "close": 28.6, "volume": 1_200_000},
    ]
    for rep_val in ("", "   ", pd.NaT, None):
        res = run_backtest(
            spec,
            ["600900"],
            "2025-05-14",
            "2025-05-15",
            market="CN",
            bars=bars,
            fundamentals=[
                {
                    "symbol": "600900",
                    "report_date": rep_val,
                    "pe_ttm": 15.0,
                    "pb": float("nan"),
                    "roe": "",
                    "debt_ratio": float("inf"),
                    "revenue": None,
                    "net_income": "not-a-number",
                }
            ],
        )
        assert len(res.trades) == 1
        assert res.trades[0]["status"] == "FILLED"
        assert res.trades[0]["action"] == "BUY"


# ---------------------------------------------------------------------------
# Category J: Round 8 White-box Hardening Regressions (Defects 1–4)
# ---------------------------------------------------------------------------


def test_duckdb_progress_bar_disabled_across_connections_and_clean_json_stdout(
    isolated_env: dict[str, Path],
) -> None:
    db_path = isolated_env["db_path"]
    ledger_path = isolated_env["ledger_path"]

    # 1. schema.connect_db (memory & file)
    mem_con = connect_db(":memory:")
    try:
        assert mem_con.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0] is False
    finally:
        mem_con.close()

    with db_session(db_path) as con:
        assert con.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0] is False
        ensure_baseline_seeded(con)

    # 2. PaperAccountService (memory & file)
    mem_svc = PaperAccountService(":memory:", initial_date="2025-05-15")
    try:
        with mem_svc._connect(write=False) as con:
            assert con.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0] is False
    finally:
        mem_svc.close()

    file_svc = PaperAccountService(ledger_path, initial_date="2025-05-15")
    with file_svc._connect(write=False) as con:
        assert con.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0] is False

    # 3. backtest.load_bars_from_duckdb & backtest._normalize_bars read_only connections
    import duckdb as _duckdb

    checked_settings: list[bool] = []
    orig_connect = _duckdb.connect

    class _ConnProxy:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def execute(self, *args: Any, **kwargs: Any) -> Any:
            return self._inner.execute(*args, **kwargs)

        def close(self) -> None:
            checked_settings.append(
                bool(self._inner.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0])
            )
            self._inner.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    orig_duckdb_connect = _duckdb.connect
    try:
        _duckdb.connect = lambda *a, **kw: _ConnProxy(orig_duckdb_connect(*a, **kw))  # type: ignore[assignment]
        bars = load_bars_from_duckdb(["000858"], market="CN", db_path=db_path)
        assert len(bars) >= 1
        bt_res = run_backtest("cn_daily_momentum_sentiment", ["000858"], "2025-04-24", "2025-05-15", db_path=db_path)
        assert len(bt_res.equity_curve) >= 5
    finally:
        _duckdb.connect = orig_duckdb_connect
    # 1 connection from load_bars_from_duckdb + 2 connections from run_backtest (bars + optional context)
    assert len(checked_settings) == 3
    assert all(val is False for val in checked_settings), checked_settings

    # Subprocess non-TTY stdout probe running a >2s DuckDB query (would leak progress bar if enabled)
    probe_script = r"""
import json, sys
from stock_research.storage.schema import init_db
from stock_research.quant.ledger import PaperAccountService

db_path, ledger_path = sys.argv[1], sys.argv[2]
con = init_db(db_path)
try:
    prog1 = con.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0]
    cnt1 = con.execute(
        "SELECT COUNT(*) FROM range(15000000) a, range(20) b WHERE (a.range + b.range) % 13 = 0"
    ).fetchone()[0]
finally:
    con.close()

svc = PaperAccountService(ledger_path, initial_date="2025-05-15")
with svc._connect(write=False) as lcon:
    prog2 = lcon.execute("SELECT current_setting('enable_progress_bar')").fetchone()[0]
    cnt2 = lcon.execute(
        "SELECT COUNT(*) FROM range(15000000) a, range(20) b WHERE (a.range + b.range) % 13 = 0"
    ).fetchone()[0]

print(json.dumps({"cnt1": cnt1, "prog1": prog1, "cnt2": cnt2, "prog2": prog2}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", probe_script, str(db_path), str(ledger_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"Probe failed: stderr={proc.stderr!r}, stdout={proc.stdout!r}"
    for bad_glyph in ("▕", "█", "%"):
        assert bad_glyph not in proc.stdout, f"Progress bar leaked into stdout: {proc.stdout!r}"
    parsed = json.loads(proc.stdout.strip())
    assert parsed == {"cnt1": 23076923, "prog1": False, "cnt2": 23076923, "prog2": False}


def test_ledger_advance_day_unheld_and_closed_bare_us_ticker_inference(
    isolated_env: dict[str, Path],
) -> None:
    svc = PaperAccountService(isolated_env["ledger_path"], initial_date="2025-05-15")

    # 1. Pre-seed unheld bare US and CN symbols via advance_day
    adv1 = svc.advance_day(
        "2025-05-16",
        mark_prices={"AAPL": 220.0, "000858": 130.0, "sh600900": 29.0},
    )
    assert adv1["current_date"] == "2025-05-16"

    with svc._connect(write=False) as con:
        marks = {
            (r[0], r[1]): float(r[2])
            for r in con.execute("SELECT symbol, market, close_price FROM mark_prices").fetchall()
        }
    assert ("AAPL", "US") in marks and marks[("AAPL", "US")] == pytest.approx(220.0)
    assert ("AAPL", "CN") not in marks, f"Misclassified unheld AAPL as CN: {marks}"
    assert ("000858", "CN") in marks and marks[("000858", "CN")] == pytest.approx(130.0)
    assert ("600900", "CN") in marks and marks[("600900", "CN")] == pytest.approx(29.0)

    # 2. Submit US BUY order without reference_bar -> uses ('AAPL', 'US') mark price
    buy_us = svc.submit_order("AAPL", "BUY", 2, market="US", trade_date="2025-05-16")
    assert buy_us["accepted"] is True, buy_us
    assert buy_us["market"] == "US"
    assert buy_us["reference_price"] == pytest.approx(220.0)

    # 3. Verify price_overrides for bare US & CN tickers in get_account_summary
    summary_ov = svc.get_account_summary(price_overrides={"AAPL": 225.0})
    assert summary_ov["positions"][0]["symbol"] == "AAPL"
    assert summary_ov["positions"][0]["mark_price"] == pytest.approx(225.0)

    # 4. Close AAPL completely (remaining_qty == 0), then advance_day with bare "AAPL"
    sell_us = svc.submit_order("AAPL", "SELL", 2, market="US", trade_date="2025-05-16")
    assert sell_us["accepted"] is True, sell_us
    assert svc.get_account_summary()["positions"] == []

    svc.advance_day("2025-05-17", mark_prices={"AAPL": 232.0})
    with svc._connect(write=False) as con:
        aapl_mkts = con.execute(
            "SELECT market, close_price FROM mark_prices WHERE symbol = 'AAPL'"
        ).fetchall()
    assert aapl_mkts == [("US", 232.0)], f"Unexpected AAPL mark_prices rows: {aapl_mkts}"

    rebuy_us = svc.submit_order("AAPL", "BUY", 1, market="US", trade_date="2025-05-17")
    assert rebuy_us["accepted"] is True, rebuy_us
    assert rebuy_us["reference_price"] == pytest.approx(232.0)

    # 5. REST POST /api/paper/advance-day with unheld bare US symbol -> POST /api/paper/order
    client = TestClient(create_app())
    r_adv = client.post(
        "/api/paper/advance-day",
        json={"new_date": "2025-05-18", "mark_prices": {"MSFT": 410.0}},
    )
    assert r_adv.status_code == 200, r_adv.text
    r_ord = client.post(
        "/api/paper/order",
        json={"symbol": "MSFT", "side": "BUY", "quantity": 1, "market": "US", "trade_date": "2025-05-18"},
    )
    assert r_ord.status_code == 200, r_ord.text
    assert r_ord.json()["accepted"] is True, r_ord.json()
    assert r_ord.json()["reference_price"] == pytest.approx(410.0)


def test_backtest_multisource_fundamentals_coalescing_ordering_and_market_isolation(
    isolated_env: dict[str, Path],
) -> None:
    def _run_case(insert_yfinance_first: bool) -> Any:
        db_file = isolated_env["runtime"] / f"fund_case_{'yf' if insert_yfinance_first else 'edgar'}.duckdb"
        with db_session(db_file) as con:
            con.execute(
                """
                INSERT INTO market_bars VALUES
                ('AAPL', 'US', DATE '2025-05-12', 198.0, 200.0, 197.0, 199.0, 100000, 19900000, 'yfinance'),
                ('AAPL', 'US', DATE '2025-05-13', 199.0, 201.0, 198.0, 200.0, 100000, 20000000, 'yfinance'),
                ('AAPL', 'US', DATE '2025-05-14', 200.0, 202.0, 199.0, 201.0, 100000, 20100000, 'yfinance'),
                ('AAPL', 'US', DATE '2025-05-15', 201.0, 203.0, 200.0, 202.0, 100000, 20200000, 'yfinance'),
                ('AAPL', 'US', DATE '2025-05-16', 202.0, 204.0, 201.0, 203.0, 100000, 20300000, 'yfinance')
                """
            )
            yf_row = (
                "AAPL", "US", date(2025, 3, 31), 25.0, 4.0, 0.30, 1000.0, 200.0, 0.40,
                "{}", "yfinance", datetime(2025, 5, 1, 10, 0, 0),
            )
            edgar_row = (
                "AAPL", "US", date(2025, 3, 31), None, None, 0.32, 1100.0, 220.0, 0.38,
                "{}", "sec_edgar", datetime(2025, 5, 1, 10, 5, 0),
            )
            # Colliding CN row for same symbol must be excluded when market='US'
            cn_collision = (
                "AAPL", "CN", date(2025, 3, 31), 999.0, 99.0, 0.01, 1.0, 1.0, 0.99,
                "{}", "akshare_em", datetime(2025, 5, 2, 10, 0, 0),
            )
            # Newer partial sec_edgar row on 2025-05-13 (pe_ttm=None, pb=None, roe=0.35) must preserve pe_ttm=25.0
            newer_edgar = (
                "AAPL", "US", date(2025, 5, 13), None, None, 0.35, 1200.0, 240.0, 0.36,
                "{}", "sec_edgar", datetime(2025, 5, 13, 10, 0, 0),
            )
            # Newer yfinance row on 2025-05-15 genuinely reporting high PE (45.0) must override pe_ttm
            newer_high_pe = (
                "AAPL", "US", date(2025, 5, 15), 45.0, 6.0, 0.35, 1200.0, 240.0, 0.36,
                "{}", "yfinance", datetime(2025, 5, 15, 10, 0, 0),
            )
            rows = [yf_row, edgar_row] if insert_yfinance_first else [edgar_row, yf_row]
            rows.extend([cn_collision, newer_edgar, newer_high_pe])
            con.executemany(
                "INSERT INTO fundamentals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            # CN news collision must not affect US backtest
            con.execute(
                """
                INSERT INTO news_events VALUES
                ('cn_bad_1', 'AAPL', 'CN', '', TIMESTAMP '2025-05-12 09:00:00', 'Negative CN', 'loss', '', 'test', 'negative', '[]', []),
                ('us_good_1', 'AAPL', 'US', '', TIMESTAMP '2025-05-12 09:00:00', 'Positive US', 'growth', '', 'test', 'positive', '[]', [])
                """
            )

            spec = StrategySpec(
                strategy_id="t5_r8_coalesce_val",
                name="Coalesced Value + Sentiment",
                style="value",
                market="US",
                params={"sentiment_lookback_days": 5},
                rules={
                    "buy": {
                        "and": [
                            {"field": "pe_ttm", "op": "<=", "value": 30.0},
                            {"field": "pb", "op": "<=", "value": 5.0},
                            {"field": "roe", "op": ">=", "value": 0.31},
                            {"field": "sentiment_score", "op": ">", "value": 0.0},
                        ]
                    },
                    "sell": {"field": "pe_ttm", "op": ">", "value": 40.0},
                },
                source="user",
            )
            return run_backtest(
                spec,
                ["AAPL"],
                "2025-05-12",
                "2025-05-16",
                market="US",
                con=con,
            )

    res_yf_first = _run_case(True)
    res_edgar_first = _run_case(False)
    assert res_yf_first.trades == res_edgar_first.trades
    assert res_yf_first.reproducibility_hash == res_edgar_first.reproducibility_hash

    filled = [t for t in res_yf_first.trades if t["status"] == "FILLED"]
    assert any(t["action"] == "BUY" and t["date"] == "2025-05-13" for t in filled), filled
    # On 2025-05-15 pe_ttm becomes 45.0 -> triggers SELL signal at 2025-05-15 close -> fills 2025-05-16 open
    assert any(t["action"] == "SELL" and t["date"] == "2025-05-16" for t in filled), filled

    # Undated-vs-dated override semantics preserved
    spec_undated = StrategySpec(
        strategy_id="t5_r8_undated",
        name="Undated Override",
        style="value",
        market="US",
        rules={"buy": {"field": "pe_ttm", "op": "<=", "value": 20.0}, "sell": {"field": "pe_ttm", "op": ">", "value": 30.0}},
    )
    bars_u = [
        {"date": "2025-05-12", "symbol": "AAPL", "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 100000},
        {"date": "2025-05-13", "symbol": "AAPL", "open": 201.0, "high": 203.0, "low": 200.0, "close": 202.0, "volume": 100000},
        {"date": "2025-05-14", "symbol": "AAPL", "open": 202.0, "high": 204.0, "low": 201.0, "close": 203.0, "volume": 100000},
        {"date": "2025-05-15", "symbol": "AAPL", "open": 203.0, "high": 205.0, "low": 202.0, "close": 204.0, "volume": 100000},
    ]
    res_u = run_backtest(
        spec_undated,
        ["AAPL"],
        "2025-05-12",
        "2025-05-15",
        market="US",
        bars=bars_u,
        fundamentals=[
            {"symbol": "AAPL", "report_date": None, "pe_ttm": 15.0},
            {"symbol": "AAPL", "report_date": "2025-05-13", "pe_ttm": 35.0},
        ],
    )
    u_filled = [t for t in res_u.trades if t["status"] == "FILLED"]
    assert [(t["date"], t["action"]) for t in u_filled] == [("2025-05-13", "BUY"), ("2025-05-14", "SELL")]


def test_finnhub_earnings_and_backtest_normalize_bars_guard_malformed_and_nat_dates(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = isolated_env["db_path"]

    monkeypatch.setattr(
        finnhub_us,
        "_finnhub_get",
        lambda *_a, **_kw: {
            "earningsCalendar": [
                {"symbol": "AAPL", "date": "bad-date", "epsEstimate": 1.5},
                {"symbol": "AAPL", "date": pd.NaT, "epsEstimate": 1.6},
                {"symbol": "AAPL", "date": "", "epsEstimate": 1.7},
                {"symbol": "AAPL", "date": None, "epsEstimate": 1.8},
                {"symbol": "AAPL", "date": "2025-05-15bad", "epsEstimate": 1.9},
                {"symbol": "MSFT", "date": "2025-05-15", "epsEstimate": 2.5},
            ]
        },
    )
    earn_res = finnhub_us.ingest_finnhub_earnings("2025-05-01", "2025-05-31", db_path=db_path, api_key="k")
    assert earn_res["status"] == "success"
    assert earn_res["earnings"] == 1
    with db_session(db_path) as con:
        rows = con.execute("SELECT symbol, report_date, eps_estimate FROM earnings_calendar").fetchall()
    assert rows == [("MSFT", date(2025, 5, 15), 2.5)]

    spec = StrategySpec(
        strategy_id="t5_r8_nat_bars",
        name="NaT Bars Guard",
        style="quant_daily",
        market="US",
        rules={"buy": {"const": True}, "sell": {"const": False}},
    )
    mixed_bars = [
        {"date": pd.NaT, "symbol": "AAPL", "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 100000},
        {"date": pd.NA, "symbol": "AAPL", "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 100000},
        {"date": "bad-date", "symbol": "AAPL", "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 100000},
        {"date": "", "symbol": "AAPL", "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 100000},
        {"date": "2025-05-14", "symbol": "AAPL", "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 100000},
        {"date": "2025-05-15", "symbol": "AAPL", "open": 201.0, "high": 203.0, "low": 200.0, "close": 202.0, "volume": 100000},
    ]
    res_mixed = run_backtest(spec, ["AAPL"], "2025-05-14", "2025-05-15", market="US", bars=mixed_bars)
    assert len(res_mixed.equity_curve) == 2
    assert len([t for t in res_mixed.trades if t["status"] == "FILLED"]) == 1

    dict_bars = {
        (pd.NaT, "AAPL"): {"open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 100000},
        ("2025-05-14", "AAPL"): {"open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 100000},
        ("2025-05-15", "AAPL"): {"open": 201.0, "high": 203.0, "low": 200.0, "close": 202.0, "volume": 100000},
    }
    res_dict = run_backtest(spec, ["AAPL"], "2025-05-14", "2025-05-15", market="US", bars=dict_bars)
    assert len(res_dict.equity_curve) == 2

    only_bad_bars = [
        {"date": pd.NaT, "symbol": "AAPL", "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 100000},
        {"date": "bad-date", "symbol": "AAPL", "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 100000},
    ]
    with pytest.raises(ValueError, match="No bars available"):
        run_backtest(spec, ["AAPL"], "2025-05-14", "2025-05-15", market="US", bars=only_bad_bars)


# ---------------------------------------------------------------------------
# Category K: Round 9 White-box Hardening Regressions (Defects 1–3)
# ---------------------------------------------------------------------------


def test_portfolio_risk_and_api_reject_malformed_price_and_benchmark_history(
    isolated_env: dict[str, Path],
) -> None:
    db_path = isolated_env["db_path"]
    with db_session(db_path) as con:
        ensure_baseline_seeded(con)
        con.execute(
            """
            INSERT INTO market_bars VALUES
            ('AAPL', 'US', DATE '2025-05-14', 200.0, 202.0, 199.0, 201.0, 50000, 10050000, 'yfinance'),
            ('AAPL', 'US', DATE '2025-05-15', 201.0, 204.0, 200.5, 203.0, 55000, 11165000, 'yfinance')
            """
        )

    holdings_us = [{"symbol": "AAPL", "market": "US", "weight": 1.0}]
    holdings_cn = [{"symbol": "000858", "market": "CN", "weight": 1.0}]

    # Direct compute_portfolio_risk rejections
    malformed_series: list[Any] = [
        123,
        "2025-05-01",
        b"100.0",
        {"close": 100.0},
        None,
        [{"close": 100.0}, 105.0],
        [100.0, {"close": 105.0}],
        [100.0, True],
        [100.0, "105.0"],
        [100.0, float("nan")],
        [100.0, -5.0],
        [{"close": None}, {"close": 105.0}],
        [{"close": "bad"}, {"close": 105.0}],
        [{"close": True}, {"close": 105.0}],
        [{"close": float("inf")}, {"close": 105.0}],
    ]
    for bad in malformed_series:
        with pytest.raises(ValueError):
            compute_portfolio_risk(holdings_us, {"AAPL": bad})
        if bad is not None:
            with pytest.raises(ValueError):
                compute_portfolio_risk(holdings_us, {"AAPL": [100.0, 102.0]}, benchmark_history=bad)
            # Even with empty holdings, malformed benchmark_history must raise ValueError
            with pytest.raises(ValueError):
                compute_portfolio_risk([], {}, benchmark_history=bad)

    with pytest.raises(ValueError):
        compute_portfolio_risk(holdings_us, "not-a-dict")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        compute_portfolio_risk(["bad-holding"], {"AAPL": [100.0, 102.0]})  # type: ignore[list-item]

    # MCP analyze_portfolio_risk wraps ValueError into ToolError cleanly
    with pytest.raises(ToolError):
        mcp_srv.analyze_portfolio_risk(holdings=holdings_us, price_history={"AAPL": "2025-05-01"})
    with pytest.raises(ToolError):
        mcp_srv.analyze_portfolio_risk(holdings=holdings_us, price_history={"AAPL": [{"close": 100.0}, 105.0]})

    # REST POST /api/quant/portfolio-risk returns 422 (never 500 or 200)
    client = TestClient(create_app())
    api_bad_payloads: list[dict[str, Any]] = [
        {"holdings": holdings_us, "price_history": {"AAPL": 123}},
        {"holdings": holdings_us, "price_history": {"AAPL": "2025-05-01"}},
        {"holdings": holdings_us, "price_history": {"AAPL": {"close": 100.0}}},
        {"holdings": holdings_us, "price_history": {"AAPL": [{"close": 100.0}, 105.0]}},
        {"holdings": holdings_us, "price_history": {"AAPL": [100.0, {"close": 105.0}]}},
        {"holdings": holdings_us, "price_history": {"AAPL": [100.0, True]}},
        {"holdings": holdings_us, "price_history": {"AAPL": [100.0, "105.0"]}},
        {"holdings": holdings_us, "price_history": {"AAPL": [{"date": "2025-05-01", "close": None}, {"date": "2025-05-02", "close": 102.0}]}},
        {"holdings": holdings_cn, "benchmark_history": [{"close": 100.0}, 105.0]},
        {"holdings": holdings_cn, "benchmark_history": ["bad"]},
        {"holdings": [], "benchmark_history": [{"close": 100.0}, 105.0]},
    ]
    for payload in api_bad_payloads:
        resp = client.post("/api/quant/portfolio-risk", json=payload)
        assert resp.status_code == 422, f"Expected 422 for {payload!r}, got {resp.status_code}: {resp.text}"

    # Valid dated dict price_history & benchmark_history return 200 cleanly
    ok_resp = client.post(
        "/api/quant/portfolio-risk",
        json={
            "holdings": holdings_us,
            "price_history": {
                "AAPL": [
                    {"date": "2025-05-13", "close": 199.0},
                    {"date": "2025-05-14", "price": 201.0},
                    {"date": "2025-05-15", "close": 203.0},
                ]
            },
            "benchmark_history": [
                {"date": "2025-05-13", "close": 500.0},
                {"date": "2025-05-14", "close": 502.5},
                {"date": "2025-05-15", "price": 505.0},
            ],
        },
    )
    assert ok_resp.status_code == 200, ok_resp.text
    ok_body = ok_resp.json()
    assert ok_body["portfolio_beta"] is not None
    assert math.isfinite(ok_body["portfolio_beta"])


def test_sec_edgar_retry_after_http_date_and_nonfinite_xbrl_guard(
    isolated_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = isolated_env["db_path"]

    # 1. _parse_retry_after bounds numeric, HTTP-date (past & future), NaN/Inf, and garbage
    assert sec_edgar._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 1.0) == pytest.approx(0.5)
    assert sec_edgar._parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT", 1.0) == pytest.approx(5.0)
    assert sec_edgar._parse_retry_after("2.5", 1.0) == pytest.approx(2.5)
    assert sec_edgar._parse_retry_after("0", 1.0) == pytest.approx(0.5)
    assert sec_edgar._parse_retry_after("100", 1.0) == pytest.approx(5.0)
    assert sec_edgar._parse_retry_after("nan", 1.5) == pytest.approx(1.5)
    assert sec_edgar._parse_retry_after("inf", 1.5) == pytest.approx(1.5)
    assert sec_edgar._parse_retry_after("invalid-retry-after", 2.0) == pytest.approx(2.0)

    # 2. _sec_get_json retries cleanly on 429/503 with HTTP-date and garbage Retry-After
    sleeps: list[float] = []
    monkeypatch.setattr(sec_edgar._RATE_LIMITER, "wait", lambda: None)
    monkeypatch.setattr(sec_edgar.time, "sleep", lambda s: sleeps.append(float(s)))

    class _FakeResp:
        def __init__(self, status_code: int, headers: dict[str, str], payload: Any = None) -> None:
            self.status_code = status_code
            self.headers = headers
            self._payload = payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self) -> Any:
            return self._payload

    responses = iter(
        [
            _FakeResp(429, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
            _FakeResp(503, {"Retry-After": "not-a-date"}),
            _FakeResp(200, {}, {"ok": True}),
        ]
    )

    class _FakeClient:
        def get(self, _url: str) -> _FakeResp:
            return next(responses)

    out = sec_edgar._sec_get_json(_FakeClient(), "https://data.sec.gov/test.json")  # type: ignore[arg-type]
    assert out == {"ok": True}
    assert sleeps == [pytest.approx(0.5), pytest.approx(2.0)]

    # 3. _pick_metric_by_date and ingest_sec_edgar skip NaN/Inf XBRL facts cleanly
    us_gaap = {
        "Revenues": {
            "units": {
                "USD": [
                    {"form": "10-K", "start": "2024-01-01", "end": "2024-12-31", "filed": "2025-02-01", "val": float("nan")},
                    {"form": "10-K", "start": "2024-01-01", "end": "2024-12-31", "filed": "2025-02-02", "val": float("inf")},
                    {"form": "10-K", "start": "2023-01-01", "end": "2023-12-31", "filed": "2024-02-01", "val": 1000.0},
                ]
            }
        },
        "NetIncomeLoss": {
            "units": {
                "USD": [
                    {"form": "10-K", "start": "2024-01-01", "end": "2024-12-31", "filed": "2025-02-01", "val": "NaN"},
                    {"form": "10-K", "start": "2023-01-01", "end": "2023-12-31", "filed": "2024-02-01", "val": 250.0},
                ]
            }
        },
        "Assets": {
            "units": {
                "USD": [
                    {"form": "10-K", "end": "2023-12-31", "filed": "2024-02-01", "val": 5000.0},
                ]
            }
        },
        "StockholdersEquity": {
            "units": {
                "USD": [
                    {"form": "10-K", "end": "2023-12-31", "filed": "2024-02-01", "val": 2000.0},
                ]
            }
        },
        "Liabilities": {
            "units": {
                "USD": [
                    {"form": "10-K", "end": "2023-12-31", "filed": "2024-02-01", "val": float("-inf")},
                ]
            }
        },
    }
    rev_picked = sec_edgar._pick_metric_by_date(us_gaap, ("Revenues",), is_duration=True)
    assert list(rev_picked.keys()) == [date(2023, 12, 31)]
    assert rev_picked[date(2023, 12, 31)][0] == pytest.approx(1000.0)

    class _DummySecClient:
        def __enter__(self) -> "_DummySecClient":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(sec_edgar, "_sec_client", lambda: _DummySecClient())
    monkeypatch.setattr(sec_edgar, "resolve_cik", lambda _sym, **_kw: ("0000320193", "Apple Inc."))

    def _fake_sec_json(_client: Any, url: str) -> Any:
        if "/submissions/" in url:
            return {"filings": {"recent": {}}}
        return {"facts": {"us-gaap": us_gaap}}

    monkeypatch.setattr(sec_edgar, "_sec_get_json", _fake_sec_json)
    res = sec_edgar.ingest_sec_edgar("AAPL", db_path=db_path)
    assert res["fundamentals"] == 1
    with db_session(db_path) as con:
        rows = con.execute(
            "SELECT report_date, revenue, net_income, debt_ratio, metrics_json FROM fundamentals WHERE symbol='AAPL' AND source='sec_edgar'"
        ).fetchall()
    assert len(rows) == 1
    rep_dt, rev_v, ni_v, debt_v, metrics_raw = rows[0]
    assert rep_dt == date(2023, 12, 31)
    assert rev_v == pytest.approx(1000.0)
    assert ni_v == pytest.approx(250.0)
    assert debt_v is None
    parsed_metrics = json.loads(metrics_raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    assert parsed_metrics["liabilities"] is None


def test_strict_iso_dates_nat_start_end_and_normalize_bars_edge_cases(
    isolated_env: dict[str, Path],
) -> None:
    from stock_research.quant import backtest as bt_mod

    db_path = isolated_env["db_path"]
    with db_session(db_path) as con:
        ensure_baseline_seeded(con)

    # 1. Strict full-string ISO date validation in finnhub_us._to_date & backtest._to_date/_to_date_or_none
    bad_dates: list[Any] = [
        "2025-05-15bad",
        "2025-05-15T12:00:00bad",
        "2025-05-15X12:00:00",
        "2025-05-15T",
        pd.NaT,
        pd.NA,
        None,
        True,
        12345,
        float("nan"),
        "",
    ]
    for bad in bad_dates:
        assert finnhub_us._to_date(bad) is None, f"finnhub_us._to_date({bad!r}) did not return None"
        assert bt_mod._to_date_or_none(bad) is None, f"backtest._to_date_or_none({bad!r}) did not return None"
        with pytest.raises(ValueError):
            bt_mod._to_date(bad)

    for good in (
        "2025-05-15",
        "2025-05-15T12:00:00Z",
        "2025-05-15T12:00:00+08:00",
        "2025-05-15 09:30:00",
        pd.Timestamp("2025-05-15"),
        date(2025, 5, 15),
    ):
        assert finnhub_us._to_date(good) == date(2025, 5, 15)
        assert bt_mod._to_date_or_none(good) == date(2025, 5, 15)
        assert bt_mod._to_date(good) == date(2025, 5, 15)

    spec = StrategySpec(
        strategy_id="t5_r9_edge_bars",
        name="R9 Edge Bars",
        style="quant_daily",
        market="US",
        rules={"buy": {"const": True}, "sell": {"const": False}},
    )
    bars_ok = [
        {"date": "2025-05-14", "symbol": "AAPL", "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": 100000},
        {"date": "2025-05-15", "symbol": "AAPL", "open": 201.0, "high": 203.0, "low": 200.0, "close": 202.0, "volume": 100000},
    ]

    # 2. pd.NaT start_date / end_date raises ValueError cleanly (not TypeError)
    with pytest.raises(ValueError, match="Invalid date"):
        run_backtest(spec, ["AAPL"], pd.NaT, "2025-05-15", market="US", bars=bars_ok)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Invalid date"):
        run_backtest(spec, ["AAPL"], "2025-05-14", pd.NaT, market="US", bars=bars_ok)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Invalid date"):
        run_backtest(spec, ["AAPL"], "2025-05-14bad", "2025-05-15", market="US", bars=bars_ok)

    # 3. volume=NaN / Inf / None succeeds (normalized to None, matching load_bars_from_duckdb)
    bars_nan_vol = [
        {"date": "2025-05-14", "symbol": "AAPL", "open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": float("nan")},
        {"date": "2025-05-15", "symbol": "AAPL", "open": 201.0, "high": 203.0, "low": 200.0, "close": 202.0, "volume": float("inf")},
    ]
    res_nan_vol = run_backtest(spec, ["AAPL"], "2025-05-14", "2025-05-15", market="US", bars=bars_nan_vol)
    assert len(res_nan_vol.equity_curve) == 2
    assert len([t for t in res_nan_vol.trades if t["status"] == "FILLED"]) == 1

    # Dict bars with NaN volume also succeed
    dict_bars_nan_vol = {
        ("2025-05-14", "AAPL"): {"open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0, "volume": float("nan")},
        ("2025-05-15", "AAPL"): {"open": 201.0, "high": 203.0, "low": 200.0, "close": 202.0, "volume": None},
    }
    res_dict_nan_vol = run_backtest(spec, ["AAPL"], "2025-05-14", "2025-05-15", market="US", bars=dict_bars_nan_vol)
    assert len(res_dict_nan_vol.equity_curve) == 2

    # 4. open=None / non-finite OHLC / non-dict bar rows raise ValueError cleanly
    bad_bar_inputs: list[Any] = [
        ["bad-row", *bars_ok],
        [{"date": "2025-05-14", "symbol": "AAPL", "open": None, "high": 202.0, "low": 199.0, "close": 201.0}, bars_ok[1]],
        [{"date": "2025-05-14", "symbol": "AAPL", "open": 200.0, "high": float("nan"), "low": 199.0, "close": 201.0}, bars_ok[1]],
        [{"date": "2025-05-14", "symbol": "AAPL", "open": True, "high": 202.0, "low": 199.0, "close": 201.0}, bars_ok[1]],
        {("2025-05-14", "AAPL"): {"open": None, "high": 202.0, "low": 199.0, "close": 201.0}},
    ]
    for bad_bars in bad_bar_inputs:
        with pytest.raises(ValueError):
            run_backtest(spec, ["AAPL"], "2025-05-14", "2025-05-15", market="US", bars=bad_bars)

    # 5. REST POST /api/quant/backtest & /api/quant/compare return 422 on trailing-suffix dates
    client = TestClient(create_app())
    for bad_dt in ("2025-04-24bad", "2025-04-24T12:00:00bad"):
        r_bt = client.post(
            "/api/quant/backtest",
            json={
                "strategy_id": "cn_daily_momentum_sentiment",
                "symbols": ["000858"],
                "start_date": bad_dt,
                "end_date": "2025-05-15",
                "market": "CN",
            },
        )
        assert r_bt.status_code == 422, f"Expected 422 for start_date={bad_dt!r}, got {r_bt.status_code}: {r_bt.text}"

        r_cmp = client.post(
            "/api/quant/compare",
            json={
                "strategy_ids": ["cn_daily_momentum_sentiment", "value_quality_margin_of_safety"],
                "symbols": ["000858"],
                "start_date": "2025-04-24",
                "end_date": bad_dt,
                "market": "CN",
            },
        )
        assert r_cmp.status_code == 422, f"Expected 422 for end_date={bad_dt!r}, got {r_cmp.status_code}: {r_cmp.text}"





