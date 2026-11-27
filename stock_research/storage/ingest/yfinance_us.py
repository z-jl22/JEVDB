"""Zero-key US market bars, fundamentals, and nested news ingestion via yfinance."""

from __future__ import annotations

import html
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from stock_research.storage.ingest import (
    make_event_id,
    make_keys,
    record_ingestion_run,
    utc_now_naive,
)
from stock_research.storage.schema import db_session

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _to_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    try:
        num = float(val)
        return num if math.isfinite(num) else None
    except (TypeError, ValueError):
        return None


def _to_date(val: Any) -> date | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, (int, float)) and not math.isfinite(float(val)):
        return None
    if isinstance(val, datetime):
        dt = val.date()
        return None if pd.isna(dt) else dt
    if isinstance(val, date):
        return val
    text = str(val).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null", "inf", "-inf"}:
        return None
    try:
        ts = pd.to_datetime(text)
        if pd.isna(ts):
            return None
        dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if isinstance(dt, datetime) and not pd.isna(dt):
            return dt.date()
        if isinstance(dt, date):
            return dt
    except Exception:
        return None
    return None


def _strip_html(text: str) -> str:
    cleaned = _HTML_TAG_RE.sub(" ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


def _parse_news_timestamp(raw: Any) -> datetime:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        ep_f = float(raw)
        if math.isfinite(ep_f) and 0.0 <= ep_f <= 4102444800.0:
            try:
                return datetime.fromtimestamp(ep_f, tz=timezone.utc).replace(tzinfo=None)
            except (OverflowError, OSError, ValueError):
                pass
        return utc_now_naive()
    text = str(raw or "").strip()
    if text:
        try:
            ts = pd.to_datetime(text, utc=True)
            if not pd.isna(ts):
                return ts.to_pydatetime().replace(tzinfo=None)
        except Exception:
            pass
    return utc_now_naive()


def _extract_statement_row(df: pd.DataFrame | None, row_names: tuple[str, ...], col: Any) -> float | None:
    if df is None or df.empty or col not in df.columns:
        return None
    for name in row_names:
        if name in df.index:
            val = _to_float(df.loc[name, col])
            if val is not None:
                return val
    return None


def ingest_us_stock(
    symbol: str,
    period: str = "6mo",
    db_path: str | Path | None = None,
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    include_fundamentals: bool = True,
    include_news: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Ingest US OHLCV bars, normalized fundamentals, and nested yfinance 1.x news."""
    del force
    started = utc_now_naive()
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("US symbol cannot be empty")

    errors: list[str] = []
    ticker = yf.Ticker(sym)
    now = utc_now_naive()

    # 1. History bars
    bar_rows: list[tuple[Any, ...]] = []
    try:
        if start_date is not None or end_date is not None:
            parsed_end = _to_date(end_date) if end_date is not None else date.today()
            end_dt = parsed_end or date.today()
            parsed_start = _to_date(start_date) if start_date is not None else (end_dt - timedelta(days=180))
            start_dt = (parsed_start or (end_dt - timedelta(days=180))) - timedelta(days=30)
            hist = ticker.history(
                start=start_dt.isoformat(),
                end=(end_dt + timedelta(days=1)).isoformat(),
                auto_adjust=False,
            )
        else:
            hist = ticker.history(period=period, auto_adjust=False)
        if hist is not None and not hist.empty:
            idx = hist.index
            if getattr(idx, "tz", None) is not None:
                idx = idx.tz_localize(None)
            for dt_idx, rec in zip(idx, hist.to_dict("records")):
                t_date = _to_date(dt_idx)
                open_p = _to_float(rec.get("Open"))
                high_p = _to_float(rec.get("High"))
                low_p = _to_float(rec.get("Low"))
                close_p = _to_float(rec.get("Close"))
                vol = _to_float(rec.get("Volume")) or 0.0
                if t_date is None or None in (open_p, high_p, low_p, close_p):
                    continue
                bar_rows.append(
                    (
                        sym,
                        "US",
                        t_date,
                        open_p,
                        high_p,
                        low_p,
                        close_p,
                        vol,
                        round(close_p * vol, 2),
                        "yfinance",
                    )
                )
    except Exception as exc:
        errors.append(f"history failed ({exc})")

    # 2. Asset metadata & valuation ratios
    info: dict[str, Any] = {}
    if include_fundamentals:
        try:
            raw_info = ticker.info
            if isinstance(raw_info, dict):
                info = raw_info
        except Exception as exc:
            errors.append(f"info failed ({exc})")

    name = str(info.get("shortName") or info.get("longName") or sym).strip()
    sector = str(info.get("sector") or "").strip()
    industry = str(info.get("industry") or "").strip()
    pe_ttm = _to_float(info.get("trailingPE"))
    pb = _to_float(info.get("priceToBook"))
    roe = _to_float(info.get("returnOnEquity"))
    debt_to_eq_pct = _to_float(info.get("debtToEquity"))
    fallback_debt_ratio = (debt_to_eq_pct / 100.0) if debt_to_eq_pct is not None else None

    # 3. Balance sheets (for Liabilities / Assets debt_ratio) & financial statements
    bs_ratios_by_date: dict[date, tuple[float | None, float | None, float | None]] = {}
    fund_rows: list[tuple[Any, ...]] = []
    if include_fundamentals:
        for bs_attr in ("balance_sheet", "quarterly_balance_sheet"):
            try:
                bs_df = getattr(ticker, bs_attr, None)
                if bs_df is None or bs_df.empty:
                    continue
                for col in bs_df.columns:
                    r_date = _to_date(col)
                    if r_date is None or r_date in bs_ratios_by_date:
                        continue
                    assets = _extract_statement_row(bs_df, ("Total Assets",), col)
                    liabilities = _extract_statement_row(
                        bs_df,
                        ("Total Liabilities Net Minority Interest", "Total Liabilities", "Total Liab"),
                        col,
                    )
                    ratio = (
                        (liabilities / assets)
                        if assets is not None and assets > 0 and liabilities is not None
                        else None
                    )
                    if assets is not None or liabilities is not None:
                        bs_ratios_by_date[r_date] = (assets, liabilities, ratio)
            except Exception as exc:
                errors.append(f"{bs_attr} failed ({exc})")

        latest_bs_ratio: float | None = None
        if bs_ratios_by_date:
            for dt_key in sorted(bs_ratios_by_date.keys(), reverse=True):
                if bs_ratios_by_date[dt_key][2] is not None:
                    latest_bs_ratio = bs_ratios_by_date[dt_key][2]
                    break

        seen_dates: set[date] = set()
        for stmt_attr in ("financials", "quarterly_financials"):
            try:
                stmt_df = getattr(ticker, stmt_attr)
                if stmt_df is None or stmt_df.empty:
                    continue
                for col in stmt_df.columns:
                    r_date = _to_date(col)
                    if r_date is None or r_date in seen_dates:
                        continue
                    rev = _extract_statement_row(stmt_df, ("Total Revenue", "Operating Revenue"), col)
                    ni = _extract_statement_row(
                        stmt_df, ("Net Income", "Net Income Common Stockholders"), col
                    )
                    op_inc = _extract_statement_row(stmt_df, ("Operating Income",), col)
                    ebitda = _extract_statement_row(stmt_df, ("EBITDA",), col)
                    if rev is None and ni is None:
                        continue
                    seen_dates.add(r_date)
                    is_latest = len(fund_rows) == 0
                    bs_assets, bs_liab, bs_ratio = bs_ratios_by_date.get(r_date, (None, None, None))
                    row_debt_ratio = bs_ratio
                    debt_basis = "liabilities_over_assets" if bs_ratio is not None else None
                    if row_debt_ratio is None and is_latest:
                        if latest_bs_ratio is not None:
                            row_debt_ratio = latest_bs_ratio
                            debt_basis = "liabilities_over_assets"
                        elif fallback_debt_ratio is not None:
                            row_debt_ratio = fallback_debt_ratio
                            debt_basis = "debt_to_equity_fallback"
                    fund_rows.append(
                        (
                            sym,
                            "US",
                            r_date,
                            pe_ttm if is_latest else None,
                            pb if is_latest else None,
                            roe if is_latest else None,
                            rev,
                            ni,
                            row_debt_ratio,
                            json.dumps(
                                {
                                    "operating_income": op_inc,
                                    "ebitda": ebitda,
                                    "statement": stmt_attr,
                                    "total_assets": bs_assets,
                                    "total_liabilities": bs_liab,
                                    "debt_ratio_basis": debt_basis,
                                    "debt_to_equity_pct": debt_to_eq_pct if is_latest else None,
                                },
                                ensure_ascii=False,
                            ),
                            "yfinance",
                            now,
                        )
                    )
            except Exception as exc:
                errors.append(f"{stmt_attr} failed ({exc})")

        effective_fallback_debt = latest_bs_ratio if latest_bs_ratio is not None else fallback_debt_ratio
        if not fund_rows and any(
            v is not None
            for v in (
                pe_ttm,
                pb,
                roe,
                effective_fallback_debt,
                _to_float(info.get("totalRevenue")),
                _to_float(info.get("netIncomeToCommon")),
            )
        ):
            fund_rows.append(
                (
                    sym,
                    "US",
                    date.today(),
                    pe_ttm,
                    pb,
                    roe,
                    _to_float(info.get("totalRevenue")),
                    _to_float(info.get("netIncomeToCommon")),
                    effective_fallback_debt,
                    json.dumps(
                        {
                            "debt_ratio_basis": (
                                "liabilities_over_assets"
                                if latest_bs_ratio is not None
                                else "debt_to_equity_fallback"
                            ),
                            "debt_to_equity_pct": debt_to_eq_pct,
                        },
                        ensure_ascii=False,
                    ),
                    "yfinance",
                    now,
                )
            )

    # 4. News (handling yfinance 1.x nested content object)
    news_rows: list[tuple[Any, ...]] = []
    if include_news:
        try:
            raw_news = ticker.news or []
            for item in raw_news:
                if not isinstance(item, dict):
                    continue
                content = item.get("content") if isinstance(item.get("content"), dict) else item
                title = str(content.get("title") or "").strip()
                raw_summary = content.get("summary") or content.get("description") or ""
                summary = _strip_html(str(raw_summary))
                pub_raw = (
                    content.get("pubDate")
                    or content.get("displayTime")
                    or item.get("providerPublishTime")
                )
                pub = _parse_news_timestamp(pub_raw)
                canon = content.get("canonicalUrl") if isinstance(content.get("canonicalUrl"), dict) else {}
                click = content.get("clickThroughUrl") if isinstance(content.get("clickThroughUrl"), dict) else {}
                url = str(canon.get("url") or click.get("url") or content.get("link") or "").strip()
                provider_obj = content.get("provider") if isinstance(content.get("provider"), dict) else {}
                provider = str(provider_obj.get("displayName") or item.get("publisher") or "Yahoo Finance").strip()
                if not title and not summary:
                    continue
                eid = make_event_id(
                    "yfinance", str(item.get("id") or content.get("id") or url or f"{sym}:{title}")
                )
                keys = make_keys("US", sym, name, sector, industry)
                news_rows.append(
                    (
                        eid,
                        sym,
                        "US",
                        sector,
                        pub,
                        title or summary[:80],
                        summary,
                        url,
                        f"yfinance:{provider}",
                        None,
                        json.dumps(keys, ensure_ascii=False),
                        keys,
                    )
                )
        except Exception as exc:
            errors.append(f"news failed ({exc})")

    bar_rows = list({(r[0], r[1], r[2], r[9]): r for r in bar_rows}.values())
    fund_rows = list({(r[0], r[1], r[2], r[10]): r for r in fund_rows}.values())
    news_rows = list({r[0]: r for r in news_rows}.values())

    rows_written = 0
    has_asset_meta = bool(
        info.get("shortName")
        or info.get("longName")
        or info.get("sector")
        or info.get("industry")
        or info.get("regularMarketPrice")
    )
    with db_session(db_path, init=True) as con:
        if bar_rows or fund_rows or news_rows or has_asset_meta:
            con.execute(
                """
                INSERT INTO assets (symbol, market, name, sector, industry, updated_at)
                VALUES (?, 'US', ?, ?, ?, ?)
                ON CONFLICT (symbol, market) DO UPDATE SET
                    name = COALESCE(NULLIF(excluded.name, ''), assets.name),
                    sector = COALESCE(NULLIF(excluded.sector, ''), assets.sector),
                    industry = COALESCE(NULLIF(excluded.industry, ''), assets.industry),
                    updated_at = excluded.updated_at
                """,
                [sym, name, sector, industry, now],
            )
        if bar_rows:
            con.executemany(
                """
                INSERT INTO market_bars (
                    symbol, market, trade_date, open, high, low, close, volume, amount, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, market, trade_date, source) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    amount = excluded.amount
                """,
                bar_rows,
            )
            rows_written += len(bar_rows)
        if fund_rows:
            con.executemany(
                """
                INSERT INTO fundamentals (
                    symbol, market, report_date, pe_ttm, pb, roe, revenue, net_income,
                    debt_ratio, metrics_json, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, market, report_date, source) DO UPDATE SET
                    pe_ttm = COALESCE(excluded.pe_ttm, fundamentals.pe_ttm),
                    pb = COALESCE(excluded.pb, fundamentals.pb),
                    roe = COALESCE(excluded.roe, fundamentals.roe),
                    revenue = COALESCE(excluded.revenue, fundamentals.revenue),
                    net_income = COALESCE(excluded.net_income, fundamentals.net_income),
                    debt_ratio = COALESCE(excluded.debt_ratio, fundamentals.debt_ratio),
                    metrics_json = excluded.metrics_json,
                    updated_at = excluded.updated_at
                """,
                fund_rows,
            )
            rows_written += len(fund_rows)
        if news_rows:
            con.executemany(
                """
                INSERT INTO news_events (
                    event_id, symbol, market, sector, published_at, title, summary,
                    url, source, sentiment_label, keywords_json, keys
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (event_id) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    url = excluded.url,
                    keywords_json = excluded.keywords_json,
                    keys = excluded.keys
                """,
                news_rows,
            )
            rows_written += len(news_rows)

    status = "success" if not errors else ("partial" if rows_written > 0 else "error")
    err_joined = "; ".join(errors) if errors else None
    run_id = record_ingestion_run(
        db_path,
        source="yfinance",
        target=sym,
        status=status,
        rows_written=rows_written,
        started_at=started,
        error_msg=err_joined,
    )
    return {
        "run_id": run_id,
        "symbol": sym,
        "status": status,
        "bars": len(bar_rows),
        "fundamentals": len(fund_rows),
        "news": len(news_rows),
        "rows_written": rows_written,
        "errors": errors,
    }
