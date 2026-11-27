"""Unit, integration, and multi-process persistence tests for M2 (F7-F10)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import jevdb

from stock_research.config import BASELINE_DATA_PATH
from stock_research.quant import (
    BUILTIN_STRATEGIES,
    BacktestResult,
    PaperAccountService,
    StrategyNotFoundError,
    StrategySpec,
    compare_strategies,
    compute_portfolio_risk,
    compute_risk_metrics,
    delete_strategy,
    ensure_builtin_strategies,
    evaluate_rule,
    evaluate_strategy_signals,
    get_strategy,
    list_strategies,
    load_bars_from_duckdb,
    run_backtest,
    save_strategy,
    validate_rules,
)
from stock_research.quant.backtest import _align_comparison_curves

REPO_ROOT = Path(__file__).resolve().parents[2]
PRICES_CSV = REPO_ROOT / BASELINE_DATA_PATH / "prices.csv"


def _assert_json_safe(testcase: unittest.TestCase, obj: object) -> None:
    dumped = json.dumps(obj, allow_nan=False)
    testcase.assertIsInstance(dumped, str)


class TestStrategyRegistry(unittest.TestCase):
    def test_builtin_strategies_populated_and_filterable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            specs = list_strategies(strategy_dir=td)
            ids = {s.strategy_id for s in specs}
            self.assertGreaterEqual(len(specs), 3)
            self.assertTrue(
                {
                    "value_quality_margin_of_safety",
                    "cn_daily_momentum_sentiment",
                    "cn_mean_reversion_daily",
                }.issubset(ids)
            )
            value_specs = list_strategies(strategy_dir=td, style="value")
            self.assertEqual([s.strategy_id for s in value_specs], ["value_quality_margin_of_safety"])
            us_specs = list_strategies(strategy_dir=td, market="US")
            self.assertEqual(us_specs, [])

    def test_save_reload_delete_and_duckdb_sync(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            con = jevdb.connect(":memory:")
            try:
                ensure_builtin_strategies(strategy_dir=td, con=con)
                custom = StrategySpec(
                    strategy_id="agent_momentum_alpha",
                    name="Agent Momentum Alpha",
                    style="quant_daily",
                    market="CN",
                    description="Agent-proposed test strategy",
                    params={"min_mom": 0.01, "max_weight": 0.2},
                    rules={
                        "buy": {
                            "all": [
                                {"field": "momentum_return", "op": ">=", "param": "min_mom"},
                                {"field": "sma_short", "op": "cross_above", "ref": "sma_long"},
                            ]
                        },
                        "sell": {"field": "rsi", "op": ">=", "value": 75.0},
                        "ranking": {"field": "momentum_return", "order": "desc"},
                    },
                    source="agent",
                )
                saved = save_strategy(custom, strategy_dir=td, con=con)
                loaded = get_strategy("agent_momentum_alpha", strategy_dir=td)
                self.assertEqual(loaded.strategy_id, saved.strategy_id)
                self.assertEqual(loaded.source, "agent")

                # Verify persisted across a separate Python subprocess
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import sys; from stock_research.quant import get_strategy; "
                            "s = get_strategy('agent_momentum_alpha', strategy_dir=sys.argv[1]); "
                            "print(s.name)"
                        ),
                        td,
                    ],
                    cwd=str(REPO_ROOT),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(proc.stdout.strip(), "Agent Momentum Alpha")

                # Verify DuckDB table sync
                row = con.execute(
                    "SELECT strategy_id, source FROM quant_strategies WHERE strategy_id = ?",
                    ["agent_momentum_alpha"],
                ).fetchone()
                self.assertEqual(row, ("agent_momentum_alpha", "agent"))

                # Built-in protected from deletion by default
                with self.assertRaises(ValueError):
                    delete_strategy("value_quality_margin_of_safety", strategy_dir=td)
                self.assertTrue(delete_strategy("agent_momentum_alpha", strategy_dir=td, con=con))
                with self.assertRaises(StrategyNotFoundError):
                    get_strategy("agent_momentum_alpha", strategy_dir=td, con=con)
            finally:
                con.close()

    def test_rule_validation_rejects_unsafe_inputs_and_evaluates_safely(self) -> None:
        with self.assertRaises(ValueError):
            validate_rules({"buy": "__import__('os').system('echo pwned')"})
        with self.assertRaises(ValueError):
            validate_rules({"buy": {"field": "close", "op": "eval", "value": 10}})
        with self.assertRaises(ValueError):
            validate_rules({"unknown_section": {"const": True}})
        with self.assertRaises(ValueError):
            StrategySpec(strategy_id="../escape", name="Bad", style="value")

        # Deep AST rejection
        deep: dict[str, object] = {"const": True}
        for _ in range(20):
            deep = {"not": deep}
        with self.assertRaises(ValueError):
            validate_rules({"buy": deep})

        # Safe evaluation of combinators, cross_above, between, in, and None/NaN
        feats = {"sma_short": 10.5, "sma_long": 10.2, "rsi": 45.0, "pe_ttm": None, "nan_val": float("nan")}
        prev_feats = {"sma_short": 9.8, "sma_long": 10.0}
        self.assertTrue(
            evaluate_rule(
                {
                    "all": [
                        {"field": "sma_short", "op": "cross_above", "ref": "sma_long"},
                        {"field": "rsi", "op": "between", "value": [30.0, 60.0]},
                        {"field": "pe_ttm", "op": "==", "value": None},
                    ]
                },
                feats,
                prev_features=prev_feats,
            )
        )
        self.assertFalse(evaluate_rule({"field": "nan_val", "op": ">", "value": 0.0}, feats))
        self.assertFalse(evaluate_rule({"field": "pe_ttm", "op": "<", "value": 20.0}, feats))

    def test_validation_rejects_set_nan_inf_and_invalid_execution_params(self) -> None:
        # set in 'in' rule rejected at validation time with ValueError (not TypeError in json.dumps)
        with self.assertRaises(ValueError):
            validate_rules({"buy": {"field": "sector", "op": "in", "value": {"Power", "Tech"}}})
        with self.assertRaises(ValueError):
            StrategySpec(
                strategy_id="bad_set_rule",
                name="Bad Set",
                style="quant_daily",
                rules={"buy": {"field": "sector", "op": "in", "value": {"Power"}}},
            )

        # NaN / Inf in rule literals or StrategySpec.params rejected
        for bad_float in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                validate_rules({"buy": {"field": "close", "op": ">", "value": bad_float}})
            with self.assertRaises(ValueError):
                StrategySpec(
                    strategy_id="bad_nan_param",
                    name="Bad NaN",
                    style="quant_daily",
                    params={"threshold": bad_float},
                    rules={"buy": {"const": True}},
                )

        # Invalid stop_loss_pct / take_profit_pct / min_hold_days rejected at construction
        for bad_params in (
            {"stop_loss_pct": "not_a_number"},
            {"stop_loss_pct": True},
            {"stop_loss_pct": -0.05},
            {"take_profit_pct": "bad"},
            {"take_profit_pct": -0.1},
            {"min_hold_days": -1},
            {"min_hold_days": 1.5},
            {"min_hold_days": True},
            {"min_hold_days": "3"},
        ):
            with self.assertRaises(ValueError):
                StrategySpec(
                    strategy_id="bad_exec_param",
                    name="Bad Exec",
                    style="quant_daily",
                    params=bad_params,
                    rules={"buy": {"const": True}, "sell": {"const": True}},
                )

        # Post-construction mutation of spec.params also caught in evaluate_strategy_signals
        mut_spec = StrategySpec(
            strategy_id="mut_exec_param",
            name="Mut Exec",
            style="quant_daily",
            params={"stop_loss_pct": 0.05, "min_hold_days": 1},
            rules={"buy": {"const": True}, "sell": {"const": True}},
        )
        mut_spec.params["stop_loss_pct"] = "bad"
        with self.assertRaises(ValueError):
            evaluate_strategy_signals(
                mut_spec,
                {"unrealized_return": -0.1},
                has_position=True,
                hold_days=2,
            )
        mut_spec.params["stop_loss_pct"] = 0.05
        mut_spec.params["min_hold_days"] = True
        with self.assertRaises(ValueError):
            evaluate_strategy_signals(
                mut_spec,
                {"unrealized_return": 0.0},
                has_position=True,
                hold_days=2,
            )

    def test_concurrent_multithread_save_strategy_produces_valid_strict_json(self) -> None:
        def _reject_const(c: str) -> object:
            raise ValueError(f"Non-standard JSON constant {c!r}")

        with tempfile.TemporaryDirectory() as td:
            def _worker(idx: int) -> StrategySpec:
                spec = StrategySpec(
                    strategy_id="concurrent_shared_strat",
                    name=f"Concurrent Strategy {idx}",
                    style="quant_daily",
                    market="CN",
                    params={"max_weight": 0.2, "worker": idx},
                    rules={"buy": {"field": "close", "op": ">", "value": 10.0}},
                )
                return save_strategy(spec, strategy_dir=td)

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(_worker, range(16)))
            self.assertEqual(len(results), 16)
            saved_file = Path(td) / "concurrent_shared_strat.json"
            self.assertTrue(saved_file.exists())
            parsed = json.loads(saved_file.read_text(encoding="utf-8"), parse_constant=_reject_const)
            self.assertEqual(parsed["strategy_id"], "concurrent_shared_strat")
            loaded = get_strategy("concurrent_shared_strat", strategy_dir=td)
            self.assertEqual(loaded.strategy_id, "concurrent_shared_strat")


class TestBacktestDeterminismAndComparison(unittest.TestCase):
    def test_prices_csv_determinism_and_three_strategy_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ids = list(BUILTIN_STRATEGIES.keys())
            comp1 = compare_strategies(
                ids,
                ["000858", "601318"],
                "2025-04-24",
                "2025-05-15",
                initial_capital=100000.0,
                market="CN",
                bars=PRICES_CSV,
                strategy_dir=td,
            )
            comp2 = compare_strategies(
                ids,
                ["000858", "601318"],
                "2025-04-24",
                "2025-05-15",
                initial_capital=100000.0,
                market="CN",
                bars=PRICES_CSV,
                strategy_dir=td,
            )
            self.assertEqual(comp1.reproducibility_hash, comp2.reproducibility_hash)
            self.assertEqual(len(comp1.ranking), 3)
            self.assertEqual(len(comp1.aligned_equity_curves), 13)
            self.assertEqual(len(comp1.aligned_drawdown_curves), 13)
            self.assertEqual(len(comp1.benchmark_curve), 13)

            for sid in ids:
                res = comp1.results[sid]
                self.assertEqual(res.reproducibility_hash, comp2.results[sid].reproducibility_hash)
                filled = [t for t in res.trades if t["status"] == "FILLED"]
                self.assertGreaterEqual(len(filled), 2, f"{sid} should trigger >=2 filled trades on prices.csv")
            _assert_json_safe(self, comp1.to_dict())

    def test_causal_execution_no_lookahead_and_cn_vs_us_rules(self) -> None:
        bars = [
            {"date": "2026-01-02", "symbol": "000001", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 10_000_000},
            {"date": "2026-01-05", "symbol": "000001", "open": 10.3, "high": 10.6, "low": 10.1, "close": 10.5, "volume": 10_000_000},
            {"date": "2026-01-06", "symbol": "000001", "open": 10.5, "high": 10.9, "low": 10.4, "close": 10.8, "volume": 10_000_000},
            {"date": "2026-01-07", "symbol": "000001", "open": 10.8, "high": 11.2, "low": 10.7, "close": 11.0, "volume": 10_000_000},
        ]
        altered_future_bars = [
            *bars[:-1],
            {"date": "2026-01-07", "symbol": "000001", "open": 10.8, "high": 11.5, "low": 9.0, "close": 9.1, "volume": 10_000_000},
        ]
        spec = StrategySpec(
            strategy_id="causal_test",
            name="Causal Test",
            style="quant_daily",
            market="CN",
            params={"max_weight": 0.5},
            rules={"buy": {"field": "close", "op": ">", "value": 10.0}},
        )
        res_a = run_backtest(spec, ["000001"], "2026-01-02", "2026-01-06", bars=bars)
        res_b = run_backtest(spec, ["000001"], "2026-01-02", "2026-01-06", bars=altered_future_bars)
        self.assertEqual(res_a.reproducibility_hash, res_b.reproducibility_hash)

        # CN price limit blocks limit-up open
        limit_up_bars = [
            {"date": "2026-01-02", "symbol": "000001", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 10_000_000},
            {"date": "2026-01-05", "symbol": "000001", "open": 11.11, "high": 11.11, "low": 10.9, "close": 11.11, "volume": 10_000_000},
        ]
        cn_lim = run_backtest(spec, ["000001"], "2026-01-02", "2026-01-05", market="CN", bars=limit_up_bars)
        self.assertEqual(cn_lim.trades[0]["status"], "SKIPPED")
        self.assertEqual(cn_lim.trades[0]["reason"], "price_limit_exceeded")

        # US mode has no price limit and 1-share buy lot
        us_spec = StrategySpec(
            strategy_id="us_test",
            name="US Test",
            style="quant_daily",
            market="US",
            params={"max_weight": 0.5},
            rules={"buy": {"field": "close", "op": ">", "value": 9.0}},
        )
        us_res = run_backtest(
            us_spec,
            ["AAPL"],
            "2026-01-02",
            "2026-01-05",
            initial_capital=1000.0,
            market="US",
            bars=[
                {"date": "2026-01-02", "symbol": "AAPL", "open": 200.0, "high": 205.0, "low": 199.0, "close": 202.0, "volume": 1_000_000},
                {"date": "2026-01-05", "symbol": "AAPL", "open": 230.0, "high": 235.0, "low": 229.0, "close": 232.0, "volume": 1_000_000},
            ],
        )
        filled_us = [t for t in us_res.trades if t["status"] == "FILLED"]
        self.assertEqual(len(filled_us), 1)
        self.assertEqual(filled_us[0]["quantity"], 2)  # 1-share lot, <100 shares, no 10% limit block

    def test_duckdb_market_bars_fundamentals_and_news_integration(self) -> None:
        con = jevdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE market_bars (
                    symbol VARCHAR, market VARCHAR, trade_date VARCHAR,
                    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                    volume BIGINT, amount DOUBLE, source VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE fundamentals (
                    symbol VARCHAR, market VARCHAR, report_date VARCHAR,
                    pe_ttm DOUBLE, pb DOUBLE, roe DOUBLE,
                    revenue DOUBLE, net_income DOUBLE, debt_ratio DOUBLE
                )
                """
            )
            con.execute(
                """
                CREATE TABLE news_events (
                    event_id VARCHAR, symbol VARCHAR, market VARCHAR,
                    published_at VARCHAR, title VARCHAR, summary VARCHAR, sentiment_label VARCHAR
                )
                """
            )
            bars_data = [
                ("600900", "CN", "2026-03-02", 25.0, 25.4, 24.8, 25.2, 5_000_000, 125_000_000.0, "test"),
                ("600900", "CN", "2026-03-03", 25.2, 25.6, 25.0, 25.1, 5_000_000, 125_000_000.0, "test"),
                ("600900", "CN", "2026-03-04", 25.1, 25.5, 24.9, 25.0, 5_000_000, 125_000_000.0, "test"),
                ("600900", "CN", "2026-03-05", 25.0, 25.7, 24.9, 25.6, 6_000_000, 150_000_000.0, "test"),
                ("600900", "CN", "2026-03-06", 25.6, 26.2, 25.5, 26.0, 6_500_000, 165_000_000.0, "test"),
            ]
            con.executemany("INSERT INTO market_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", bars_data)
            con.execute(
                "INSERT INTO fundamentals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["600900", "CN", "2026-02-28", 16.5, 2.1, 0.14, 8e10, 2.5e10, 0.55],
            )
            con.execute(
                "INSERT INTO news_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                ["n1", "600900", "CN", "2026-03-02T09:00:00", "Strong hydro output", "Earnings beat", "positive"],
            )

            with tempfile.TemporaryDirectory() as td:
                res_val = run_backtest(
                    "value_quality_margin_of_safety",
                    ["600900"],
                    "2026-03-02",
                    "2026-03-06",
                    market="CN",
                    con=con,
                    strategy_dir=td,
                )
                res_mom = run_backtest(
                    "cn_daily_momentum_sentiment",
                    ["600900"],
                    "2026-03-02",
                    "2026-03-06",
                    market="CN",
                    con=con,
                    strategy_dir=td,
                )
                self.assertTrue(any(t["status"] == "FILLED" for t in res_val.trades))
                self.assertTrue(any(t["status"] == "FILLED" for t in res_mom.trades))
                _assert_json_safe(self, res_val.to_dict())
        finally:
            con.close()

    def test_max_weight_zero_blocks_buys_and_invalid_max_weight_raises(self) -> None:
        bars = [
            {"date": "2026-01-02", "symbol": "000001", "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2, "volume": 10_000_000},
            {"date": "2026-01-05", "symbol": "000001", "open": 10.3, "high": 10.6, "low": 10.1, "close": 10.5, "volume": 10_000_000},
            {"date": "2026-01-06", "symbol": "000001", "open": 10.5, "high": 10.9, "low": 10.4, "close": 10.8, "volume": 10_000_000},
        ]
        zero_spec = StrategySpec(
            strategy_id="zero_max_weight",
            name="Zero Max Weight",
            style="quant_daily",
            market="CN",
            params={"max_weight": 0.0},
            rules={"buy": {"const": True}},
        )
        res = run_backtest(zero_spec, ["000001"], "2026-01-02", "2026-01-06", initial_capital=100000.0, bars=bars)
        filled = [t for t in res.trades if t["status"] == "FILLED"]
        self.assertEqual(len(filled), 0)
        self.assertEqual(res.final_equity, 100000.0)

        for bad_mw in (-0.1, True, "bad"):
            bad_spec = StrategySpec(
                strategy_id="bad_mw",
                name="Bad MW",
                style="quant_daily",
                market="CN",
                params={"max_weight": bad_mw},
                rules={"buy": {"const": True}},
            )
            with self.assertRaises(ValueError):
                run_backtest(bad_spec, ["000001"], "2026-01-02", "2026-01-06", bars=bars)

        nan_mw_spec = StrategySpec(
            strategy_id="nan_mw",
            name="NaN MW",
            style="quant_daily",
            market="CN",
            params={"max_weight": 0.2},
            rules={"buy": {"const": True}},
        )
        nan_mw_spec.params["max_weight"] = float("nan")
        with self.assertRaises(ValueError):
            run_backtest(nan_mw_spec, ["000001"], "2026-01-02", "2026-01-06", bars=bars)

    def test_suspended_symbol_phase3_preserves_crossover_signal(self) -> None:
        # BBB is suspended on 2026-02-03 while AAA trades; Day 2 news lifts BBB sentiment_score from 0.0 -> 1.0.
        # On Day 3 (2026-02-04) BBB resumes: prev_features must still be Day 1 (sentiment_score=0.0),
        # so cross_above 0.5 triggers BUY at Day 3 close and fills on Day 4 (2026-02-05).
        bars = [
            {"date": "2026-02-02", "symbol": "AAA", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0, "volume": 10_000_000},
            {"date": "2026-02-02", "symbol": "BBB", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0, "volume": 10_000_000},
            {"date": "2026-02-03", "symbol": "AAA", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0, "volume": 10_000_000},
            # BBB has NO bar on 2026-02-03 (suspended)
            {"date": "2026-02-04", "symbol": "AAA", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0, "volume": 10_000_000},
            {"date": "2026-02-04", "symbol": "BBB", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 10_000_000},
            {"date": "2026-02-05", "symbol": "AAA", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0, "volume": 10_000_000},
            {"date": "2026-02-05", "symbol": "BBB", "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2, "volume": 10_000_000},
        ]
        news = [
            {"symbol": "BBB", "published_at": "2026-02-03T10:00:00", "title": "BBB surge", "sentiment_label": "positive"},
        ]
        spec = StrategySpec(
            strategy_id="suspend_cross",
            name="Suspend Cross",
            style="quant_daily",
            market="CN",
            params={"max_weight": 0.5, "sentiment_lookback_days": 5},
            rules={"buy": {"field": "sentiment_score", "op": "cross_above", "value": 0.5}},
        )
        res = run_backtest(
            spec,
            ["AAA", "BBB"],
            "2026-02-02",
            "2026-02-05",
            bars=bars,
            news=news,
        )
        skipped = [t for t in res.trades if t["status"] == "SKIPPED"]
        self.assertEqual(skipped, [])
        filled_bbb = [t for t in res.trades if t["status"] == "FILLED" and t["symbol"] == "BBB"]
        self.assertEqual(len(filled_bbb), 1)
        self.assertEqual(filled_bbb[0]["date"], "2026-02-05")

    def test_load_bars_from_duckdb_null_ohlc_and_multisource_dedup(self) -> None:
        con = jevdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE market_bars (
                    symbol VARCHAR, market VARCHAR, trade_date VARCHAR,
                    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
                    volume DOUBLE, amount DOUBLE, source VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE fundamentals (
                    symbol VARCHAR, market VARCHAR, report_date VARCHAR,
                    pe_ttm DOUBLE, pb DOUBLE, roe DOUBLE,
                    revenue DOUBLE, net_income DOUBLE, debt_ratio DOUBLE
                )
                """
            )
            # Insert in reverse priority order; also include NULL OHLC and NaN volume/fundamentals
            rows = [
                ("000858", "CN", "2026-04-01", 10.0, 10.5, 9.8, 10.1, 1000.0, 10000.0, "static_baseline"),
                ("000858", "CN", "2026-04-01", 10.2, 10.6, 9.9, 10.3, 2000.0, 20000.0, "akshare_sina"),
                ("000858", "CN", "2026-04-01", 10.4, 10.8, 10.1, 10.5, float("nan"), 30000.0, "akshare_tx"),
                # Date 2: akshare_tx has NULL close -> fallback to akshare_sina
                ("000858", "CN", "2026-04-02", 10.5, 10.9, 10.2, None, 3000.0, 30000.0, "akshare_tx"),
                ("000858", "CN", "2026-04-02", 10.5, 10.9, 10.2, 10.7, 3500.0, 35000.0, "akshare_sina"),
                # Date 3: all-NULL row must be skipped cleanly
                ("000858", "CN", "2026-04-03", None, None, None, None, None, None, "akshare_tx"),
            ]
            con.executemany("INSERT INTO market_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
            con.execute(
                "INSERT INTO fundamentals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["000858", "CN", "2026-03-31", float("nan"), 2.0, 0.12, 1e9, 1e8, 0.4],
            )
            loaded = load_bars_from_duckdb(["000858"], market="CN", con=con)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["date"], "2026-04-01")
            self.assertAlmostEqual(loaded[0]["close"], 10.5)  # akshare_tx wins
            self.assertIsNone(loaded[0]["volume"])  # NaN sanitized to None
            self.assertEqual(loaded[1]["date"], "2026-04-02")
            self.assertAlmostEqual(loaded[1]["close"], 10.7)  # fallback to akshare_sina

            with tempfile.TemporaryDirectory() as td:
                res = run_backtest(
                    "cn_daily_momentum_sentiment",
                    ["000858"],
                    "2026-04-01",
                    "2026-04-03",
                    market="CN",
                    con=con,
                    strategy_dir=td,
                )
                _assert_json_safe(self, res.to_dict())
        finally:
            con.close()

    def test_compare_strategies_disjoint_dates_and_duplicate_ids(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ids = ["cn_daily_momentum_sentiment", "cn_daily_momentum_sentiment", "cn_mean_reversion_daily"]
            comp = compare_strategies(
                ids,
                ["000858", "601318"],
                "2025-04-24",
                "2025-05-15",
                initial_capital=100000.0,
                market="CN",
                bars=PRICES_CSV,
                strategy_dir=td,
            )
            self.assertEqual(len(comp.strategy_ids), 2)
            self.assertEqual(len(comp.ranking), 2)
            _assert_json_safe(self, comp.to_dict())

            # Direct _align_comparison_curves with disjoint / gapped strategy curves
            r1 = BacktestResult(
                strategy_id="s1",
                strategy_name="S1",
                style="quant_daily",
                market="CN",
                symbols=["AAA"],
                start_date="2026-01-01",
                end_date="2026-01-04",
                initial_capital=10000.0,
                final_equity=10200.0,
                seed=42,
                equity_curve=[
                    {"date": "2026-01-01", "equity": 10000.0, "benchmark_equity": 10000.0, "drawdown": 0.0},
                    {"date": "2026-01-02", "equity": 10200.0, "benchmark_equity": 10100.0, "drawdown": 0.0},
                ],
                trades=[],
                metrics={},
                benchmark_metrics={},
                assumptions={},
                reproducibility_hash="h1",
            )
            r2 = BacktestResult(
                strategy_id="s2",
                strategy_name="S2",
                style="value",
                market="CN",
                symbols=["BBB"],
                start_date="2026-01-01",
                end_date="2026-01-04",
                initial_capital=10000.0,
                final_equity=9800.0,
                seed=42,
                equity_curve=[
                    {"date": "2026-01-03", "equity": 9900.0, "benchmark_equity": 10050.0, "drawdown": 0.01},
                    {"date": "2026-01-04", "equity": 9800.0, "benchmark_equity": 10000.0, "drawdown": 0.02},
                ],
                trades=[],
                metrics={},
                benchmark_metrics={},
                assumptions={},
                reproducibility_hash="h2",
            )
            eq_c, dd_c, b_c = _align_comparison_curves(["s1", "s2"], {"s1": r1, "s2": r2}, 10000.0)
            self.assertEqual(len(eq_c), 4)
            self.assertEqual(len(dd_c), 4)
            self.assertEqual(len(b_c), 4)
            for row in eq_c:
                self.assertIn("s1", row)
                self.assertIn("s2", row)
                self.assertIn("benchmark_equity", row)
            # s2 starts at initial_capital on Jan 1-2; s1 forward-fills 10200.0 on Jan 3-4
            self.assertEqual(eq_c[0]["s2"], 10000.0)
            self.assertEqual(eq_c[3]["s1"], 10200.0)
            _assert_json_safe(self, {"eq": eq_c, "dd": dd_c, "bench": b_c})

            # Empty bars still raises ValueError
            with self.assertRaises(ValueError):
                compare_strategies(["cn_daily_momentum_sentiment"], ["000858"], "2030-01-01", "2030-01-10", bars=PRICES_CSV, strategy_dir=td)


class TestRiskMetrics(unittest.TestCase):
    def test_hand_calculated_five_bar_metrics(self) -> None:
        curve = [
            {"date": "2026-01-02", "equity": 100.0},
            {"date": "2026-01-05", "equity": 110.0},
            {"date": "2026-01-06", "equity": 99.0},
            {"date": "2026-01-07", "equity": 108.9},
            {"date": "2026-01-08", "equity": 119.79},
        ]
        # Returns: +0.10, -0.10, +0.10, +0.10
        # Total return: 119.79 / 100 - 1 = 0.1979
        # Max DD: peak 110 -> 99 = 0.10; duration: Jan 5 -> Jan 7 (still < 110) = 2 days
        trades = [
            {"side": "BUY", "symbol": "A", "quantity": 10, "price": 10.0, "total_fee": 1.0, "notional": 100.0},
            {"side": "SELL", "symbol": "A", "quantity": 10, "price": 12.0, "total_fee": 1.0, "notional": 120.0},
            {"side": "BUY", "symbol": "B", "quantity": 10, "price": 10.0, "total_fee": 1.0, "notional": 100.0},
            {"side": "SELL", "symbol": "B", "quantity": 10, "price": 9.0, "total_fee": 1.0, "notional": 90.0},
        ]
        # FIFO PnLs: A = (120 - 1) - (100 + 1) = +18.0; B = (90 - 1) - (100 + 1) = -12.0
        metrics = compute_risk_metrics(curve, trades, initial_capital=100.0, risk_free_rate=0.0)
        self.assertAlmostEqual(metrics["total_return"], 0.1979, places=6)
        self.assertAlmostEqual(metrics["max_drawdown"], 0.10, places=6)
        self.assertEqual(metrics["max_drawdown_duration_days"], 2)
        self.assertAlmostEqual(metrics["win_rate"], 0.5, places=6)
        self.assertAlmostEqual(metrics["profit_factor"], 1.5, places=6)
        self.assertAlmostEqual(metrics["total_fees"], 4.0, places=2)
        self.assertGreater(metrics["sharpe_ratio"], 0.0)
        self.assertGreater(metrics["sortino_ratio"], 0.0)
        self.assertGreater(metrics["calmar_ratio"], 0.0)
        self.assertAlmostEqual(metrics["cagr"], metrics["annualized_return"], places=6)
        _assert_json_safe(self, metrics)

    def test_edge_cases_finite_json_safe(self) -> None:
        empty = compute_risk_metrics([], [])
        self.assertEqual(empty["num_bars"], 0)
        self.assertEqual(empty["profit_factor"], 0.0)
        _assert_json_safe(self, empty)

        single = compute_risk_metrics([{"date": "2026-01-02", "equity": 100000.0}], [])
        self.assertEqual(single["annualized_volatility"], 0.0)
        self.assertEqual(single["sharpe_ratio"], 0.0)
        _assert_json_safe(self, single)

        flat = compute_risk_metrics(
            [
                {"date": "2026-01-02", "equity": 100.0},
                {"date": "2026-01-05", "equity": 100.0},
                {"date": "2026-01-06", "equity": 100.0},
            ]
        )
        self.assertEqual(flat["annualized_volatility"], 0.0)
        self.assertEqual(flat["sharpe_ratio"], 0.0)
        self.assertTrue(math.isfinite(flat["sortino_ratio"]))
        _assert_json_safe(self, flat)

        flat_rf0 = compute_risk_metrics(
            [
                {"date": "2026-01-02", "equity": 100.0},
                {"date": "2026-01-05", "equity": 100.0},
                {"date": "2026-01-06", "equity": 100.0},
            ],
            risk_free_rate=0.0,
        )
        self.assertEqual(flat_rf0["sortino_ratio"], 0.0)
        _assert_json_safe(self, flat_rf0)

        rising = compute_risk_metrics(
            [
                {"date": "2026-01-02", "equity": 100.0},
                {"date": "2026-01-05", "equity": 105.0},
                {"date": "2026-01-06", "equity": 110.25},
                {"date": "2026-01-07", "equity": 116.0},
            ],
            [{"side": "SELL", "symbol": "A", "realized_pnl": 500.0, "notional": 1000.0}],
            risk_free_rate=0.0,
        )
        self.assertEqual(rising["max_drawdown"], 0.0)
        self.assertEqual(rising["sortino_ratio"], 0.0)  # zero downside deviation -> finite 0.0
        self.assertIsNone(rising["profit_factor"])  # all winning trades -> None (JSON null)
        _assert_json_safe(self, rising)

        wipeout = compute_risk_metrics(
            [
                {"date": "2026-01-02", "equity": 100.0},
                {"date": "2026-01-05", "equity": 50.0},
                {"date": "2026-01-06", "equity": 0.0},
            ],
            initial_capital=100.0,
        )
        self.assertEqual(wipeout["total_return"], -1.0)
        self.assertEqual(wipeout["annualized_return"], -1.0)
        self.assertEqual(wipeout["max_drawdown"], 1.0)
        _assert_json_safe(self, wipeout)


class TestPortfolioRisk(unittest.TestCase):
    def test_portfolio_risk_metrics_correlation_hhi_exposures_and_beta(self) -> None:
        holdings = [
            {"symbol": "AAA", "market_value": 50000.0, "sector": "Power", "market": "CN"},
            {"symbol": "BBB", "market_value": 50000.0, "sector": "Tech", "market": "US"},
            {"symbol": "CCC", "market_value": 0.0, "sector": "Ignored", "market": "CN"},
        ]
        # Bench returns: +1%, -2%, +3%, -1%
        bench = [100.0, 101.0, 98.98, 101.9494, 100.929906]
        # Both AAA and BBB move at 2x bench returns -> portfolio return == 2x bench return -> beta == 2.0
        p_2x = [100.0]
        b_rets = [bench[i] / bench[i - 1] - 1.0 for i in range(1, len(bench))]
        for r in b_rets:
            p_2x.append(p_2x[-1] * (1.0 + 2.0 * r))

        res_no_bench = compute_portfolio_risk(holdings, {"AAA": p_2x, "BBB": p_2x})
        self.assertIsNone(res_no_bench["portfolio_beta"])
        self.assertAlmostEqual(res_no_bench["hhi"], 0.5, places=6)
        self.assertAlmostEqual(res_no_bench["effective_n"], 2.0, places=6)
        self.assertEqual(res_no_bench["concentration_level"], "high")
        self.assertGreaterEqual(res_no_bench["cvar_95"], res_no_bench["var_95"])
        self.assertGreaterEqual(res_no_bench["var_95"], 0.0)
        self.assertAlmostEqual(sum(res_no_bench["sector_exposure"].values()), 1.0, places=6)
        self.assertAlmostEqual(sum(res_no_bench["market_exposure"].values()), 1.0, places=6)
        self.assertAlmostEqual(res_no_bench["correlation_matrix"]["AAA"]["AAA"], 1.0, places=6)
        self.assertAlmostEqual(res_no_bench["correlation_matrix"]["AAA"]["BBB"], 1.0, places=6)

        res_with_bench = compute_portfolio_risk(
            holdings, {"AAA": p_2x, "BBB": p_2x}, benchmark_history=bench
        )
        self.assertIsNotNone(res_with_bench["portfolio_beta"])
        self.assertAlmostEqual(res_with_bench["portfolio_beta"], 2.0, places=4)
        _assert_json_safe(self, res_with_bench)

        # Zero-variance asset has 0.0 off-diagonal correlation
        flat_res = compute_portfolio_risk(
            [
                {"symbol": "AAA", "market_value": 50.0},
                {"symbol": "FLAT", "market_value": 50.0},
            ],
            {"AAA": p_2x, "FLAT": [10.0, 10.0, 10.0, 10.0, 10.0]},
        )
        self.assertEqual(flat_res["correlation_matrix"]["AAA"]["FLAT"], 0.0)
        self.assertEqual(flat_res["correlation_matrix"]["FLAT"]["FLAT"], 1.0)
        _assert_json_safe(self, flat_res)

        empty_res = compute_portfolio_risk([], {})
        self.assertEqual(empty_res["warnings"], ["empty_or_zero_portfolio"])
        _assert_json_safe(self, empty_res)

    def test_portfolio_risk_negative_and_non_finite_holding_warnings(self) -> None:
        res = compute_portfolio_risk(
            [
                {"symbol": "NEG", "market_value": -500.0},
                {"symbol": "NAN_Q", "quantity": float("nan")},
                {"symbol": "BOOL_W", "weight": True},
                {"symbol": "OK", "market_value": 1000.0},
            ],
            {"OK": [10.0, 10.5, 10.2, 10.8]},
        )
        self.assertEqual(res["weights"], {"OK": 1.0})
        self.assertIn("negative_holding_value:NEG", res["warnings"])
        self.assertIn("invalid_or_non_finite_holding:NAN_Q", res["warnings"])
        self.assertIn("invalid_or_non_finite_holding:BOOL_W", res["warnings"])
        _assert_json_safe(self, res)

        all_neg = compute_portfolio_risk([{"symbol": "NEG", "market_value": -500.0}], {})
        self.assertIn("negative_holding_value:NEG", all_neg["warnings"])
        self.assertIn("empty_or_zero_portfolio", all_neg["warnings"])
        _assert_json_safe(self, all_neg)


class TestPaperLedgerRulesAndFees(unittest.TestCase):
    def test_validation_rejections_and_clean_state(self) -> None:
        with PaperAccountService(":memory:", initial_capital=10000.0, initial_date="2026-05-06") as svc:
            bar = {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.1, "prev_close": 10.0}
            cases = [
                (("  ", "BUY", 100), {"reference_bar": bar}, "invalid_symbol"),
                (("000858", "BUY", 100), {"market": "HK", "reference_bar": bar}, "invalid_market"),
                (("000858", "SHORT", 100), {"reference_bar": bar}, "invalid_side"),
                (("000858", "BUY", 100), {"order_type": "stop", "reference_bar": bar}, "invalid_order_type"),
                (("000858", "BUY", 0), {"reference_bar": bar}, "invalid_quantity"),
                (("000858", "BUY", True), {"reference_bar": bar}, "invalid_quantity"),
                (("000858", "BUY", 150), {"reference_bar": bar}, "invalid_cn_buy_lot"),
                (("000858", "BUY", 100), {"order_type": "limit", "limit_price": -1.0, "reference_bar": bar}, "invalid_limit_price"),
                (("000858", "BUY", 100), {"reference_bar": {"open": 10.0, "high": 9.5, "low": 10.2}}, "invalid_reference_bar"),
                (("000858", "BUY", 100), {}, "missing_reference_price"),
                (("000858", "BUY", 100), {"reference_bar": {"open": 11.0, "high": 11.0, "low": 10.8, "prev_close": 10.0}}, "price_limit_exceeded"),
                (("300750", "BUY", 100), {"reference_bar": {"open": 11.5, "high": 12.1, "low": 11.0, "prev_close": 10.0}, "price_limit_rate": 0.20, "order_type": "limit", "limit_price": 12.5}, "price_limit_exceeded"),
                (("000858", "BUY", 100), {"order_type": "limit", "limit_price": 9.9, "reference_bar": bar}, "limit_price_not_met"),
                (("000858", "BUY", 100), {"reference_bar": {"open": 10.0, "high": 10.0, "low": 9.8, "prev_close": 10.0}}, "outside_bar_range"),
                (("000858", "SELL", 100), {"reference_bar": bar}, "insufficient_position"),
                (("000858", "BUY", 1000), {"reference_bar": {"open": 10.0, "high": 10.5, "low": 9.8, "prev_close": 10.0}}, "insufficient_cash"),
            ]
            for args, kwargs, expected_code in cases:
                res = svc.submit_order(*args, **kwargs)
                self.assertFalse(res["accepted"], f"Expected rejection {expected_code} for {args} {kwargs}")
                self.assertEqual(res["reject_code"], expected_code)

            summary = svc.get_account_summary()
            self.assertEqual(summary["cash"], 10000.0)
            self.assertEqual(summary["positions"], [])
            self.assertEqual(len(svc.get_orders()), len(cases))
            self.assertEqual(len(svc.get_fills()), 0)

    def test_cn_fees_t_plus_one_fifo_and_odd_lot_close(self) -> None:
        with PaperAccountService(":memory:", initial_capital=50000.0, initial_date="2026-05-06") as svc:
            # Day 1: buy 200 shares @ ref 10.00 -> fill 10.01 (slippage 5bps = 0.005 -> rounds to 10.01)
            buy1 = svc.submit_order(
                "000858",
                "BUY",
                200,
                reference_bar={"open": 10.00, "high": 10.20, "low": 9.90, "close": 10.10, "prev_close": 10.00},
            )
            self.assertTrue(buy1["accepted"])
            self.assertEqual(buy1["fill_price"], 10.01)
            self.assertEqual(buy1["notional"], 2002.0)
            self.assertEqual(buy1["commission"], 5.0)  # min 5.00 > 2002 * 0.0003
            self.assertEqual(buy1["transfer_fee"], 0.02)  # 2002 * 0.00001 = 0.02002
            self.assertEqual(buy1["stamp_duty"], 0.0)
            self.assertEqual(buy1["total_fee"], 5.02)
            self.assertEqual(buy1["cash_after"], 47992.98)

            # Same day sell rejected due to T+1
            t1_reject = svc.submit_order(
                "000858",
                "SELL",
                100,
                reference_bar={"open": 10.10, "high": 10.20, "low": 9.90, "prev_close": 10.00},
            )
            self.assertFalse(t1_reject["accepted"])
            self.assertEqual(t1_reject["reject_code"], "t_plus_one_restricted")

            # Advance to Day 2, buy another 100 @ ref 12.00 -> fill 12.01
            svc.advance_day("2026-05-07", {"000858": 12.00})
            buy2 = svc.submit_order(
                "000858",
                "BUY",
                100,
                reference_bar={"open": 12.00, "high": 12.20, "low": 11.80, "close": 12.00, "prev_close": 10.10},
                price_limit_rate=0.20,
            )
            self.assertTrue(buy2["accepted"])

            # On Day 2, total held = 300, sellable = 200 (Day 2's 100 is T+1 locked)
            over_t1 = svc.submit_order(
                "000858",
                "SELL",
                300,
                reference_bar={"open": 12.00, "high": 12.20, "low": 11.80, "prev_close": 10.10},
                price_limit_rate=0.20,
            )
            self.assertFalse(over_t1["accepted"])
            self.assertEqual(over_t1["reject_code"], "t_plus_one_restricted")

            # Odd-lot sell when sellable is 200 (round lot) must be rejected
            odd_reject = svc.submit_order(
                "000858",
                "SELL",
                50,
                reference_bar={"open": 12.00, "high": 12.20, "low": 11.80, "prev_close": 10.10},
                price_limit_rate=0.20,
            )
            self.assertFalse(odd_reject["accepted"])
            self.assertEqual(odd_reject["reject_code"], "invalid_cn_sell_lot")

            # Sell the 200 sellable shares from Day 1 lot (FIFO) @ ref 12.00 -> fill 11.99
            sell1 = svc.submit_order(
                "000858",
                "SELL",
                200,
                reference_bar={"open": 12.00, "high": 12.20, "low": 11.80, "close": 12.00, "prev_close": 10.10},
                price_limit_rate=0.20,
            )
            self.assertTrue(sell1["accepted"])
            self.assertEqual(sell1["fill_price"], 11.99)
            self.assertEqual(sell1["notional"], 2398.0)
            self.assertEqual(sell1["commission"], 5.0)
            self.assertEqual(sell1["transfer_fee"], 0.02)
            self.assertEqual(sell1["stamp_duty"], 1.20)  # 2398 * 0.0005 = 1.199 -> 1.20
            self.assertEqual(sell1["total_fee"], 6.22)
            # FIFO PnL: (2398 - 6.22) - (2002 + 5.02) = 2391.78 - 2007.02 = 384.76
            self.assertAlmostEqual(sell1["realized_pnl"], 384.76, places=2)
            _assert_json_safe(self, svc.get_account_summary())
            _assert_json_safe(self, svc.get_equity_history())

            # Verify CN odd-lot remainder closing rule (150 sellable shares -> sell 30 rejected, sell 50 accepted)
            with svc._connect(write=True) as con:
                con.execute(
                    """
                    INSERT INTO position_lots (
                        lot_id, symbol, market, quantity, remaining_qty, buy_date,
                        avg_cost, total_cost, strategy_id, created_at, updated_at
                    ) VALUES ('lot_odd', '601318', 'CN', 150, 150, '2026-05-05', 50.0, 7500.0, NULL, '2026-05-05T00:00:00Z', '2026-05-05T00:00:00Z')
                    """
                )
            bad_odd = svc.submit_order(
                "601318",
                "SELL",
                30,
                reference_bar={"open": 51.0, "high": 52.0, "low": 50.0, "prev_close": 50.0},
            )
            self.assertFalse(bad_odd["accepted"])
            self.assertEqual(bad_odd["reject_code"], "invalid_cn_sell_lot")

            good_odd = svc.submit_order(
                "601318",
                "SELL",
                50,
                reference_bar={"open": 51.0, "high": 52.0, "low": 50.0, "prev_close": 50.0},
            )
            self.assertTrue(good_odd["accepted"])

    def test_backdated_malformed_trade_date_price_limit_and_mark_prices(self) -> None:
        with PaperAccountService(":memory:", initial_capital=50000.0, initial_date="2026-05-06") as svc:
            bar = {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.1, "prev_close": 10.0}
            buy1 = svc.submit_order("000858", "BUY", 100, reference_bar=bar)
            self.assertTrue(buy1["accepted"])
            cash_before = svc.get_account_summary()["cash"]
            snaps_before = svc.get_equity_history()

            # Backdated trade_date must be rejected cleanly and never bypass CN T+1 or mutate snapshots
            backdated = svc.submit_order("000858", "BUY", 100, trade_date="2026-05-05", reference_bar=bar)
            self.assertFalse(backdated["accepted"])
            self.assertEqual(backdated["reject_code"], "invalid_trade_date")

            # Malformed trade_dates must return structured rejection, never raise ValueError
            for bad_dt in ("not-a-date", "", "2026-05-06bad", 123):
                rej = svc.submit_order("000858", "BUY", 100, trade_date=bad_dt, reference_bar=bar)  # type: ignore[arg-type]
                self.assertFalse(rej["accepted"])
                self.assertEqual(rej["reject_code"], "invalid_trade_date")

            # T+1 must still block same-day sell (no backdated lot created)
            t1 = svc.submit_order("000858", "SELL", 100, reference_bar=bar)
            self.assertFalse(t1["accepted"])
            self.assertEqual(t1["reject_code"], "t_plus_one_restricted")
            self.assertEqual(svc.get_account_summary()["cash"], cash_before)
            self.assertEqual(svc.get_equity_history(), snaps_before)

            # Invalid price_limit_rate (NaN, Inf, out-of-range, bool, string) rejected cleanly
            lim_bar = {"open": 15.0, "high": 15.5, "low": 14.5, "close": 15.0, "prev_close": 10.0}
            for bad_rate in (float("nan"), float("inf"), -0.1, 1.0, 1.2, True, "0.1"):
                rej_lim = svc.submit_order(
                    "000858",
                    "BUY",
                    100,
                    reference_bar=lim_bar,
                    price_limit_rate=bad_rate,  # type: ignore[arg-type]
                )
                self.assertFalse(rej_lim["accepted"])
                self.assertEqual(rej_lim["reject_code"], "invalid_price_limit_rate")

            # Mark-price extraction: {"close": None, "price": 13.5} works; bad values raise ValueError
            adv = svc.advance_day("2026-05-07", {"000858": {"close": None, "price": 13.5}})
            self.assertEqual(adv["positions"][0]["mark_price"], 13.5)

            ov = svc.get_account_summary(price_overrides={"000858": {"close": None, "price": 14.0}})
            self.assertEqual(ov["positions"][0]["mark_price"], 14.0)

            for bad_mark in (
                {"close": None, "price": None},
                None,
                True,
                float("nan"),
                -5.0,
            ):
                with self.assertRaises(ValueError):
                    svc.advance_day("2026-05-08", {"000858": bad_mark})
                with self.assertRaises(ValueError):
                    svc.get_account_summary(price_overrides={"000858": bad_mark})  # type: ignore[dict-item]
            with self.assertRaises(ValueError):
                svc.advance_day("2026-05-08", {"000858": {"close": 13.5, "prev_close": float("nan")}})

    def test_us_t_plus_zero_and_assumptions(self) -> None:
        with PaperAccountService(
            ":memory:",
            initial_capital=10000.0,
            slippage_bps=5.0,
            us_commission_rate=0.001,
            us_min_commission=1.0,
            initial_date="2026-05-06",
        ) as svc:
            b = svc.submit_order(
                "AAPL",
                "BUY",
                3,
                market="US",
                reference_bar={"open": 200.0, "high": 205.0, "low": 199.0, "close": 202.0},
            )
            self.assertTrue(b["accepted"])
            s = svc.submit_order(
                "AAPL",
                "SELL",
                3,
                market="US",
                reference_bar={"open": 202.0, "high": 205.0, "low": 199.0, "close": 202.0},
            )
            self.assertTrue(s["accepted"])
            assumptions = svc.get_simplification_assumptions()
            self.assertEqual(assumptions["cn_a_share_rules"]["t_plus"], 1)
            self.assertEqual(assumptions["us_equity_rules"]["t_plus"], 0)
            _assert_json_safe(self, assumptions)


class TestPaperLedgerMultiProcessPersistence(unittest.TestCase):
    def test_multiprocess_buy_advance_sell_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_file = str(Path(td) / "paper_ledger.duckdb")

            # Process 1: initialize & buy 100 shares of 000858 on 2026-05-06
            p1 = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json, sys; from stock_research.quant import PaperAccountService; "
                        "svc = PaperAccountService(sys.argv[1], initial_capital=100000.0, initial_date='2026-05-06'); "
                        "res = svc.submit_order('000858', 'BUY', 100, reference_bar={'open': 130.0, 'high': 132.0, 'low': 129.0, 'close': 131.0, 'prev_close': 129.0}); "
                        "print(json.dumps(res))"
                    ),
                    db_file,
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=True,
            )
            r1 = json.loads(p1.stdout.strip())
            self.assertTrue(r1["accepted"])

            # Process 2: verify persisted holding, advance day to 2026-05-07, sell all 100 shares
            p2 = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json, sys; from stock_research.quant import PaperAccountService; "
                        "svc = PaperAccountService(sys.argv[1]); "
                        "summary_before = svc.get_account_summary(); "
                        "assert len(summary_before['positions']) == 1 and summary_before['positions'][0]['quantity'] == 100; "
                        "svc.advance_day('2026-05-07', {'000858': 135.0}); "
                        "res = svc.submit_order('000858', 'SELL', 100, reference_bar={'open': 135.0, 'high': 136.0, 'low': 134.0, 'close': 135.5, 'prev_close': 131.0}); "
                        "print(json.dumps(res))"
                    ),
                    db_file,
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=True,
            )
            r2 = json.loads(p2.stdout.strip())
            self.assertTrue(r2["accepted"])
            self.assertGreater(r2["realized_pnl"], 0.0)

            # Process 3: verify 0 positions, realized PnL, full audit trail, and clean reset_account
            p3 = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json, sys; from stock_research.quant import PaperAccountService; "
                        "svc = PaperAccountService(sys.argv[1]); "
                        "s = svc.get_account_summary(); "
                        "orders = svc.get_orders(); fills = svc.get_fills(); hist = svc.get_equity_history(); "
                        "assert s['positions'] == [] and s['realized_pnl'] > 0; "
                        "assert len(orders) == 2 and len(fills) == 2 and len(hist) >= 2; "
                        "reset_s = svc.reset_account(50000.0, initial_date='2026-05-08'); "
                        "assert reset_s['cash'] == 50000.0 and len(svc.get_orders()) == 0; "
                        "print(json.dumps({'realized_pnl': s['realized_pnl'], 'reset_cash': reset_s['cash']}))"
                    ),
                    db_file,
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=True,
            )
            r3 = json.loads(p3.stdout.strip())
            self.assertGreater(r3["realized_pnl"], 0.0)
            self.assertEqual(r3["reset_cash"], 50000.0)

    def test_12_process_plus_reader_concurrent_duckdb_stress(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_file = str(Path(td) / "concurrent_stress.duckdb")
            # Initialize account before concurrent wave so all processes share same initial capital/date
            init_svc = PaperAccountService(db_file, initial_capital=500000.0, initial_date="2026-05-06")
            init_svc.close()

            writer_code = (
                "import json, sys; from stock_research.quant import PaperAccountService; "
                "svc = PaperAccountService(sys.argv[1]); "
                "bar = {'open': 10.0, 'high': 10.5, 'low': 9.8, 'close': 10.1, 'prev_close': 10.0}; "
                "res = [svc.submit_order('000858', 'BUY', 100, reference_bar=bar) for _ in range(3)]; "
                "assert all(r['accepted'] for r in res), res; "
                "print(json.dumps([r['total_fee'] + r['notional'] for r in res]))"
            )
            reader_code = (
                "import json, sys; from stock_research.quant import PaperAccountService; "
                "svc = PaperAccountService(sys.argv[1]); "
                "s = svc.get_account_summary(); "
                "o = svc.get_orders(); "
                "print(json.dumps({'cash': s['cash'], 'orders': len(o)}))"
            )

            writers = [
                subprocess.Popen(
                    [sys.executable, "-c", writer_code, db_file],
                    cwd=str(REPO_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(12)
            ]
            reader = subprocess.Popen(
                [sys.executable, "-c", reader_code, db_file],
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            total_outflow = 0.0
            for idx, proc in enumerate(writers):
                out, err = proc.communicate(timeout=60)
                self.assertEqual(proc.returncode, 0, f"Writer {idx} failed: {err}")
                total_outflow += sum(json.loads(out.strip()))

            r_out, r_err = reader.communicate(timeout=60)
            self.assertEqual(reader.returncode, 0, f"Reader failed: {r_err}")
            _ = json.loads(r_out.strip())

            check_svc = PaperAccountService(db_file)
            summary = check_svc.get_account_summary()
            orders = check_svc.get_orders()
            fills = check_svc.get_fills()
            self.assertEqual(len(orders), 36)
            self.assertEqual(len(fills), 36)
            self.assertEqual(summary["positions"][0]["quantity"], 3600)
            self.assertAlmostEqual(summary["cash"], 500000.0 - total_outflow, places=2)

    def test_6_thread_multi_instance_contention(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_file = str(Path(td) / "thread_stress.duckdb")
            PaperAccountService(db_file, initial_capital=500000.0, initial_date="2026-05-06").close()
            bar = {"open": 10.0, "high": 10.5, "low": 9.8, "close": 10.1, "prev_close": 10.0}

            def _thread_worker(idx: int) -> float:
                svc = PaperAccountService(db_file)
                outflow = 0.0
                for _ in range(3):
                    res = svc.submit_order("000858", "BUY", 100, reference_bar=bar)
                    self.assertTrue(res["accepted"], res)
                    outflow += res["notional"] + res["total_fee"]
                    _ = svc.get_account_summary()
                return outflow

            with ThreadPoolExecutor(max_workers=6) as pool:
                outflows = list(pool.map(_thread_worker, range(6)))

            check_svc = PaperAccountService(db_file)
            summary = check_svc.get_account_summary()
            self.assertEqual(len(check_svc.get_orders()), 18)
            self.assertEqual(summary["positions"][0]["quantity"], 1800)
            self.assertAlmostEqual(summary["cash"], 500000.0 - sum(outflows), places=2)


if __name__ == "__main__":
    unittest.main()
