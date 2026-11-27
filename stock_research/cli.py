"""Unified CLI for JEVDB Stock Research & Paper-Trading Prototype."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from typing import Any

from stock_research.agent.pi_runner import run_research_query
from stock_research.quant.backtest import load_bars_from_duckdb, run_backtest
from stock_research.quant.ledger import PaperAccountService
from stock_research.quant.strategies import ensure_builtin_strategies
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
from stock_research.storage.schema import db_session, ensure_baseline_seeded


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


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _format_ask_text(res: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"=== 需求 (Prompt) ===\n{res.get('prompt', '')}")
    lines.append("\n=== 工具调用 (Tool Calls) ===")
    tool_calls = res.get("tool_calls") or []
    if not tool_calls:
        lines.append("(无工具调用)")
    else:
        for idx, tc in enumerate(tool_calls, 1):
            args_txt = json.dumps(tc.get("args") or {}, ensure_ascii=False)
            lines.append(
                f"{idx}. {tc.get('name')}({args_txt}) [{tc.get('duration_ms', 0)}ms]\n"
                f"   -> {tc.get('result_summary', '')}"
            )

    ver = res.get("verification") or {}
    sources = res.get("sources") or []
    lines.append("\n=== 交叉核验与来源 (Verification & Sources) ===")
    lines.append(
        f"核验状态 (grounded/passed): {ver.get('grounded', ver.get('passed'))} | "
        f"未落地数值 (ungrounded_numbers): {ver.get('ungrounded_numbers', [])}"
    )
    if ver.get("warnings"):
        lines.append(f"警告: {'; '.join(ver['warnings'])}")
    lines.append(f"数据来源 (Sources): {', '.join(sources) if sources else '无'}")
    lines.append(f"耗时: {res.get('duration_ms', 0)} ms")
    lines.append("\n=== 研究结论 (Answer) ===")
    lines.append(str(res.get("answer") or ""))
    return "\n".join(lines)


def _handle_ask(args: argparse.Namespace) -> int:
    prompt = str(args.prompt or "").strip()
    if not prompt:
        print("Error: prompt must be non-empty.", file=sys.stderr)
        return 2
    try:
        res = run_research_query(prompt=prompt)
    except Exception as exc:
        print(f"Error executing research query: {exc}", file=sys.stderr)
        return 1

    if args.json:
        _print_json(res)
    else:
        print(_format_ask_text(res))
    return 0


def _handle_ingest(args: argparse.Namespace) -> int:
    results: dict[str, Any] = {}
    try:
        if args.sector:
            results["sector"] = ingest_a_share_sector(sector=args.sector, days=args.days, force=args.force)
        if args.symbol:
            mkt = args.market.upper()
            if mkt == "US":
                results["yfinance"] = ingest_us_stock(symbol=args.symbol, period=args.period, force=args.force)
                if args.edgar:
                    results["sec_edgar"] = ingest_sec_edgar(symbol=args.symbol)
                if args.finnhub:
                    results["finnhub_news"] = ingest_finnhub_news(symbol=args.symbol, days=args.days)
            else:
                results["a_share_stock"] = ingest_a_share_stock(
                    symbol=args.symbol, days=args.days, force=args.force
                )
        else:
            if args.edgar:
                print("Error: --edgar requires --symbol", file=sys.stderr)
                return 2
            if args.finnhub:
                if args.from_date or args.to_date:
                    today = date.today()
                    f_dt = args.from_date or (today - timedelta(days=7)).isoformat()
                    t_dt = args.to_date or (today + timedelta(days=14)).isoformat()
                    results["finnhub_earnings"] = ingest_finnhub_earnings(from_date=f_dt, to_date=t_dt)
                else:
                    results["finnhub_news"] = ingest_finnhub_news(days=args.days)
        if not results:
            # Default ingestion target if neither --symbol nor --sector nor --finnhub given
            results["a_share_stock"] = ingest_a_share_stock(symbol="000858", days=args.days, force=args.force)
    except Exception as exc:
        print(f"Error during ingestion: {exc}", file=sys.stderr)
        return 1

    _print_json(results)
    return 0


def _handle_backtest(args: argparse.Namespace) -> int:
    mkt_u = str(args.market or "CN").strip().upper()
    symbols = [_normalize_symbol_for_market(s, mkt_u) for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("Error: --symbols must contain at least one symbol", file=sys.stderr)
        return 2
    try:
        ensure_builtin_strategies()
        with db_session() as con:
            ensure_baseline_seeded(con, seed_strategies=True)
            res = run_backtest(
                strategy_id=args.strategy_id,
                symbols=symbols,
                start_date=args.start_date,
                end_date=args.end_date,
                initial_capital=args.initial_capital,
                seed=args.seed,
                market=mkt_u,
                con=con,
            )
        _print_json(res.to_dict())
        return 0
    except Exception as exc:
        print(f"Error running backtest: {exc}", file=sys.stderr)
        return 1


def _lookup_latest_bar(
    symbol: str,
    market: str,
    *,
    trade_date: str | None = None,
) -> dict[str, float] | None:
    mkt_u = str(market or "CN").strip().upper()
    sym_u = _normalize_symbol_for_market(symbol, mkt_u)
    if not sym_u:
        return None
    end_dt = (
        trade_date.strip()[:10]
        if isinstance(trade_date, str) and len(trade_date.strip()) >= 10
        else None
    )
    try:
        with db_session() as con:
            ensure_baseline_seeded(con)
            bars = load_bars_from_duckdb([sym_u], end_date=end_dt, market=mkt_u, con=con)
            if not bars and end_dt:
                bars = load_bars_from_duckdb([sym_u], market=mkt_u, con=con)
        if bars:
            latest = bars[-1]
            prev_c = float(bars[-2]["close"]) if len(bars) > 1 else float(latest["close"])
            return {
                "open": float(latest["open"]),
                "high": float(latest["high"]),
                "low": float(latest["low"]),
                "close": float(latest["close"]),
                "prev_close": prev_c,
            }
    except Exception:
        pass
    return None


def _handle_paper(args: argparse.Namespace) -> int:
    action = (args.action or "status").lower()
    svc = PaperAccountService()
    try:
        if action == "status":
            summary = svc.get_account_summary()
            summary["orders"] = svc.get_orders(limit=10)
            _print_json(summary)
            return 0
        if action == "reset":
            res = svc.reset_account(initial_capital=args.capital, initial_date=args.date)
            _print_json(res)
            return 0
        if action == "advance":
            if not args.date:
                print("Error: --date YYYY-MM-DD is required for paper advance", file=sys.stderr)
                return 2
            marks = json.loads(args.prices) if args.prices else None
            res = svc.advance_day(args.date, mark_prices=marks)
            _print_json(res)
            return 0
        if action in ("buy", "sell"):
            if not args.symbol or args.quantity is None:
                print(f"Error: --symbol and --quantity are required for paper {action}", file=sys.stderr)
                return 2
            mkt_u = str(args.market or "CN").strip().upper()
            norm_sym = _normalize_symbol_for_market(args.symbol, mkt_u)
            ref_bar = _lookup_latest_bar(norm_sym, mkt_u, trade_date=args.date)
            res = svc.submit_order(
                symbol=norm_sym,
                side=action.upper(),
                quantity=args.quantity,
                order_type=args.order_type,
                limit_price=args.limit_price,
                market=mkt_u,
                trade_date=args.date,
                reference_bar=ref_bar,
                strategy_id=args.strategy_id,
            )
            _print_json(res)
            return 0 if res.get("accepted") else 1
        print(f"Error: unsupported paper action {action!r}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error in paper command: {exc}", file=sys.stderr)
        return 1


def _handle_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
        from stock_research.api.app import app
    except ImportError as exc:
        print(f"Error: Web API dependencies or stock_research.api.app not available: {exc}", file=sys.stderr)
        return 1
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stock_research.cli",
        description="JEVDB Stock Research & Paper-Trading Prototype CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="Run multi-turn research query via Pi coding agent + MCP")
    p_ask.add_argument("prompt", help="Research question or investment prompt")
    p_ask.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_ask.set_defaults(func=_handle_ask)

    p_ing = sub.add_parser("ingest", help="Fetch A-share/US market data into DuckDB")
    p_ing.add_argument("--symbol", default=None, help="Stock ticker/code (e.g. 000858, AAPL)")
    p_ing.add_argument("--sector", default=None, help="CN sector name (e.g. 电力)")
    p_ing.add_argument("--market", default="CN", choices=["CN", "US", "cn", "us"], help="Market (CN or US)")
    p_ing.add_argument("--days", type=int, default=30, help="Lookback window in days")
    p_ing.add_argument("--period", default="6mo", help="yfinance period (e.g. 1mo, 6mo)")
    p_ing.add_argument("--edgar", action="store_true", help="Also ingest SEC EDGAR filings/facts for US symbol")
    p_ing.add_argument("--finnhub", action="store_true", help="Also ingest Finnhub news or earnings calendar")
    p_ing.add_argument("--from-date", default=None, help="Finnhub earnings start date (YYYY-MM-DD)")
    p_ing.add_argument("--to-date", default=None, help="Finnhub earnings end date (YYYY-MM-DD)")
    p_ing.add_argument("--force", action="store_true", help="Bypass cache/freshness throttling")
    p_ing.set_defaults(func=_handle_ingest)

    p_bt = sub.add_parser("backtest", help="Run deterministic strategy backtest")
    p_bt.add_argument("--strategy-id", "--strategy", dest="strategy_id", default="cn_daily_momentum_sentiment", help="Strategy ID")
    p_bt.add_argument("--symbols", default="000858,601318", help="Comma-separated symbols")
    p_bt.add_argument("--start-date", default="2025-04-24", help="Start date (YYYY-MM-DD)")
    p_bt.add_argument("--end-date", default="2025-05-15", help="End date (YYYY-MM-DD)")
    p_bt.add_argument("--initial-capital", type=float, default=100000.0, help="Initial capital")
    p_bt.add_argument("--market", default="CN", choices=["CN", "US", "cn", "us"], help="Market (CN or US)")
    p_bt.add_argument("--seed", type=int, default=42, help="Deterministic seed")
    p_bt.set_defaults(func=_handle_backtest)

    p_pap = sub.add_parser("paper", help="Inspect or trade on persistent paper-trading ledger")
    p_pap.add_argument("action", nargs="?", default="status", choices=["status", "buy", "sell", "advance", "reset"])
    p_pap.add_argument("--symbol", default=None, help="Symbol for buy/sell")
    p_pap.add_argument("--quantity", type=int, default=None, help="Order quantity")
    p_pap.add_argument("--market", default="CN", choices=["CN", "US", "cn", "us"], help="Market (CN or US)")
    p_pap.add_argument("--order-type", default="market", choices=["market", "limit"], help="Order type")
    p_pap.add_argument("--limit-price", type=float, default=None, help="Limit price")
    p_pap.add_argument("--date", default=None, help="Trade date or advance/reset date (YYYY-MM-DD)")
    p_pap.add_argument("--prices", default=None, help="JSON map of mark prices for advance")
    p_pap.add_argument("--capital", type=float, default=100000.0, help="Initial capital for reset")
    p_pap.add_argument("--strategy-id", default=None, help="Optional strategy attribution")
    p_pap.set_defaults(func=_handle_paper)

    p_srv = sub.add_parser("serve", help="Start FastAPI Web server")
    p_srv.add_argument("--host", default="127.0.0.1", help="Bind host")
    p_srv.add_argument("--port", type=int, default=8000, help="Bind port")
    p_srv.set_defaults(func=_handle_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
