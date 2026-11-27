"""Tier 3: Cross-Feature Pairwise Integration E2E Tests (14 tests)."""

from __future__ import annotations

from datetime import date, timedelta
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from stock_research.tests.e2e.conftest import REPO_ROOT
from stock_research.tests.e2e.test_tier1_features import (
    _get_risk_mod,
    _http_json,
    _mcp_cmd,
    _mcp_stdio_session,
    _new_ledger,
    _next_day,
    _prepare_bars_for_quant,
    _start_web_backend,
    _stop_web_backend,
    _to_dict,
)

pytestmark = pytest.mark.tier3


@pytest.mark.live_network
def test_T3_01_cn_ingest_to_jevdb_hybrid_semantic_query(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        ak = importlib.import_module("stock_research.storage.ingest.akshare_cn")
        ak.ingest_a_share_stock("000858", days=10)
        ak.ingest_a_share_sector("电力", days=5)
        bridge = importlib.import_module("stock_research.storage.jevdb_bridge")
        res = bridge.hybrid_semantic_query({"filters": {"sector": "电力"}, "condition": "Positive industry growth"})
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ingest_a_share_stock("000858", days=10, db_path=isolated_env["db_path"])
        ch.ingest_a_share_sector("电力", days=5, db_path=isolated_env["db_path"])
        res = ch.hybrid_semantic_query({"filters": {"sector": "电力"}, "condition": "Positive industry growth"}, db_path=isolated_env["db_path"])

    assert len(res["rows"]) >= 1
    assert res["stats"]
    assert res["sources"]


@pytest.mark.live_network
def test_T3_02_static_and_dynamic_to_mcp_stdio_query(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    cmd = _mcp_cmd(e2e_mode, isolated_env)
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t3", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "fetch_market_data", "arguments": {"sector": "电力", "days": 5, "market": "CN"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "jevdb_semantic_query", "arguments": {"condition": "Positive catalyst", "limit": 100}}},
    ]
    resps = _mcp_stdio_session(cmd, reqs, os.environ.copy())
    assert resps[1]["result"].get("isError", False) is False
    sem = json.loads(resps[2]["result"]["content"][0]["text"])
    assert len(sem["rows"]) >= 2
    assert "static_baseline" in sem["sources"]
    assert len(sem["sources"]) >= 2


@pytest.mark.live_network
def test_T3_03_mcp_plus_web_tools_to_pi_cli_ask(e2e_mode: str, isolated_env: dict[str, Path]):
    prompt = "我想知道最近3天a股电力行业的情况"
    if e2e_mode == "live":
        proc = subprocess.run(
            [sys.executable, "-m", "stock_research.cli", "ask", prompt, "--json"],
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
        v_mod = importlib.import_module("stock_research.agent.verifier")
        assert getattr(v_mod, "verify_answer_grounding", None) is not None
        assert data["verification"].get("grounded", data["verification"].get("passed")) is True
    else:
        from stock_research.tests.e2e import contract_harness as ch
        data = ch.contract_run_research_query(prompt, isolated_env["db_path"], isolated_env["ledger_db_path"], isolated_env["strategy_dir"])
        assert data["verification"]["grounded"] is True

    names = [t["name"] for t in data["tool_calls"]]
    assert len(names) >= 2
    assert any("web" in n for n in names)
    assert any("market" in n or "jevdb" in n for n in names)


@pytest.mark.live_network
def test_T3_04_us_three_source_ingest_to_hybrid_query(e2e_mode: str, isolated_env: dict[str, Path]):
    earn_from = (date.today() - timedelta(days=120)).isoformat()
    earn_to = (date.today() + timedelta(days=120)).isoformat()
    if e2e_mode == "live":
        yf_mod = importlib.import_module("stock_research.storage.ingest.yfinance_us")
        sec_mod = importlib.import_module("stock_research.storage.ingest.sec_edgar")
        fh_mod = importlib.import_module("stock_research.storage.ingest.finnhub_us")
        yf_mod.ingest_us_stock("AAPL", period="1mo")
        sec_mod.ingest_sec_edgar("AAPL")
        fh_mod.ingest_finnhub_news(symbol="AAPL", days=7)
        fh_mod.ingest_finnhub_earnings(earn_from, earn_to, symbol="AAPL")
        bridge = importlib.import_module("stock_research.storage.jevdb_bridge")
        sem = bridge.hybrid_semantic_query({"filters": {"symbol": "AAPL"}, "condition": "Earnings and revenue strength"})
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ingest_us_stock("AAPL", period="1mo", db_path=isolated_env["db_path"])
        ch.ingest_sec_edgar("AAPL", db_path=isolated_env["db_path"])
        ch.ingest_finnhub_news(symbol="AAPL", days=7, db_path=isolated_env["db_path"])
        ch.ingest_finnhub_earnings(earn_from, earn_to, symbol="AAPL", db_path=isolated_env["db_path"])
        sem = ch.hybrid_semantic_query({"filters": {"symbol": "AAPL"}, "condition": "Earnings and revenue strength"}, db_path=isolated_env["db_path"])
        con = ch.init_db(isolated_env["db_path"])

    try:
        for tbl in ("market_bars", "edgar_filings", "fundamentals", "news_events", "earnings_calendar"):
            cnt = con.execute(f"SELECT COUNT(*) FROM {tbl} WHERE symbol='AAPL'").fetchone()[0]
            assert cnt >= 1, f"Table {tbl} missing AAPL rows"
    finally:
        con.close()
    assert len(sem["rows"]) >= 1


def test_T3_05_strategy_save_to_backtest_to_risk_metrics(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    risk_mod = _get_risk_mod(e2e_mode)
    valid_rules = {
        "buy": {"field": "momentum_return", "op": ">", "value": 0.0},
        "sell": {"field": "momentum_return", "op": "<", "value": 0.0},
    }
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        spec = st_mod.StrategySpec(
            strategy_id="t3_custom_strat",
            name="T3 Custom",
            style="quant_daily",
            market="CN",
            description="T3 chain",
            params={"position_weight": 0.4},
            rules=valid_rules,
            source="agent",
        )
        st_mod.save_strategy(spec)
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        bt = _to_dict(bt_mod.run_backtest("t3_custom_strat", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN"))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        spec = ch.StrategySpec(
            strategy_id="t3_custom_strat",
            name="T3 Custom",
            style="quant_daily",
            market="CN",
            description="T3 chain",
            params={"position_weight": 0.4},
            rules=valid_rules,
            source="agent",
        )
        ch.save_strategy(spec, isolated_env["strategy_dir"])
        bt = ch.run_backtest("t3_custom_strat", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])

    ind = _to_dict(risk_mod.compute_risk_metrics(bt["equity_curve"], bt["trades"]))
    for key in ("sharpe_ratio", "max_drawdown", "var_95", "cvar_95"):
        assert bt["metrics"][key] == pytest.approx(ind[key], abs=1e-6)


def test_T3_06_compare_strategies_to_portfolio_risk(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    risk_mod = _get_risk_mod(e2e_mode)
    sids = ["value_quality_margin_of_safety", "cn_daily_momentum_sentiment"]
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        cmp_res = _to_dict(bt_mod.compare_strategies(sids, ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, "CN"))
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        cmp_res = ch.compare_strategies(sids, ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])
        con = ch.init_db(isolated_env["db_path"])

    try:
        p0 = [r[0] for r in con.execute("SELECT close FROM market_bars WHERE symbol='000858' ORDER BY trade_date").fetchall()]
        p1 = [r[0] for r in con.execute("SELECT close FROM market_bars WHERE symbol='601318' ORDER BY trade_date").fetchall()]
    finally:
        con.close()

    assert len(cmp_res["results"]) == 2
    pr = _to_dict(risk_mod.compute_portfolio_risk(
        [{"symbol": "000858", "weight": 0.5}, {"symbol": "601318", "weight": 0.5}],
        {"000858": p0, "601318": p1},
    ))
    assert pr["hhi"] == pytest.approx(0.5, abs=0.02)
    assert set(pr["correlation_matrix"].keys()) == {"000858", "601318"}


def test_T3_07_backtest_signal_to_paper_order_and_persistence(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        bt = _to_dict(bt_mod.run_backtest("cn_daily_momentum_sentiment", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN"))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        bt = ch.run_backtest("cn_daily_momentum_sentiment", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])

    first_buy = next(t for t in bt["trades"] if t["side"] == "BUY" and t.get("status", "FILLED") == "FILLED")
    sym = first_buy["symbol"]
    bar = {"open": first_buy["price"], "high": first_buy["price"] + 1.0, "low": first_buy["price"] - 1.0, "close": first_buy["price"], "prev_close": first_buy["price"]}

    svc1 = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc1.reset_account(100000.0)
    d1 = svc1.get_account_summary()["current_date"]
    d2 = _next_day(d1, 1)
    b_res = svc1.submit_order(sym, "BUY", 100, market="CN", trade_date=d1, reference_bar=bar, strategy_id="cn_daily_momentum_sentiment")
    assert b_res["accepted"] is True
    # T+1 block on same day
    assert svc1.submit_order(sym, "SELL", 100, market="CN", trade_date=d1, reference_bar=bar)["accepted"] is False
    del svc1

    svc2 = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc2.advance_day(d2, {sym: first_buy["price"]})
    s_res = svc2.submit_order(sym, "SELL", 100, market="CN", trade_date=d2, reference_bar=bar, strategy_id="cn_daily_momentum_sentiment")
    assert s_res["accepted"] is True and s_res["stamp_duty"] > 0
    orders = svc2.get_orders()
    assert any(o.get("strategy_id") == "cn_daily_momentum_sentiment" for o in orders)


def test_T3_08_mcp_quant_and_paper_tools_chain(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    cmd = _mcp_cmd(e2e_mode, isolated_env)
    valid_rules = {
        "buy": {"field": "momentum_return", "op": ">", "value": 0.0},
        "sell": {"field": "momentum_return", "op": "<", "value": 0.0},
    }
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t3", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "save_quant_strategy", "arguments": {"strategy_id": "mcp_chain_strat", "name": "MCP Chain", "style": "quant_daily", "market": "CN", "description": "MCP chain", "params": {"position_weight": 0.3}, "rules": valid_rules, "source": "agent"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "run_backtest_tool", "arguments": {"strategy_id": "mcp_chain_strat", "symbols": ["000858", "601318"], "start_date": "2025-04-24", "end_date": "2025-05-15"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "compare_strategies_tool", "arguments": {"strategy_ids": ["value_quality_margin_of_safety", "mcp_chain_strat"], "symbols": ["000858", "601318"], "start_date": "2025-04-24", "end_date": "2025-05-15"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "analyze_portfolio_risk", "arguments": {"holdings": [{"symbol": "000858", "weight": 0.5}, {"symbol": "601318", "weight": 0.5}], "price_history": {"000858": [129.0, 130.0, 131.0], "601318": [50.0, 50.5, 51.0]}}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "submit_paper_order", "arguments": {"symbol": "000858", "side": "BUY", "quantity": 100, "market": "CN", "reference_bar": {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "paper_account_status", "arguments": {}}},
    ]
    resps = _mcp_stdio_session(cmd, reqs, os.environ.copy())
    assert len(resps) == 7
    for r in resps[1:]:
        assert r["result"].get("isError", False) is False
    status = json.loads(resps[-1]["result"]["content"][0]["text"])
    assert status["positions"][0]["symbol"] == "000858"
    assert status["positions"][0]["quantity"] == 100


def test_T3_09_multi_market_cn_us_ledger_and_portfolio_risk(e2e_mode: str, isolated_env: dict[str, Path]):
    svc = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc.reset_account(100000.0)
    d1 = svc.get_account_summary()["current_date"]
    cn_bar = {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}
    us_bar = {"open": 210.0, "high": 212.0, "low": 208.0, "close": 211.0}
    assert svc.submit_order("000858", "BUY", 100, market="CN", trade_date=d1, reference_bar=cn_bar)["accepted"] is True
    assert svc.submit_order("AAPL", "BUY", 5, market="US", trade_date=d1, reference_bar=us_bar)["accepted"] is True
    # Same-day US sell succeeds, CN sell blocked
    assert svc.submit_order("AAPL", "SELL", 5, market="US", trade_date=d1, reference_bar=us_bar)["accepted"] is True
    assert svc.submit_order("000858", "SELL", 100, market="CN", trade_date=d1, reference_bar=cn_bar)["accepted"] is False

    risk_mod = _get_risk_mod(e2e_mode)
    pr = _to_dict(risk_mod.compute_portfolio_risk(
        [{"symbol": "000858", "weight": 0.6}, {"symbol": "AAPL", "weight": 0.4}],
        {"000858": [129.0, 130.0, 130.85, 131.5], "AAPL": [208.0, 210.0, 211.0, 212.5]},
    ))
    assert pr["hhi"] == pytest.approx(0.52, abs=0.02)


@pytest.mark.playwright
@pytest.mark.live_network
def test_T3_10_api_ingest_to_market_query_to_ui_chart(e2e_mode: str, isolated_env: dict[str, Path], playwright_runner):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        st, _ = _http_json("POST", f"{base_url}/api/market/ingest", {"kind": "a_share_stock", "symbol": "000858", "days": 10}, timeout=90)
        assert st == 200
        _, bars = _http_json("GET", f"{base_url}/api/market/bars?symbol=000858&market=CN")
        assert bars
        res = playwright_runner.run_steps([
            {"action": "goto", "url": f"{base_url}/"},
            {"action": "click_if_present", "selector": "[data-tab='market']"},
            {"action": "wait_for_selector", "selector": "[data-testid='market-chart']"},
            {"action": "inspect_selectors", "selectors": ["[data-testid='market-chart']"], "key": "mkt"},
        ])
        assert res["console_errors"] == [] and res["page_errors"] == []
        mc = res["results"]["mkt"]["[data-testid='market-chart']"]
        assert mc["visible"] is True and mc["chart"]["width"] > 0 and mc["chart"]["height"] > 0
    finally:
        _stop_web_backend(handle)


@pytest.mark.playwright
@pytest.mark.live_network
def test_T3_11_ui_agent_submit_to_sse_trace_to_answer(e2e_mode: str, isolated_env: dict[str, Path], playwright_runner):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        prompt = "我想知道最近3天a股电力行业的情况"
        res = playwright_runner.run_steps([
            {"action": "goto", "url": f"{base_url}/"},
            {"action": "click_if_present", "selector": "[data-tab='agent']"},
            {"action": "fill", "selector": "[data-testid='agent-input']", "value": prompt},
            {"action": "click", "selector": "[data-testid='agent-submit']"},
            {"action": "wait_for_nonempty_text", "selector": "[data-testid='tool-calls-panel']", "exclude": ["等待调用工具...", "正在执行工具链..."], "timeout": 120000},
            {"action": "wait_for_nonempty_text", "selector": "[data-testid='agent-answer']", "exclude": ["等待回答...", "正在核验结果..."], "timeout": 240000},
            {"action": "inspect_selectors", "selectors": ["[data-testid='tool-calls-panel']", "[data-testid='agent-answer']"], "key": "agent"},
        ])
        assert res["console_errors"] == [] and res["page_errors"] == [] and res["server_errors"] == []
        assert len(res["results"]["agent"]["[data-testid='tool-calls-panel']"]["text"]) > 0
        assert len(res["results"]["agent"]["[data-testid='agent-answer']"]["text"]) > 0
    finally:
        _stop_web_backend(handle)


@pytest.mark.playwright
def test_T3_12_api_strategy_compare_to_ui_backtest_chart(e2e_mode: str, isolated_env: dict[str, Path], playwright_runner):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        _http_json("POST", f"{base_url}/api/quant/compare", {"strategy_ids": ["value_quality_margin_of_safety", "cn_daily_momentum_sentiment"], "symbols": ["000858", "601318"], "start_date": "2025-04-24", "end_date": "2025-05-15"})
        res = playwright_runner.run_steps([
            {"action": "goto", "url": f"{base_url}/"},
            {"action": "click_if_present", "selector": "[data-tab='quant']"},
            {"action": "wait_for_selector", "selector": "[data-testid='backtest-chart']"},
            {"action": "inspect_selectors", "selectors": ["[data-testid='backtest-chart']"], "key": "q"},
        ])
        assert res["console_errors"] == [] and res["page_errors"] == []
        bc = res["results"]["q"]["[data-testid='backtest-chart']"]
        assert bc["visible"] is True and bc["chart"]["width"] > 0 and bc["chart"]["height"] > 0
    finally:
        _stop_web_backend(handle)


@pytest.mark.playwright
def test_T3_13_api_paper_order_to_ui_holdings_and_pnl_persist(e2e_mode: str, isolated_env: dict[str, Path], playwright_runner):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        _, reset_res = _http_json("POST", f"{base_url}/api/paper/reset", {"initial_capital": 100000.0})
        _, acc0 = _http_json("GET", f"{base_url}/api/paper/account")
        d1 = acc0.get("current_date") or reset_res.get("current_date")
        d2 = _next_day(d1, 1)
        _http_json("POST", f"{base_url}/api/paper/order", {"symbol": "000858", "side": "BUY", "quantity": 100, "market": "CN", "trade_date": d1, "reference_bar": {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}})
        _http_json("POST", f"{base_url}/api/paper/advance-day", {"new_date": d2, "mark_prices": {"000858": 132.00}})
        res = playwright_runner.run_steps([
            {"action": "goto", "url": f"{base_url}/"},
            {"action": "click_if_present", "selector": "[data-tab='paper']"},
            {"action": "wait_for_nonempty_text", "selector": "[data-testid='paper-holdings']", "exclude": ["加载持仓中..."]},
            {"action": "reload"},
            {"action": "click_if_present", "selector": "[data-tab='paper']"},
            {"action": "wait_for_nonempty_text", "selector": "[data-testid='paper-holdings']", "exclude": ["加载持仓中..."]},
            {"action": "inspect_selectors", "selectors": ["[data-testid='paper-holdings']", "[data-testid='paper-pnl-chart']"], "key": "paper"},
        ])
        assert res["console_errors"] == [] and res["page_errors"] == []
        ph = res["results"]["paper"]["[data-testid='paper-holdings']"]
        pc = res["results"]["paper"]["[data-testid='paper-pnl-chart']"]
        assert "000858" in ph["text"]
        assert pc["visible"] is True and pc["chart"]["width"] > 0 and pc["chart"]["height"] > 0
    finally:
        _stop_web_backend(handle)


@pytest.mark.live_network
def test_T3_14_scheduler_trigger_to_api_news_endpoint(e2e_mode: str, isolated_env: dict[str, Path]):
    jobs = [{"type": "a_share_sector", "kwargs": {"sector": "电力", "days": 3}}]
    if e2e_mode == "live":
        sched_mod = importlib.import_module("stock_research.storage.ingest.scheduler")
        sched = sched_mod.SchedulerService(db_path=isolated_env["db_path"])
        sched.trigger_once(jobs=jobs)
    else:
        from stock_research.tests.e2e import contract_harness as ch
        sched = ch.ContractSchedulerService(isolated_env["db_path"])
        sched.trigger_once(jobs=jobs)

    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        st, body = _http_json("GET", f"{base_url}/api/market/news?sector=%E7%94%B5%E5%8A%9B")
        assert st == 200
        news_list = body.get("news", body) if isinstance(body, dict) else body
        assert len(news_list) >= 1
    finally:
        _stop_web_backend(handle)
