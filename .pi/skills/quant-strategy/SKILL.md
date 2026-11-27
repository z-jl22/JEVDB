---
name: quant-strategy
description: Propose, save, backtest, and compare daily-frequency quantitative trading strategies (including A-share daily rebalancing), analyze portfolio risk (Sharpe, max drawdown, VaR/CVaR, HHI), and simulate orders on the persistent paper-trading ledger respecting T+1, lot sizes, price limits, and fees.
license: MIT
metadata:
  upstream: https://github.com/jinhyeonseo01/stock-trade-analysis-skills
  upstream_commit: 66ebac37bfc51a4918fc1edc0a6d84760851d52d
---

<!--
PROVENANCE & LICENSE NOTICE (R5 Compliance):
- Upstream Source: jinhyeonseo01/stock-trade-analysis-skills (references/validation.md, references/decision-policy.md)
  Repository URL: https://github.com/jinhyeonseo01/stock-trade-analysis-skills
  Commit SHA: 66ebac37bfc51a4918fc1edc0a6d84760851d52d
  License: MIT (Copyright (c) 2026 Clrain)
- Modifications Made:
  1. Adapted strategy validation and trade-gating policy to the JEVDB M2 declarative AST schema (`StrategySpec`), deterministic backtester (`run_backtest_tool`, `compare_strategies_tool`), portfolio risk engine (`analyze_portfolio_risk`), and paper ledger (`paper_account_status`, `submit_paper_order`).
  2. Added explicit A-share market mechanics (T+1 sell lock, 100-share buy lots, +/-10% price limit, commission/transfer/stamp duty/slippage).
- Security & Operational Risk Notes:
  1. `save_quant_strategy` persists declarative JSON AST rules only (no arbitrary Python code execution).
  2. `submit_paper_order` operates exclusively on the local simulated paper-trading ledger and never connects to real brokerage accounts.
  3. Backtest results depend on historical bars and simplification assumptions (next-bar open/close fill model, slippage) and do not guarantee future returns.
-->

# Quantitative Strategy, Backtesting & Paper-Trading Workflow

Follow this workflow for daily-frequency quantitative strategy proposals, backtests, comparisons, risk checks, and paper-trading execution:

## 1. Inspect Existing Strategies & Market Data
1. Call `list_quant_strategies()` to view built-in and saved strategies (e.g., `cn_daily_momentum_sentiment`, `cn_mean_reversion_daily`, `value_quality_margin_of_safety`).
2. Call `query_stored_market_data` (and `fetch_market_data` if needed) to verify historical `market_bars` date range for the target symbols.

## 2. Propose & Save Declarative Strategy Rules (Optional)
If proposing a new strategy, call `save_quant_strategy` using the valid declarative AST schema:
- Top-level `rules` keys MUST be a subset of `{"buy", "sell", "ranking"}`.
- Leaf comparisons: `{"field": "<indicator>", "op": ">", "value": 0.0}` (or `"param"` / `"ref"`).
- Valid `op`: `>`, `>=`, `<`, `<=`, `==`, `!=`, `between`, `in`, `cross_above`, `cross_below`.
- Combinators: `{"all": [...]}`, `{"any": [...]}`, `{"not": {...}}`, `{"const": false}`.
- Common fields: `close`, `open`, `high`, `low`, `volume`, `sma_short`, `sma_long`, `momentum_return`, `rsi`, `volatility`, `sentiment_score`, `pe_ttm`, `pb`, `roe`.

## 3. Reproducible Backtest & Multi-Strategy Comparison
3. Call `run_backtest_tool(strategy_id=..., symbols=[...], start_date=..., end_date=..., market="CN")` to obtain deterministic equity curve, trade log, `reproducibility_hash`, and risk metrics (`total_return`, `cagr`, `sharpe_ratio`, `sortino_ratio`, `calmar_ratio`, `max_drawdown`, `var_95`, `cvar_95`, `win_rate`, `turnover`).
4. Call `compare_strategies_tool(strategy_ids=[...], ...)` to compare at least 2 strategies side-by-side (e.g., daily quant vs long-term value).
5. Call `analyze_portfolio_risk(holdings=[...])` to evaluate portfolio volatility, 95% VaR/CVaR, correlation matrix, and HHI concentration.

## 4. Paper-Trading Execution Rules
6. Call `paper_account_status()` before proposing or submitting simulated orders.
7. Only call `submit_paper_order(...)` if the user asks to execute/place a simulated trade, respecting:
   - **CN (A-shares)**: T+1 settlement (`sellable_qty` excludes same-day buys), buy lot multiple of 100 shares, ±10% price limit, 0.03% (min ¥5) commission, 0.001% transfer fee, 0.05% sell stamp duty, 5 bps slippage.
   - **US**: T+0 settlement, 1-share minimum lot.
