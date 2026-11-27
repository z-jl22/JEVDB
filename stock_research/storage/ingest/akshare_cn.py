"""Zero-key China A-share multi-source ingestion via AkShare (avoiding push2)."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd

from stock_research.storage.ingest import (
    make_event_id,
    make_keys,
    read_json_cache,
    record_ingestion_run,
    retry_call,
    utc_now_naive,
    write_json_cache,
)
from stock_research.storage.schema import db_session

_KNOWN_SECTORS: dict[str, dict[str, str]] = {
    "电力": {"ths_name": "电力", "sina_name": "电力行业", "sina_label": "new_dlhy"},
    "电力行业": {"ths_name": "电力", "sina_name": "电力行业", "sina_label": "new_dlhy"},
    "new_dlhy": {"ths_name": "电力", "sina_name": "电力行业", "sina_label": "new_dlhy"},
    "银行": {"ths_name": "银行", "sina_name": "金融行业", "sina_label": "new_jrhy"},
    "半导体": {"ths_name": "半导体", "sina_name": "电子器件", "sina_label": "new_dzqj"},
    "白酒": {"ths_name": "白酒", "sina_name": "酿酒行业", "sina_label": "new_ljhy"},
    "医药": {"ths_name": "化学制药", "sina_name": "生物制药", "sina_label": "new_swzy"},
}

_POWER_SECTOR_KEYWORDS = (
    "电力",
    "电网",
    "发电",
    "火电",
    "水电",
    "核电",
    "绿电",
    "风电",
    "光伏",
    "储能",
    "煤电",
    "电价",
)


def _to_float(val: Any) -> float | None:
    if val is None or val == "" or (isinstance(val, float) and math.isnan(val)):
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    try:
        num = float(str(val).replace(",", "").replace("%", "").strip())
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
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass
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


def _to_timestamp(val: Any) -> datetime:
    if val is None or isinstance(val, bool):
        return utc_now_naive()
    try:
        if pd.isna(val):
            return utc_now_naive()
    except (TypeError, ValueError):
        pass
    if isinstance(val, (int, float)) and not math.isfinite(float(val)):
        return utc_now_naive()
    if isinstance(val, datetime):
        return val.replace(tzinfo=None)
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    text = str(val).strip()
    if text and text.lower() not in {"nan", "nat", "none", "null", "inf", "-inf"}:
        try:
            ts = pd.to_datetime(text)
            if not pd.isna(ts):
                dt = ts.to_pydatetime()
                if isinstance(dt, datetime) and not pd.isna(dt):
                    return dt.replace(tzinfo=None)
        except Exception:
            pass
    return utc_now_naive()


def normalize_cn_symbol(symbol: str) -> tuple[str, str, str]:
    """Normalize any A-share ticker format to `(code6, prefixed_lower, dot_upper)`."""
    raw = str(symbol or "").strip()
    if not raw:
        raise ValueError("Symbol cannot be empty")
    m = re.fullmatch(
        r"(?:(?:sh|sz|bj)?(\d{6})|(\d{6})\.(?:sh|sz|bj))",
        raw,
        flags=re.IGNORECASE,
    )
    if not m:
        raise ValueError(f"Invalid A-share symbol: {symbol!r}")
    code6 = m.group(1) or m.group(2)

    lower = raw.lower()
    if lower.startswith(("sh", "sz", "bj")):
        exch = lower[:2]
    elif lower.endswith((".sh", ".sz", ".bj")):
        exch = lower[-2:]
    elif code6.startswith(("6", "90", "5")):
        exch = "sh"
    elif code6.startswith(("4", "8", "92")):
        exch = "bj"
    else:
        exch = "sz"

    return code6, f"{exch}{code6}", f"{code6}.{exch.upper()}"


def resolve_cn_sector(sector: str) -> dict[str, str]:
    """Resolve an A-share sector query to THS name and Sina sector label/name."""
    raw = str(sector or "").strip()
    if not raw:
        raise ValueError("Sector cannot be empty")
    if raw in _KNOWN_SECTORS:
        return dict(_KNOWN_SECTORS[raw])

    ths_name = raw.removesuffix("行业") if raw.endswith("行业") else raw
    sina_name = raw if raw.endswith("行业") else f"{raw}行业"
    sina_label = ""

    try:
        ths_df = retry_call(ak.stock_board_industry_summary_ths, retries=1, backoff=0.5)
        if ths_df is not None and not ths_df.empty and "板块" in ths_df.columns:
            names = [str(x).strip() for x in ths_df["板块"].tolist()]
            if raw in names:
                ths_name = raw
            elif ths_name in names:
                pass
            else:
                match = next((n for n in names if ths_name in n or n in raw), None)
                if match:
                    ths_name = match
    except Exception:
        pass

    try:
        sina_df = retry_call(
            lambda: ak.stock_sector_spot(indicator="新浪行业"), retries=1, backoff=0.5
        )
        if sina_df is not None and not sina_df.empty:
            for _, row in sina_df.iterrows():
                lbl = str(row.get("label") or "").strip()
                name = str(row.get("板块") or "").strip()
                if raw in (lbl, name) or ths_name == name.removesuffix("行业"):
                    sina_label = lbl
                    sina_name = name
                    break
            if not sina_label:
                for _, row in sina_df.iterrows():
                    lbl = str(row.get("label") or "").strip()
                    name = str(row.get("板块") or "").strip()
                    if ths_name in name or name in raw:
                        sina_label = lbl
                        sina_name = name
                        break
    except Exception:
        pass

    return {
        "ths_name": ths_name,
        "sina_name": sina_name,
        "sina_label": sina_label,
    }


def _fetch_a_share_bars(
    symbol: str,
    *,
    days: int = 120,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
) -> tuple[list[tuple[Any, ...]], str, str | None]:
    """Fetch daily OHLCV bars via Tencent primary with Sina fallback (no push2)."""
    code6, prefixed, _ = normalize_cn_symbol(symbol)
    explicit_range = start_date is not None or end_date is not None
    parsed_end = _to_date(end_date) if end_date is not None else None
    parsed_start = _to_date(start_date) if start_date is not None else None
    end_dt = parsed_end or date.today()
    if parsed_start is not None:
        # Include 30 calendar days of warmup bars before start_date for indicator calculation
        start_dt = parsed_start - timedelta(days=30)
    else:
        start_dt = end_dt - timedelta(days=max(days * 2, 45))
    start_str = start_dt.strftime("%Y%m%d")
    end_str = end_dt.strftime("%Y%m%d")

    df: pd.DataFrame | None = None
    source = "akshare_tx"
    err_msg: str | None = None

    try:
        df = retry_call(
            lambda: ak.stock_zh_a_hist_tx(
                symbol=prefixed, start_date=start_str, end_date=end_str, adjust=""
            ),
            retries=1,
            backoff=0.6,
        )
        if df is None or df.empty:
            raise ValueError(f"Empty Tencent bars for {prefixed}")
    except Exception as tx_exc:
        err_msg = f"tx failed ({tx_exc})"
        source = "akshare_sina"
        try:
            df = retry_call(
                lambda: ak.stock_zh_a_hist_sina(
                    symbol=prefixed, start_date=start_str, end_date=end_str, adjust=""
                ),
                retries=1,
                backoff=0.8,
            )
            if df is None or df.empty:
                raise ValueError(f"Empty Sina bars for {prefixed}")
            err_msg = None
        except Exception as sina_exc:
            return [], source, f"{err_msg}; sina failed ({sina_exc})"

    rows: list[tuple[Any, ...]] = []
    # Keep last `days` trading days only when fetching by relative day count
    if not explicit_range and len(df) > days:
        df = df.tail(days)
    for _, rec in df.iterrows():
        t_date = _to_date(rec.get("date"))
        open_p = _to_float(rec.get("open"))
        high_p = _to_float(rec.get("high"))
        low_p = _to_float(rec.get("low"))
        close_p = _to_float(rec.get("close"))
        vol = _to_float(rec.get("volume")) or 0.0
        amt = _to_float(rec.get("amount"))
        if t_date is None or None in (open_p, high_p, low_p, close_p):
            continue
        if amt is None:
            amt = round(close_p * vol, 2)
        rows.append(
            (
                code6,
                "CN",
                t_date,
                open_p,
                high_p,
                low_p,
                close_p,
                vol,
                amt,
                source,
            )
        )
    return rows, source, err_msg


def _fetch_a_share_fundamentals(
    symbol: str,
) -> tuple[list[tuple[Any, ...]], str | None, str | None]:
    """Fetch A-share fundamentals from EM datacenter + Baidu valuation, with THS fallback."""
    code6, _, dot_upper = normalize_cn_symbol(symbol)
    now = utc_now_naive()
    pe_ttm: float | None = None
    pb_val: float | None = None
    stock_name: str | None = None

    try:
        pe_df = retry_call(
            lambda: ak.stock_zh_valuation_baidu(
                symbol=code6, indicator="市盈率(TTM)", period="近一年"
            ),
            retries=1,
            backoff=0.5,
        )
        if pe_df is not None and not pe_df.empty:
            pe_ttm = _to_float(pe_df.iloc[-1].get("value"))
    except Exception:
        pass

    try:
        pb_df = retry_call(
            lambda: ak.stock_zh_valuation_baidu(
                symbol=code6, indicator="市净率", period="近一年"
            ),
            retries=1,
            backoff=0.5,
        )
        if pb_df is not None and not pb_df.empty:
            pb_val = _to_float(pb_df.iloc[-1].get("value"))
    except Exception:
        pass

    rows: list[tuple[Any, ...]] = []
    err_msg: str | None = None
    try:
        em_df = retry_call(
            lambda: ak.stock_financial_analysis_indicator_em(
                symbol=dot_upper, indicator="按报告期"
            ),
            retries=1,
            backoff=0.6,
        )
        if em_df is not None and not em_df.empty:
            for _, rec in em_df.head(8).iterrows():
                r_date = _to_date(rec.get("REPORT_DATE"))
                if r_date is None:
                    continue
                if not stock_name:
                    stock_name = str(rec.get("SECURITY_NAME_ABBR") or "").strip() or None
                roe_pct = _to_float(rec.get("ROEJQ"))
                debt_pct = _to_float(rec.get("ZCFZL"))
                rev = _to_float(rec.get("TOTALOPERATEREVE"))
                ni = _to_float(rec.get("PARENTNETPROFIT"))
                roe_dec = (roe_pct / 100.0) if roe_pct is not None else None
                debt_dec = (debt_pct / 100.0) if debt_pct is not None else None
                metrics = {
                    "report_type": str(rec.get("REPORT_TYPE") or ""),
                    "eps_jb": _to_float(rec.get("EPSJB")),
                    "bps": _to_float(rec.get("BPS")),
                    "roe_pct": roe_pct,
                    "debt_ratio_pct": debt_pct,
                }
                is_first = not rows
                rows.append(
                    (
                        code6,
                        "CN",
                        r_date,
                        pe_ttm if is_first else None,
                        pb_val if is_first else None,
                        roe_dec,
                        rev,
                        ni,
                        debt_dec,
                        json.dumps(metrics, ensure_ascii=False),
                        "akshare_em_datacenter",
                        now,
                    )
                )
    except Exception as exc:
        err_msg = f"em_datacenter failed ({exc})"

    if not rows:
        try:
            ths_df = retry_call(
                lambda: ak.stock_financial_key_indicator_ths(
                    symbol=code6, indicator="按报告期"
                ),
                retries=1,
                backoff=0.6,
            )
            if ths_df is not None and not ths_df.empty:
                by_date: dict[date, dict[str, Any]] = {}
                for _, rec in ths_df.iterrows():
                    r_date = _to_date(rec.get("report_date"))
                    if r_date is None:
                        continue
                    bucket = by_date.setdefault(r_date, {})
                    m_name = str(rec.get("metric_name") or "").strip()
                    m_val = _to_float(rec.get("value"))
                    if m_name and m_val is not None:
                        bucket[m_name] = m_val
                for i, r_date in enumerate(sorted(by_date.keys(), reverse=True)[:8]):
                    m = by_date[r_date]
                    roe_raw = m.get("净资产收益率") or m.get("ROE")
                    debt_raw = m.get("资产负债率")
                    rows.append(
                        (
                            code6,
                            "CN",
                            r_date,
                            pe_ttm if i == 0 else None,
                            pb_val if i == 0 else None,
                            (roe_raw / 100.0) if roe_raw is not None else None,
                            m.get("营业总收入") or m.get("营业收入"),
                            m.get("归母净利润") or m.get("净利润"),
                            (debt_raw / 100.0) if debt_raw is not None else None,
                            json.dumps(m, ensure_ascii=False),
                            "akshare_ths",
                            now,
                        )
                    )
                err_msg = None
        except Exception as ths_exc:
            err_msg = f"{err_msg or ''}; ths failed ({ths_exc})".strip("; ")

    if not rows and (pe_ttm is not None or pb_val is not None):
        rows.append(
            (
                code6,
                "CN",
                date.today(),
                pe_ttm,
                pb_val,
                None,
                None,
                None,
                None,
                json.dumps({"valuation_only": True}),
                "akshare_baidu",
                now,
            )
        )

    return rows, stock_name, err_msg


def _fetch_stock_news_em(
    symbol: str, *, stock_name: str = "", sector: str = ""
) -> tuple[list[tuple[Any, ...]], str | None]:
    code6, _, _ = normalize_cn_symbol(symbol)
    rows: list[tuple[Any, ...]] = []
    try:
        df = retry_call(lambda: ak.stock_news_em(symbol=code6), retries=1, backoff=0.5)
        if df is None or df.empty:
            return [], None
        for _, rec in df.iterrows():
            title = str(rec.get("新闻标题") or "").strip()
            summary = str(rec.get("新闻内容") or "").strip()
            url = str(rec.get("新闻链接") or "").strip()
            pub = _to_timestamp(rec.get("发布时间"))
            src = str(rec.get("文章来源") or "eastmoney").strip()
            if not title and not summary:
                continue
            eid = make_event_id("akshare_news_em", url or f"{code6}:{title}:{pub.isoformat()}")
            keys = make_keys("CN", code6, stock_name, sector, str(rec.get("关键词") or ""))
            rows.append(
                (
                    eid,
                    code6,
                    "CN",
                    sector,
                    pub,
                    title or summary[:80],
                    summary,
                    url,
                    f"akshare_em:{src}",
                    None,
                    json.dumps(keys, ensure_ascii=False),
                    keys,
                )
            )
        return rows, None
    except Exception as exc:
        return [], f"stock_news_em failed ({exc})"


def ingest_a_share_stock(
    symbol: str,
    days: int = 120,
    db_path: str | Path | None = None,
    *,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    include_fundamentals: bool = True,
    include_news: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Ingest A-share daily bars, fundamentals, and news into DuckDB without push2."""
    del force  # reserved for future TTL short-circuiting
    started = utc_now_naive()
    code6, _, _ = normalize_cn_symbol(symbol)
    errors: list[str] = []

    bar_rows, bar_source, bar_err = _fetch_a_share_bars(
        code6, days=days, start_date=start_date, end_date=end_date
    )
    if bar_err:
        errors.append(bar_err)

    fund_rows: list[tuple[Any, ...]] = []
    stock_name: str | None = None
    if include_fundamentals:
        fund_rows, stock_name, fund_err = _fetch_a_share_fundamentals(code6)
        if fund_err:
            errors.append(fund_err)

    news_rows: list[tuple[Any, ...]] = []
    if include_news:
        news_rows, news_err = _fetch_stock_news_em(code6, stock_name=stock_name or "")
        if news_err:
            errors.append(news_err)

    bar_rows = list({(r[0], r[1], r[2], r[9]): r for r in bar_rows}.values())
    fund_rows = list({(r[0], r[1], r[2], r[10]): r for r in fund_rows}.values())
    news_rows = list({r[0]: r for r in news_rows}.values())

    now = utc_now_naive()
    rows_written = 0
    with db_session(db_path, init=True) as con:
        if bar_rows or fund_rows or news_rows or stock_name:
            con.execute(
                """
                INSERT INTO assets (symbol, market, name, sector, industry, updated_at)
                VALUES (?, 'CN', ?, '', '', ?)
                ON CONFLICT (symbol, market) DO UPDATE SET
                    name = COALESCE(NULLIF(excluded.name, ''), assets.name),
                    updated_at = excluded.updated_at
                """,
                [code6, stock_name or "", now],
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
        source="akshare_cn",
        target=f"stock:{code6}",
        status=status,
        rows_written=rows_written,
        started_at=started,
        error_msg=err_joined,
    )
    return {
        "run_id": run_id,
        "symbol": code6,
        "status": status,
        "bar_source": bar_source,
        "bars": len(bar_rows),
        "fundamentals": len(fund_rows),
        "news": len(news_rows),
        "rows_written": rows_written,
        "errors": errors,
    }


_SECTOR_CONSTITUENTS_FRESH_TTL = 6 * 3600
_SECTOR_CONSTITUENTS_STALE_TTL = 30 * 24 * 3600


def _append_constituent_records(
    records: list[dict[str, Any]],
    *,
    ths_name: str,
    sina_name: str,
    now: datetime,
    source_tag: str,
    constituent_rows: list[tuple[Any, ...]],
    asset_rows: list[tuple[Any, ...]],
    ranked_symbols: list[tuple[str, str, float]],
) -> None:
    for item in records:
        raw_sym = str(item.get("symbol") or "").strip()
        if not raw_sym:
            continue
        try:
            code6, _, _ = normalize_cn_symbol(raw_sym)
        except ValueError:
            continue
        s_name = str(item.get("name") or "").strip()
        price = _to_float(item.get("price"))
        chg = _to_float(item.get("change_pct"))
        pe = _to_float(item.get("pe"))
        pb = _to_float(item.get("pb"))
        mktcap_rmb = _to_float(item.get("market_cap"))
        rank_score = _to_float(item.get("rank_weight"))
        if rank_score is None:
            rank_score = mktcap_rmb or 0.0
        constituent_rows.append(
            (
                ths_name,
                code6,
                s_name,
                price,
                chg,
                pe,
                pb,
                mktcap_rmb,
                now,
                source_tag,
            )
        )
        asset_rows.append((code6, "CN", s_name, ths_name, sina_name, now))
        ranked_symbols.append((code6, s_name, rank_score))


def _fetch_sw_sector_constituents(ths_name: str) -> list[dict[str, Any]]:
    """Secondary non-push2 sector constituent lookup via Shenwan index components."""
    target = ths_name.removesuffix("行业").strip()
    sw_code = ""
    for fn_name in ("index_second_info_sw", "index_first_info_sw"):
        fn = getattr(ak, fn_name, None)
        if fn is None:
            continue
        df = retry_call(fn, retries=1, backoff=0.8)
        if df is None or df.empty or "行业名称" not in df.columns:
            continue
        names = df["行业名称"].astype(str).str.strip()
        exact = df[names == target]
        if exact.empty:
            exact = df[names.str.contains(target, regex=False, na=False)]
        if not exact.empty:
            sw_code = str(exact.iloc[0].get("行业代码") or "").split(".")[0].strip()
            if sw_code:
                break
    if not sw_code:
        return []
    comp_fn = getattr(ak, "index_component_sw", None)
    if comp_fn is None:
        return []
    comp_df = retry_call(lambda: comp_fn(symbol=sw_code), retries=1, backoff=0.8)
    if comp_df is None or comp_df.empty:
        return []
    records: list[dict[str, Any]] = []
    for _, rec in comp_df.iterrows():
        raw_sym = str(rec.get("证券代码") or rec.get("symbol") or "").strip()
        if not raw_sym:
            continue
        try:
            code6, _, _ = normalize_cn_symbol(raw_sym)
        except ValueError:
            continue
        s_name = str(rec.get("证券名称") or rec.get("name") or "").strip()
        weight = _to_float(rec.get("最新权重")) or 0.0
        records.append(
            {
                "symbol": code6,
                "name": s_name,
                "price": None,
                "change_pct": None,
                "pe": None,
                "pb": None,
                "market_cap": None,
                "rank_weight": weight,
            }
        )
    return records


def ingest_a_share_sector(
    sector: str = "电力",
    days: int = 30,
    db_path: str | Path | None = None,
    *,
    top_constituents: int = 5,
    force: bool = False,
) -> dict[str, Any]:
    """Ingest A-share sector index snapshots, constituents, top constituent bars, and sector news."""
    started = utc_now_naive()
    resolved = resolve_cn_sector(sector)
    ths_name = resolved["ths_name"]
    sina_name = resolved["sina_name"]
    sina_label = resolved["sina_label"]
    errors: list[str] = []

    snap_rows: list[tuple[Any, ...]] = []
    constituent_rows: list[tuple[Any, ...]] = []
    asset_rows: list[tuple[Any, ...]] = []
    bar_rows: list[tuple[Any, ...]] = []
    news_rows: list[tuple[Any, ...]] = []

    end_dt = date.today()
    start_dt = end_dt - timedelta(days=max(days * 2, 30))
    start_str = start_dt.strftime("%Y%m%d")
    end_str = end_dt.strftime("%Y%m%d")
    now = utc_now_naive()

    # 1. THS summary & historical sector index
    ths_leader = ""
    try:
        sum_df = retry_call(ak.stock_board_industry_summary_ths, retries=1, backoff=0.5)
        if sum_df is not None and not sum_df.empty and "板块" in sum_df.columns:
            match_df = sum_df[sum_df["板块"].astype(str).str.strip() == ths_name]
            if not match_df.empty:
                rec = match_df.iloc[0]
                ths_leader = str(rec.get("领涨股") or "").strip()
                snap_rows.append(
                    (
                        "CN",
                        ths_name,
                        sina_label or ths_name,
                        end_dt,
                        _to_float(rec.get("涨跌幅")),
                        _to_float(rec.get("总成交量")),
                        _to_float(rec.get("总成交额")),
                        ths_leader,
                        json.dumps(
                            {
                                "net_inflow_100m": _to_float(rec.get("净流入")),
                                "up_count": _to_float(rec.get("上涨家数")),
                                "down_count": _to_float(rec.get("下跌家数")),
                                "avg_price": _to_float(rec.get("均价")),
                                "leader_price": _to_float(rec.get("领涨股-最新价")),
                                "leader_chg_pct": _to_float(rec.get("领涨股-涨跌幅")),
                            },
                            ensure_ascii=False,
                        ),
                        "akshare_ths_summary",
                    )
                )
    except Exception as exc:
        errors.append(f"ths_summary failed ({exc})")

    try:
        idx_df = retry_call(
            lambda: ak.stock_board_industry_index_ths(
                symbol=ths_name, start_date=start_str, end_date=end_str
            ),
            retries=1,
            backoff=0.6,
        )
        if idx_df is not None and not idx_df.empty:
            if len(idx_df) > days:
                idx_df = idx_df.tail(days)
            prev_close: float | None = None
            for _, rec in idx_df.iterrows():
                t_date = _to_date(rec.get("日期"))
                close_p = _to_float(rec.get("收盘价"))
                open_p = _to_float(rec.get("开盘价"))
                if t_date is None or close_p is None:
                    continue
                chg_pct = (
                    ((close_p - prev_close) / prev_close) * 100.0
                    if prev_close and prev_close > 0
                    else (
                        ((close_p - open_p) / open_p) * 100.0
                        if open_p and open_p > 0
                        else None
                    )
                )
                prev_close = close_p
                snap_rows.append(
                    (
                        "CN",
                        ths_name,
                        sina_label or ths_name,
                        t_date,
                        chg_pct,
                        _to_float(rec.get("成交量")),
                        _to_float(rec.get("成交额")),
                        ths_leader,
                        json.dumps(
                            {
                                "open": open_p,
                                "high": _to_float(rec.get("最高价")),
                                "low": _to_float(rec.get("最低价")),
                                "close": close_p,
                            },
                            ensure_ascii=False,
                        ),
                        "akshare_ths_index",
                    )
                )
    except Exception as exc:
        errors.append(f"ths_index failed ({exc})")

    # 2. Sina sector spot + constituents (with fresh/stale JSON cache, DuckDB & Shenwan fallback)
    ranked_symbols: list[tuple[str, str, float]] = []
    safe_cache_key = re.sub(r"[^a-zA-Z0-9_-]+", "_", sina_label or ths_name).strip("_") or "sector"
    constituent_cache_name = f"cn_sector_constituents_{safe_cache_key}.json"

    if not force:
        fresh_cached = read_json_cache(constituent_cache_name, _SECTOR_CONSTITUENTS_FRESH_TTL)
        if isinstance(fresh_cached, list) and fresh_cached:
            _append_constituent_records(
                fresh_cached,
                ths_name=ths_name,
                sina_name=sina_name,
                now=now,
                source_tag="akshare_sina_cache",
                constituent_rows=constituent_rows,
                asset_rows=asset_rows,
                ranked_symbols=ranked_symbols,
            )

    if not ranked_symbols and sina_label:
        try:
            detail_df = retry_call(
                lambda: ak.stock_sector_detail(sector=sina_label), retries=2, backoff=1.5
            )
            if detail_df is not None and not detail_df.empty:
                live_records: list[dict[str, Any]] = []
                for _, rec in detail_df.iterrows():
                    raw_sym = str(rec.get("code") or rec.get("symbol") or "").strip()
                    if not raw_sym:
                        continue
                    try:
                        code6, _, _ = normalize_cn_symbol(raw_sym)
                    except ValueError:
                        continue
                    mktcap_wan = _to_float(rec.get("mktcap"))
                    mktcap_rmb = (mktcap_wan * 10000.0) if mktcap_wan is not None else None
                    live_records.append(
                        {
                            "symbol": code6,
                            "name": str(rec.get("name") or "").strip(),
                            "price": _to_float(rec.get("trade")),
                            "change_pct": _to_float(rec.get("changepercent")),
                            "pe": _to_float(rec.get("per")),
                            "pb": _to_float(rec.get("pb")),
                            "market_cap": mktcap_rmb,
                        }
                    )
                if live_records:
                    write_json_cache(constituent_cache_name, live_records)
                    _append_constituent_records(
                        live_records,
                        ths_name=ths_name,
                        sina_name=sina_name,
                        now=now,
                        source_tag="akshare_sina",
                        constituent_rows=constituent_rows,
                        asset_rows=asset_rows,
                        ranked_symbols=ranked_symbols,
                    )
        except Exception as exc:
            errors.append(f"sina_detail failed ({exc})")

    if not ranked_symbols:
        stale_cached = read_json_cache(constituent_cache_name, _SECTOR_CONSTITUENTS_STALE_TTL)
        if isinstance(stale_cached, list) and stale_cached:
            _append_constituent_records(
                stale_cached,
                ths_name=ths_name,
                sina_name=sina_name,
                now=now,
                source_tag="akshare_sina_cache",
                constituent_rows=constituent_rows,
                asset_rows=asset_rows,
                ranked_symbols=ranked_symbols,
            )

    if not ranked_symbols:
        try:
            with db_session(db_path, init=True) as con:
                db_rows = con.execute(
                    """
                    SELECT symbol, name, price, change_pct, pe, pb, market_cap
                    FROM sector_constituents
                    WHERE sector_name = ? AND source <> 'akshare_db_cache'
                    ORDER BY COALESCE(market_cap, 0) DESC, symbol ASC
                    """,
                    [ths_name],
                ).fetchall()
            if db_rows:
                seen_db_syms: set[str] = set()
                db_records: list[dict[str, Any]] = []
                for sym_val, name_val, px, chg, pe_v, pb_v, mc in db_rows:
                    if sym_val in seen_db_syms:
                        continue
                    seen_db_syms.add(sym_val)
                    db_records.append(
                        {
                            "symbol": sym_val,
                            "name": name_val,
                            "price": px,
                            "change_pct": chg,
                            "pe": pe_v,
                            "pb": pb_v,
                            "market_cap": mc,
                        }
                    )
                _append_constituent_records(
                    db_records,
                    ths_name=ths_name,
                    sina_name=sina_name,
                    now=now,
                    source_tag="akshare_db_cache",
                    constituent_rows=constituent_rows,
                    asset_rows=asset_rows,
                    ranked_symbols=ranked_symbols,
                )
        except Exception as exc:
            errors.append(f"duckdb_constituents_fallback failed ({exc})")

    if not ranked_symbols:
        try:
            sw_records = _fetch_sw_sector_constituents(ths_name)
            if sw_records:
                _append_constituent_records(
                    sw_records,
                    ths_name=ths_name,
                    sina_name=sina_name,
                    now=now,
                    source_tag="akshare_sw",
                    constituent_rows=constituent_rows,
                    asset_rows=asset_rows,
                    ranked_symbols=ranked_symbols,
                )
        except Exception as exc:
            errors.append(f"sw_constituents failed ({exc})")

    # 3. Fetch daily bars & stock news for top N constituents by market cap
    ranked_symbols.sort(key=lambda item: item[2], reverse=True)
    top_stocks = ranked_symbols[: max(0, top_constituents)]
    for code6, s_name, _ in top_stocks:
        b_rows, _, b_err = _fetch_a_share_bars(code6, days=days)
        if b_rows:
            bar_rows.extend(b_rows)
        elif b_err:
            errors.append(f"bars({code6}): {b_err}")

    for code6, s_name, _ in top_stocks[:3]:
        s_news, n_err = _fetch_stock_news_em(code6, stock_name=s_name, sector=ths_name)
        if s_news:
            news_rows.extend(s_news)
        elif n_err:
            errors.append(f"news({code6}): {n_err}")

    # 4. Sector-relevant CLS & Sina global news
    match_tokens = [ths_name, ths_name.removesuffix("行业"), sina_name]
    if "电" in ths_name:
        match_tokens.extend(_POWER_SECTOR_KEYWORDS)
    match_tokens.extend([name for _, name, _ in top_stocks[:8] if name])
    match_tokens = [t for t in dict.fromkeys(match_tokens) if t]

    try:
        cls_df = retry_call(
            lambda: ak.stock_news_global_cls(symbol="全部"), retries=1, backoff=0.5
        )
        if cls_df is not None and not cls_df.empty:
            for _, rec in cls_df.iterrows():
                title = str(rec.get("标题") or "").strip()
                content = str(rec.get("内容") or "").strip()
                combined = f"{title} {content}"
                if not any(tok in combined for tok in match_tokens):
                    continue
                d_part = _to_date(rec.get("发布日期")) or date.today()
                t_part = str(rec.get("发布时间") or "00:00:00").strip()
                pub = _to_timestamp(f"{d_part.isoformat()} {t_part}")
                eid = make_event_id("akshare_cls", f"{pub.isoformat()}:{content[:120]}")
                keys = make_keys("CN", ths_name, sina_name, "CLS")
                news_rows.append(
                    (
                        eid,
                        "",
                        "CN",
                        ths_name,
                        pub,
                        title or content[:80],
                        content,
                        "",
                        "akshare_cls",
                        None,
                        json.dumps(keys, ensure_ascii=False),
                        keys,
                    )
                )
    except Exception as exc:
        errors.append(f"cls_news failed ({exc})")

    try:
        sina_news_df = retry_call(ak.stock_news_global_sina, retries=1, backoff=0.5)
        if sina_news_df is not None and not sina_news_df.empty:
            for _, rec in sina_news_df.iterrows():
                content = str(rec.get("内容") or "").strip()
                if not any(tok in content for tok in match_tokens):
                    continue
                pub = _to_timestamp(rec.get("时间"))
                eid = make_event_id("akshare_sina_global", f"{pub.isoformat()}:{content[:120]}")
                keys = make_keys("CN", ths_name, sina_name, "Sina")
                news_rows.append(
                    (
                        eid,
                        "",
                        "CN",
                        ths_name,
                        pub,
                        content[:80],
                        content,
                        "",
                        "akshare_sina_news",
                        None,
                        json.dumps(keys, ensure_ascii=False),
                        keys,
                    )
                )
    except Exception as exc:
        errors.append(f"sina_global_news failed ({exc})")

    # Deduplicate batch rows by primary key before DB executemany
    asset_rows = list({(r[0], r[1]): r for r in asset_rows}.values())
    snap_rows = list({(r[0], r[1], r[3], r[9]): r for r in snap_rows}.values())
    constituent_rows = list({(r[0], r[1], r[9]): r for r in constituent_rows}.values())
    bar_rows = list({(r[0], r[1], r[2], r[9]): r for r in bar_rows}.values())
    dedup_news = list({row[0]: row for row in news_rows}.values())

    rows_written = 0
    with db_session(db_path, init=True) as con:
        if asset_rows:
            con.executemany(
                """
                INSERT INTO assets (symbol, market, name, sector, industry, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, market) DO UPDATE SET
                    name = COALESCE(NULLIF(excluded.name, ''), assets.name),
                    sector = COALESCE(NULLIF(excluded.sector, ''), assets.sector),
                    industry = COALESCE(NULLIF(excluded.industry, ''), assets.industry),
                    updated_at = excluded.updated_at
                """,
                asset_rows,
            )
        if snap_rows:
            con.executemany(
                """
                INSERT INTO sector_snapshots (
                    market, sector_name, sector_code, trade_date, change_pct, volume,
                    turnover, leader_symbol, payload_json, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market, sector_name, trade_date, source) DO UPDATE SET
                    sector_code = excluded.sector_code,
                    change_pct = excluded.change_pct,
                    volume = excluded.volume,
                    turnover = excluded.turnover,
                    leader_symbol = excluded.leader_symbol,
                    payload_json = excluded.payload_json
                """,
                snap_rows,
            )
            rows_written += len(snap_rows)
        if constituent_rows:
            con.executemany(
                """
                INSERT INTO sector_constituents (
                    sector_name, symbol, name, price, change_pct, pe, pb,
                    market_cap, updated_at, source
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (sector_name, symbol, source) DO UPDATE SET
                    name = excluded.name,
                    price = excluded.price,
                    change_pct = excluded.change_pct,
                    pe = excluded.pe,
                    pb = excluded.pb,
                    market_cap = excluded.market_cap,
                    updated_at = excluded.updated_at
                """,
                constituent_rows,
            )
            rows_written += len(constituent_rows)
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
        if dedup_news:
            con.executemany(
                """
                INSERT INTO news_events (
                    event_id, symbol, market, sector, published_at, title, summary,
                    url, source, sentiment_label, keywords_json, keys
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (event_id) DO UPDATE SET
                    sector = COALESCE(NULLIF(excluded.sector, ''), news_events.sector),
                    title = excluded.title,
                    summary = excluded.summary,
                    keywords_json = excluded.keywords_json,
                    keys = excluded.keys
                """,
                dedup_news,
            )
            rows_written += len(dedup_news)

    status = "success" if not errors else ("partial" if rows_written > 0 else "error")
    err_joined = "; ".join(errors) if errors else None
    run_id = record_ingestion_run(
        db_path,
        source="akshare_cn",
        target=f"sector:{ths_name}",
        status=status,
        rows_written=rows_written,
        started_at=started,
        error_msg=err_joined,
    )
    return {
        "run_id": run_id,
        "sector": ths_name,
        "sina_label": sina_label,
        "status": status,
        "snapshots": len(snap_rows),
        "constituents": len(constituent_rows),
        "bars": len(bar_rows),
        "news": len(dedup_news),
        "rows_written": rows_written,
        "errors": errors,
    }
