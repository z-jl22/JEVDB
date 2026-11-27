---
name: value-investing
description: Perform medium-to-long-term fundamental and value investing analysis covering business quality, ROE, margins, debt, PE/PB valuation, margin of safety, and value strategy backtesting. Use for fundamental stock evaluations (e.g., 000858, AAPL) and value portfolio decisions.
license: Apache-2.0 AND MIT
metadata:
  upstream_1: https://github.com/prof-little-bear/cc-equity-research
  upstream_1_commit: 7c428944a6718c35461f839c618ae66334b6371b
  upstream_2: https://github.com/jinhyeonseo01/stock-trade-analysis-skills
  upstream_2_commit: 66ebac37bfc51a4918fc1edc0a6d84760851d52d
---

<!--
PROVENANCE & LICENSE NOTICE (R5 Compliance):
- Upstream Source 1: prof-little-bear/cc-equity-research (anthropic-equity-research-skills/idea-generation, initiating-coverage)
  Repository URL: https://github.com/prof-little-bear/cc-equity-research
  Commit SHA: 7c428944a6718c35461f839c618ae66334b6371b
  Upstream Origin: anthropics/financial-services @ 853f755a61f7bbb045c681327f46b354419030a1
  License: Apache-2.0
- Upstream Source 2: jinhyeonseo01/stock-trade-analysis-skills (references/research-discipline.md)
  Repository URL: https://github.com/jinhyeonseo01/stock-trade-analysis-skills
  Commit SHA: 66ebac37bfc51a4918fc1edc0a6d84760851d52d
  License: MIT (Copyright (c) 2026 Clrain)
- Modifications Made:
  1. Mapped fundamental analysis steps onto JEVDB DuckDB (`fundamentals`, `edgar_filings`, `market_bars`, `sector_constituents`) and MCP tools (`fetch_market_data`, `query_stored_market_data`, `jevdb_semantic_query`).
  2. Integrated M2 value strategy backtesting (`value_quality_margin_of_safety` via `run_backtest_tool`) and `analyze_portfolio_risk`.
- Security & Operational Risk Notes:
  1. Financial statements and valuation ratios can lag or differ by accounting basis; cross-check `fundamentals` with `edgar_filings` (US) or `web_search` (CN).
  2. Output is analytical decision support, not regulated investment advice.
-->

# Value Investing Analysis Workflow

Use this structured workflow for medium- and long-term fundamental / value investing queries:

## 1. Gather Multi-Source Fundamentals & Price History
1. Call `query_stored_market_data` for the target symbol (`table="auto"`).
2. If fundamentals, bars, or filings are missing or outdated, call `fetch_market_data`:
   - CN stock: `fetch_market_data(source="akshare_stock", symbol="<code6>", days=120)`
   - US stock: `fetch_market_data(source="yfinance", symbol="<TICKER>", period="6mo")` and `fetch_market_data(source="sec_edgar", symbol="<TICKER>")`
3. Call `jevdb_semantic_query` on `target="combined"` (or `"news_events"` / `"fundamentals"`) to inspect qualitative catalysts, earnings quality, and risks across static and dynamic tables.
4. Optionally call `web_search` for recent earnings commentary, dividend policy, or competitive moat developments.

## 2. Evaluate Quality, Valuation & Margin of Safety
Analyze strictly from observed tool figures:
- **Profitability & Quality**: ROE, revenue growth, net income, profit durability.
- **Balance Sheet & Leverage**: Debt ratio (`debt_ratio`), cash/earnings resilience.
- **Valuation & Margin of Safety**: `pe_ttm`, `pb`, price vs historical range and sector peers (`sector_constituents`).

## 3. Quantitative Value Backtest & Risk Check
5. Call `list_quant_strategies(style="value")` and `run_backtest_tool(strategy_id="value_quality_margin_of_safety", symbols=[...], start_date=..., end_date=...)` when evaluating strategy performance or portfolio allocation.
6. If multiple holdings are discussed, call `analyze_portfolio_risk` to inspect volatility, 95% VaR/CVaR, correlation, and HHI concentration.

## 4. Synthesis Structure
Present: (a) Core Value Thesis, (b) Verified Financial & Valuation Table with sources, (c) Backtest & Risk Metrics (if run), (d) Key Catalysts, and (e) Thesis Invalidation Risks + Disclaimer.
