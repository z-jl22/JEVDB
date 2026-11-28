"""Tier 4: Full Real-World End-to-End Application Scenarios (5 Scenarios)."""

from __future__ import annotations

from datetime import date, timedelta
import importlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from stock_research.tests.e2e.conftest import REPO_ROOT
from stock_research.tests.e2e.test_tier1_features import (
    _get_risk_mod,
    _http_json,
    _new_ledger,
    _next_day,
    _prepare_bars_for_quant,
    _start_web_backend,
    _stop_web_backend,
    _to_dict,
)

pytestmark = pytest.mark.tier4


@pytest.mark.playwright
@pytest.mark.live_network
def test_T4_S1_power_sector_last_3_days_cli_and_web(e2e_mode: str, isolated_env: dict[str, Path], playwright_runner):
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
        cli_data = json.loads(proc.stdout)
    else:
        from stock_research.tests.e2e import contract_harness as ch
        cli_data = ch.contract_run_research_query(prompt, isolated_env["db_path"], isolated_env["ledger_db_path"], isolated_env["strategy_dir"])

    assert len(cli_data["tool_calls"]) >= 2
    assert "电力" in cli_data["answer"]
    assert re.search(r"\d", cli_data["answer"]) is not None
    assert cli_data["verification"]
    assert len(cli_data["sources"]) >= 1

    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        res = playwright_runner.run_steps([
            {"action": "goto", "url": f"{base_url}/"},
            {"action": "click_if_present", "selector": "[data-tab='agent']"},
            {"action": "fill", "selector": "[data-testid='agent-input']", "value": prompt},
            {"action": "click", "selector": "[data-testid='agent-submit']"},
            {"action": "click_if_present", "selector": ".trace-disclosure > summary"},
            {"action": "wait_for_nonempty_text", "selector": "[data-testid='tool-calls-panel']", "exclude": ["等待调用工具...", "正在执行工具链..."], "timeout": 120000},
            {"action": "wait_for_nonempty_text", "selector": "[data-testid='agent-answer']", "exclude": ["等待回答...", "提交研究问题后，结论和证据会显示在这里。", "正在核验结果..."], "timeout": 300000},
            {"action": "inspect_selectors", "selectors": ["[data-testid='tool-calls-panel']", "[data-testid='agent-answer']"], "key": "s1"},
        ])
        assert res["console_errors"] == [] and res["page_errors"] == [] and res["server_errors"] == []
        s1 = res["results"]["s1"]
        assert len(s1["[data-testid='tool-calls-panel']"]["text"]) > 0
        assert "电力" in s1["[data-testid='agent-answer']"]["text"]
    finally:
        _stop_web_backend(handle)


@pytest.mark.live_network
def test_T4_S2_value_investing_research_backtest_and_risk(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    prompt = "请从长期价值投资角度分析 000858（五粮液）的估值、盈利能力与安全边际，并运行价值策略回测和风险评估"

    if e2e_mode == "live":
        ak = importlib.import_module("stock_research.storage.ingest.akshare_cn")
        ak.ingest_a_share_stock("000858", days=30)
        proc = subprocess.run(
            [sys.executable, "-m", "stock_research.cli", "ask", prompt, "--json"],
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        cli_data = json.loads(proc.stdout)
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        bt1 = _to_dict(bt_mod.run_backtest("value_quality_margin_of_safety", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN"))
        bt2 = _to_dict(bt_mod.run_backtest("value_quality_margin_of_safety", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN"))
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ingest_a_share_stock("000858", days=30, db_path=isolated_env["db_path"])
        cli_data = ch.contract_run_research_query(prompt, isolated_env["db_path"], isolated_env["ledger_db_path"], isolated_env["strategy_dir"])
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        bt1 = ch.run_backtest("value_quality_margin_of_safety", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])
        bt2 = ch.run_backtest("value_quality_margin_of_safety", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])
        con = ch.init_db(isolated_env["db_path"])

    try:
        p0 = [r[0] for r in con.execute("SELECT close FROM market_bars WHERE symbol='000858' ORDER BY trade_date").fetchall()]
        p1 = [r[0] for r in con.execute("SELECT close FROM market_bars WHERE symbol='601318' ORDER BY trade_date").fetchall()]
    finally:
        con.close()

    assert len(cli_data["tool_calls"]) >= 1
    assert any(k in cli_data["answer"] for k in ("PE", "PB", "ROE", "安全边际", "估值", "营业收入"))
    assert len(cli_data["sources"]) >= 1
    assert bt1["reproducibility_hash"] == bt2["reproducibility_hash"]
    assert len(bt1["equity_curve"]) > 0
    for k in ("cagr", "sharpe_ratio", "max_drawdown", "var_95", "cvar_95"):
        assert math.isfinite(bt1["metrics"][k])

    risk_mod = _get_risk_mod(e2e_mode)
    pr = _to_dict(risk_mod.compute_portfolio_risk(
        [{"symbol": "000858", "weight": 0.5, "sector": "消费"}, {"symbol": "601318", "weight": 0.5, "sector": "金融"}],
        {"000858": p0, "601318": p1},
    ))
    assert pr["hhi"] == pytest.approx(0.5, abs=0.02)


def test_T4_S3_daily_cn_quant_strategy_compare_and_multiday_t1_paper(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    valid_rules = {
        "buy": {"field": "momentum_return", "op": ">", "value": 0.0},
        "sell": {"field": "momentum_return", "op": "<", "value": 0.0},
    }

    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        spec = st_mod.StrategySpec(
            strategy_id="agent_cn_daily_quant_e2e",
            name="Agent CN Daily Quant E2E",
            style="quant_daily",
            market="CN",
            description="E2E daily quant strategy",
            params={"position_weight": 0.35},
            rules=valid_rules,
            source="agent",
        )
        st_mod.save_strategy(spec)
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        cmp_res = _to_dict(bt_mod.compare_strategies(["agent_cn_daily_quant_e2e", "cn_daily_momentum_sentiment", "value_quality_margin_of_safety"], ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, "CN"))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        spec = ch.StrategySpec(
            strategy_id="agent_cn_daily_quant_e2e",
            name="Agent CN Daily Quant E2E",
            style="quant_daily",
            market="CN",
            description="E2E daily quant strategy",
            params={"position_weight": 0.35},
            rules=valid_rules,
            source="agent",
        )
        ch.save_strategy(spec, isolated_env["strategy_dir"])
        cmp_res = ch.compare_strategies(["agent_cn_daily_quant_e2e", "cn_daily_momentum_sentiment", "value_quality_margin_of_safety"], ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])

    assert len(cmp_res["results"]) == 3

    # 3-day paper trading simulation with process restart persistence
    dbp = isolated_env["ledger_db_path"]
    svc1 = _new_ledger(e2e_mode, dbp)
    svc1.reset_account(100000.0)
    d1 = svc1.get_account_summary()["current_date"]
    d2 = _next_day(d1, 1)
    d3 = _next_day(d1, 2)

    bar_wly_d1 = {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}
    bar_pa_d1 = {"open": 50.42, "high": 51.34, "low": 50.03, "close": 51.21, "prev_close": 51.33}
    b1 = svc1.submit_order("000858", "BUY", 100, market="CN", trade_date=d1, reference_bar=bar_wly_d1, strategy_id="agent_cn_daily_quant_e2e")
    b2 = svc1.submit_order("601318", "BUY", 200, market="CN", trade_date=d1, reference_bar=bar_pa_d1, strategy_id="agent_cn_daily_quant_e2e")
    t1_rej = svc1.submit_order("000858", "SELL", 100, market="CN", trade_date=d1, reference_bar=bar_wly_d1)
    assert b1["accepted"] is True and b2["accepted"] is True
    assert t1_rej["accepted"] is False

    adv2 = svc1.advance_day(d2, {"000858": 129.55, "601318": 51.00})
    assert all(p["sellable_qty"] == p["quantity"] for p in adv2["positions"])

    bar_wly_d2 = {"open": 130.15, "high": 130.60, "low": 129.29, "close": 129.55, "prev_close": 130.85}
    s1 = svc1.submit_order("000858", "SELL", 100, market="CN", trade_date=d2, reference_bar=bar_wly_d2, strategy_id="agent_cn_daily_quant_e2e")
    assert s1["accepted"] is True
    assert s1["stamp_duty"] > 0 and s1["commission"] >= 5.0 and s1["transfer_fee"] > 0
    del svc1

    # Simulated process restart
    svc2 = _new_ledger(e2e_mode, dbp)
    adv3 = svc2.advance_day(d3, {"601318": 50.71})
    pos_map = {p["symbol"]: p["quantity"] for p in adv3["positions"]}
    assert pos_map.get("000858", 0) == 0
    assert pos_map["601318"] == 200
    assert len(svc2.get_equity_history()) >= 3

    buy_wly_cost = 100 * b1["fill_price"] + b1["commission"] + b1["transfer_fee"]
    sell_wly_net = 100 * s1["fill_price"] - s1["commission"] - s1["transfer_fee"] - s1["stamp_duty"]
    buy_pa_cost = 200 * b2["fill_price"] + b2["commission"] + b2["transfer_fee"]
    expected_cash = 100000.0 - buy_wly_cost - buy_pa_cost + sell_wly_net
    assert adv3["cash"] == pytest.approx(expected_cash, abs=0.10)
    assert adv3["realized_pnl"] == pytest.approx(sell_wly_net - buy_wly_cost, abs=0.10)


@pytest.mark.live_network
def test_T4_S4_us_cross_source_research_and_portfolio_risk(e2e_mode: str, isolated_env: dict[str, Path]):
    prompt = "请结合 yfinance 行情、SEC EDGAR 财报与 Finnhub 新闻/财报日历交叉核验 AAPL 的财务状况与近期风险"
    earn_from = (date.today() - timedelta(days=120)).isoformat()
    earn_to = (date.today() + timedelta(days=120)).isoformat()
    if e2e_mode == "live":
        yf_mod = importlib.import_module("stock_research.storage.ingest.yfinance_us")
        sec_mod = importlib.import_module("stock_research.storage.ingest.sec_edgar")
        fh_mod = importlib.import_module("stock_research.storage.ingest.finnhub_us")
        for sym in ("AAPL", "MSFT"):
            yf_mod.ingest_us_stock(sym, period="1mo")
            sec_mod.ingest_sec_edgar(sym)
            fh_mod.ingest_finnhub_news(symbol=sym, days=7)
        fh_mod.ingest_finnhub_earnings(earn_from, earn_to, symbol="AAPL")
        proc = subprocess.run(
            [sys.executable, "-m", "stock_research.cli", "ask", prompt, "--json"],
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        cli_data = json.loads(proc.stdout)
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        for sym in ("AAPL", "MSFT"):
            ch.ingest_us_stock(sym, period="1mo", db_path=isolated_env["db_path"])
            ch.ingest_sec_edgar(sym, db_path=isolated_env["db_path"])
            ch.ingest_finnhub_news(symbol=sym, days=7, db_path=isolated_env["db_path"])
        ch.ingest_finnhub_earnings(earn_from, earn_to, symbol="AAPL", db_path=isolated_env["db_path"])
        cli_data = ch.contract_run_research_query(prompt, isolated_env["db_path"], isolated_env["ledger_db_path"], isolated_env["strategy_dir"])
        con = ch.init_db(isolated_env["db_path"])

    try:
        for tbl in ("market_bars", "edgar_filings", "fundamentals", "news_events", "earnings_calendar"):
            assert con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] >= 1
        p_aapl = [r[0] for r in con.execute("SELECT close FROM market_bars WHERE symbol='AAPL' ORDER BY trade_date").fetchall()]
        p_msft = [r[0] for r in con.execute("SELECT close FROM market_bars WHERE symbol='MSFT' ORDER BY trade_date").fetchall()]
    finally:
        con.close()

    assert len(cli_data["sources"]) >= 2
    assert "AAPL" in cli_data["answer"]

    risk_mod = _get_risk_mod(e2e_mode)
    pr = _to_dict(risk_mod.compute_portfolio_risk(
        [{"symbol": "AAPL", "weight": 0.5, "sector": "Tech"}, {"symbol": "MSFT", "weight": 0.5, "sector": "Tech"}],
        {"AAPL": p_aapl, "MSFT": p_msft},
    ))
    assert pr["hhi"] == pytest.approx(0.50, abs=0.02)
    assert pr["correlation_matrix"]["AAPL"]["AAPL"] == pytest.approx(1.0, abs=1e-6)
    assert pr["correlation_matrix"]["MSFT"]["MSFT"] == pytest.approx(1.0, abs=1e-6)
    assert pr["correlation_matrix"]["AAPL"]["MSFT"] == pytest.approx(pr["correlation_matrix"]["MSFT"]["AAPL"], abs=1e-6)
    assert math.isfinite(pr["var_95"]) and math.isfinite(pr["cvar_95"])
    assert pr["portfolio_volatility"] >= 0.0


@pytest.mark.playwright
@pytest.mark.live_network
def test_T4_S5_playwright_full_4_view_web_e2e(e2e_mode: str, isolated_env: dict[str, Path], playwright_runner):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        # Seed paper order via API before inspecting persistence across page reload
        _, reset_res = _http_json("POST", f"{base_url}/api/paper/reset", {"initial_capital": 100000.0})
        _, acc0 = _http_json("GET", f"{base_url}/api/paper/account")
        d1 = acc0.get("current_date") or reset_res.get("current_date")
        _http_json("POST", f"{base_url}/api/paper/order", {
            "symbol": "000858",
            "side": "BUY",
            "quantity": 100,
            "market": "CN",
            "trade_date": d1,
            "reference_bar": {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05},
        })

        prompt = "我想知道最近3天a股电力行业的情况"
        res = playwright_runner.run_steps([
            {"action": "goto", "url": f"{base_url}/"},
            # View 1: Agent Workspace
            {"action": "click_if_present", "selector": "[data-tab='agent']"},
            {"action": "fill", "selector": "[data-testid='agent-input']", "value": prompt},
            {"action": "click", "selector": "[data-testid='agent-submit']"},
            {"action": "click_if_present", "selector": ".trace-disclosure > summary"},
            {"action": "wait_for_nonempty_text", "selector": "[data-testid='tool-calls-panel']", "exclude": ["等待调用工具...", "正在执行工具链..."], "timeout": 120000},
            {"action": "wait_for_nonempty_text", "selector": "[data-testid='agent-answer']", "exclude": ["等待回答...", "提交研究问题后，结论和证据会显示在这里。", "正在核验结果..."], "timeout": 300000},
            {"action": "inspect_selectors", "selectors": ["[data-testid='agent-input']", "[data-testid='agent-submit']", "[data-testid='tool-calls-panel']", "[data-testid='agent-answer']"], "key": "v1"},
            # View 2: Market & Fundamentals
            {"action": "click_if_present", "selector": "[data-tab='market']"},
            {"action": "wait_for_selector", "selector": "[data-testid='market-chart']"},
            {"action": "inspect_selectors", "selectors": ["[data-testid='market-chart']"], "key": "v2"},
            # View 3: Quant Backtest & Risk
            {"action": "click_if_present", "selector": "[data-tab='quant']"},
            {"action": "wait_for_selector", "selector": "[data-testid='backtest-chart']"},
            {"action": "inspect_selectors", "selectors": ["[data-testid='backtest-chart']"], "key": "v3"},
            # View 4: Paper Trading & PnL + Reload persistence
            {"action": "click_if_present", "selector": "[data-tab='paper']"},
            {"action": "wait_for_nonempty_text", "selector": "[data-testid='paper-holdings']", "exclude": ["加载持仓中..."]},
            {"action": "reload"},
            {"action": "click_if_present", "selector": "[data-tab='paper']"},
            {"action": "wait_for_nonempty_text", "selector": "[data-testid='paper-holdings']", "exclude": ["加载持仓中..."]},
            {"action": "inspect_selectors", "selectors": ["[data-testid='paper-holdings']", "[data-testid='paper-pnl-chart']"], "key": "v4"},
        ])

        assert res["console_errors"] == []
        assert res["page_errors"] == []
        assert res["server_errors"] == []

        v1 = res["results"]["v1"]
        assert v1["[data-testid='agent-input']"]["visible"] is True
        assert v1["[data-testid='agent-submit']"]["visible"] is True
        assert len(v1["[data-testid='tool-calls-panel']"]["text"]) > 0
        assert "电力" in v1["[data-testid='agent-answer']"]["text"]

        v2 = res["results"]["v2"]
        assert v2["[data-testid='market-chart']"]["visible"] is True
        assert v2["[data-testid='market-chart']"]["chart"]["width"] > 0
        assert v2["[data-testid='market-chart']"]["chart"]["height"] > 0

        v3 = res["results"]["v3"]
        assert v3["[data-testid='backtest-chart']"]["visible"] is True
        assert v3["[data-testid='backtest-chart']"]["chart"]["width"] > 0
        assert v3["[data-testid='backtest-chart']"]["chart"]["height"] > 0

        v4 = res["results"]["v4"]
        assert v4["[data-testid='paper-holdings']"]["visible"] is True
        assert "000858" in v4["[data-testid='paper-holdings']"]["text"]
        assert v4["[data-testid='paper-pnl-chart']"]["visible"] is True
        assert v4["[data-testid='paper-pnl-chart']"]["chart"]["width"] > 0
        assert v4["[data-testid='paper-pnl-chart']"]["chart"]["height"] > 0
    finally:
        _stop_web_backend(handle)
