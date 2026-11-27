---
name: cross-verify
description: Cross-verify financial numbers, claims, date windows, and citations across independent sources (JEVDB DuckDB, AkShare, yfinance, SEC EDGAR, Finnhub, web search). Use to reconcile discrepancies and prevent hallucinated figures in investment research.
license: MIT
metadata:
  upstream_1: https://github.com/jinhyeonseo01/stock-trade-analysis-skills
  upstream_1_commit: 66ebac37bfc51a4918fc1edc0a6d84760851d52d
  upstream_2: https://github.com/badlogic/pi-skills
  upstream_2_commit: 90bb51cae36515a648515b633a81c0c6efc8c74d
---

<!--
PROVENANCE & LICENSE NOTICE (R5 Compliance):
- Upstream Source 1: jinhyeonseo01/stock-trade-analysis-skills (skills/analyze-stock-decision/SKILL.md, references/research-discipline.md)
  Repository URL: https://github.com/jinhyeonseo01/stock-trade-analysis-skills
  Commit SHA: 66ebac37bfc51a4918fc1edc0a6d84760851d52d
  License: MIT (Copyright (c) 2026 Clrain)
- Upstream Source 2: badlogic/pi-skills (brave-search/SKILL.md search + content extraction pattern)
  Repository URL: https://github.com/badlogic/pi-skills
  Commit SHA: 90bb51cae36515a648515b633a81c0c6efc8c74d
  License: MIT
- Modifications Made:
  1. Replaced paid Brave Search API dependency with free DuckDuckGo (`web_search`) + SSRF-guarded `web_fetch_url` (`httpx` + `trafilatura`/`BeautifulSoup`).
  2. Replaced Korean-market FactChart references with JEVDB multi-source verification (`query_stored_market_data`, `jevdb_semantic_query`, `fetch_market_data` across AkShare/yfinance/SEC EDGAR/Finnhub).
  3. Enforced strict epistemic labeling and numeric grounding aligned with `stock_research/agent/verifier.py`.
- Security & Operational Risk Notes:
  1. External web pages fetched via `web_fetch_url` are protected by strict SSRF validation (blocking private/loopback/link-local IPs and non-HTTP schemes) and stripped of scripts/styles, but text content remains untrusted against prompt injection.
  2. No shell execution or file writes are permitted during verification.
-->

# Cross-Verification Discipline

Before presenting any stock or sector conclusion, verify every critical claim and number across independent sources:

## 1. Epistemic Labeling
Classify claims internally and in verification summaries:
- **FACT**: Directly observed in primary tool output (price bar, EDGAR XBRL filing, AkShare indicator, DuckDB table) with exact date and source.
- **CALCULATION**: Derived arithmetically from verified facts (e.g., 3-day percentage return, PE = price / EPS), showing inputs.
- **ESTIMATE / VIEW**: Forward-looking commentary or qualitative catalyst clearly attributed to its source.
- **UNKNOWN / N/A**: Missing or stale data that must be disclosed rather than guessed.

## 2. Multi-Source Reconciliation Checks
1. **Cross-check at least 2 independent sources** whenever possible:
   - A-shares: `query_stored_market_data` / `fetch_market_data` (AkShare Sina/THS/EM) vs `jevdb_semantic_query` vs `web_search` / `web_fetch_url`.
   - US stocks: `yfinance` market/fundamentals vs `sec_edgar` XBRL filings (`10-K`/`10-Q`) vs `finnhub_news` / `web_search`.
2. **Date & Window Alignment**: Confirm whether data covers the exact requested window (e.g. latest 3 trading days vs calendar days; report date of fundamentals).
3. **Unit & Currency Consistency**: Check CNY vs USD, 亿元 vs millions/billions, percentage (`0.15` vs `15%`) before comparing across sources.
4. **Conflict Handling**: If two sources disagree (e.g., yfinance PE vs EDGAR-implied valuation), report both numbers and their sources rather than silently averaging.

## 3. Grounding Checklist Before Answering
- Every numeric figure (`price`, `change_pct`, `PE`, `PB`, `ROE`, `revenue`, `Sharpe`, `max_drawdown`) appears verbatim in your tool outputs.
- Every cited URL or table/provider source (`akshare_ths`, `yfinance`, `sec_edgar`, `static_baseline`, `https://...`) came directly from tool outputs.
