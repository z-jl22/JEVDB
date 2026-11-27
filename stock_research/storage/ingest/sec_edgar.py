"""Zero-key SEC EDGAR submissions and XBRL companyfacts ingestion."""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from stock_research.config import get_settings
from stock_research.storage.ingest import (
    read_json_cache,
    record_ingestion_run,
    utc_now_naive,
    write_json_cache,
)
from stock_research.storage.schema import db_session

_SEC_TICKERS_CACHE = "sec_company_tickers.json"
_SEC_TICKERS_TTL = 86400.0

_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
_NET_INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")
_OP_INCOME_TAGS = ("OperatingIncomeLoss",)
_ASSETS_TAGS = ("Assets",)
_EQUITY_TAGS = (
    "StockholdersEquity",
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
)
_LIABILITIES_TAGS = ("Liabilities",)


class _SecRateLimiter:
    """Thread-safe spacing limiter staying strictly under SEC's 10 req/s cap."""

    def __init__(self, min_interval: float = 0.12) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_ts = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delta = self.min_interval - (now - self._last_ts)
            if delta > 0:
                time.sleep(delta)
            self._last_ts = time.monotonic()


_RATE_LIMITER = _SecRateLimiter(min_interval=0.12)


def _sec_client() -> httpx.Client:
    settings = get_settings()
    ua = settings.sec_user_agent
    if not ua or "@" not in ua:
        ua = "JEVDBStockResearch/0.1 (research@example.org)"
    return httpx.Client(
        timeout=25.0,
        trust_env=False,
        headers={
            "User-Agent": ua,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json",
        },
    )


def _parse_retry_after(raw: str | None, default: float) -> float:
    """Parse numeric or RFC 7231 HTTP-date Retry-After into a bounded delay in [0.5, 5.0]."""
    bounded_default = max(0.5, min(5.0, default))
    text = (raw or "").strip()
    if not text:
        return bounded_default
    try:
        num = float(text)
        if math.isfinite(num):
            return max(0.5, min(5.0, num))
        return bounded_default
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return bounded_default
    if dt is None:
        return bounded_default
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(delta):
        return bounded_default
    return max(0.5, min(5.0, delta))


def _sec_get_json(client: httpx.Client, url: str) -> Any:
    for attempt in range(3):
        _RATE_LIMITER.wait()
        resp = client.get(url)
        if resp.status_code in (429, 503) and attempt < 2:
            retry_after = _parse_retry_after(
                resp.headers.get("Retry-After"),
                1.0 * (attempt + 1),
            )
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"SEC GET failed after retries: {url}")


def _parse_date(val: Any) -> date | None:
    text = str(val or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def resolve_cik(symbol: str, *, client: httpx.Client | None = None) -> tuple[str, str]:
    """Resolve US ticker symbol to `(cik10, title)` using cached SEC `company_tickers.json`."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("US symbol cannot be empty")

    data = read_json_cache(_SEC_TICKERS_CACHE, _SEC_TICKERS_TTL)
    if data is None:
        own_client = client is None
        http_client = client or _sec_client()
        try:
            data = _sec_get_json(http_client, "https://www.sec.gov/files/company_tickers.json")
        finally:
            if own_client:
                http_client.close()
        write_json_cache(_SEC_TICKERS_CACHE, data)

    alt_sym = sym.replace(".", "-")
    for entry in (data.values() if isinstance(data, dict) else []):
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker") or "").strip().upper()
        if ticker in (sym, alt_sym):
            cik10 = f"{int(entry['cik_str']):010d}"
            title = str(entry.get("title") or sym).strip()
            return cik10, title

    raise ValueError(f"Unknown SEC ticker: {sym}")


def _pick_metric_by_date(
    us_gaap: dict[str, Any],
    tags: tuple[str, ...],
    *,
    is_duration: bool,
) -> dict[date, tuple[float, str, str]]:
    """Select best XBRL fact `(val, tag, form)` per `end` date honoring tag priority & latest filed."""
    per_date: dict[date, tuple[int, str, int, float, str, str]] = {}
    for priority, tag in enumerate(tags):
        tag_obj = us_gaap.get(tag)
        if not isinstance(tag_obj, dict):
            continue
        usd_facts = ((tag_obj.get("units") or {}).get("USD")) or []
        for fact in usd_facts:
            form = str(fact.get("form") or "").strip()
            if form not in ("10-K", "10-Q"):
                continue
            end_dt = _parse_date(fact.get("end"))
            if end_dt is None:
                continue
            val = fact.get("val")
            if val is None or isinstance(val, bool):
                continue
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(num):
                continue

            filed = str(fact.get("filed") or "")
            duration_days = 0
            if is_duration:
                start_dt = _parse_date(fact.get("start"))
                if start_dt is not None:
                    duration_days = (end_dt - start_dt).days

            # For 10-K duration items, prefer full-year (~365d) over 3-month Q4 fragments;
            # then prefer newer `filed` date, then higher-priority tag (lower index).
            candidate_rank = (
                1 if (form == "10-K" and is_duration and duration_days >= 300) else 0,
                filed,
                duration_days,
                -priority,
            )
            existing = per_date.get(end_dt)
            if existing is None or candidate_rank > existing[:4]:
                per_date[end_dt] = (*candidate_rank, num, tag, form)

    return {
        dt: (item[4], item[5], item[6]) for dt, item in per_date.items()
    }


def ingest_sec_edgar(
    symbol: str,
    forms: tuple[str, ...] = ("10-K", "10-Q", "8-K"),
    max_filings: int = 30,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Ingest SEC EDGAR filings and XBRL companyfacts fundamentals for a US ticker."""
    started = utc_now_naive()
    sym = str(symbol or "").strip().upper()
    now = utc_now_naive()

    try:
        with _sec_client() as client:
            cik10, company_title = resolve_cik(sym, client=client)
            cik_int = int(cik10)
            submissions = _sec_get_json(
                client, f"https://data.sec.gov/submissions/CIK{cik10}.json"
            )
            companyfacts = _sec_get_json(
                client, f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
            )

        # 1. Filings from submissions
        recent = ((submissions.get("filings") or {}).get("recent")) or {}
        acc_list = recent.get("accessionNumber") or []
        form_list = recent.get("form") or []
        fdate_list = recent.get("filingDate") or []
        rdate_list = recent.get("reportDate") or []
        pdoc_list = recent.get("primaryDocument") or []
        desc_list = recent.get("primaryDocDescription") or []

        allowed_forms = {f.upper() for f in forms}
        filing_rows: list[tuple[Any, ...]] = []
        for i in range(min(len(acc_list), len(form_list))):
            form_val = str(form_list[i] or "").strip().upper()
            if form_val not in allowed_forms:
                continue
            acc = str(acc_list[i] or "").strip()
            pdoc = str(pdoc_list[i] or "").strip() if i < len(pdoc_list) else ""
            desc = str(desc_list[i] or "").strip() if i < len(desc_list) else ""
            f_date = _parse_date(fdate_list[i]) if i < len(fdate_list) else None
            r_date = _parse_date(rdate_list[i]) if i < len(rdate_list) else None
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc.replace('-', '')}/{pdoc}"
                if acc and pdoc
                else f"https://www.sec.gov/edgar/browse/?CIK={cik10}"
            )
            filing_rows.append(
                (
                    cik10,
                    sym,
                    acc,
                    form_val,
                    f_date,
                    r_date,
                    pdoc,
                    desc or f"{company_title} {form_val}",
                    url,
                )
            )
            if len(filing_rows) >= max_filings:
                break

        # 2. XBRL companyfacts -> fundamentals
        us_gaap = ((companyfacts.get("facts") or {}).get("us-gaap")) or {}
        rev_map = _pick_metric_by_date(us_gaap, _REVENUE_TAGS, is_duration=True)
        ni_map = _pick_metric_by_date(us_gaap, _NET_INCOME_TAGS, is_duration=True)
        op_map = _pick_metric_by_date(us_gaap, _OP_INCOME_TAGS, is_duration=True)
        assets_map = _pick_metric_by_date(us_gaap, _ASSETS_TAGS, is_duration=False)
        eq_map = _pick_metric_by_date(us_gaap, _EQUITY_TAGS, is_duration=False)
        liab_map = _pick_metric_by_date(us_gaap, _LIABILITIES_TAGS, is_duration=False)

        all_dates = sorted(
            set(rev_map) | set(ni_map) | set(assets_map),
            reverse=True,
        )[:8]

        fund_rows: list[tuple[Any, ...]] = []
        for r_date in all_dates:
            rev_item = rev_map.get(r_date)
            ni_item = ni_map.get(r_date)
            op_item = op_map.get(r_date)
            assets_item = assets_map.get(r_date)
            eq_item = eq_map.get(r_date)
            liab_item = liab_map.get(r_date)

            rev = rev_item[0] if rev_item else None
            ni = ni_item[0] if ni_item else None
            assets_v = assets_item[0] if assets_item else None
            eq_v = eq_item[0] if eq_item else None
            liab_v = liab_item[0] if liab_item else None

            roe_raw = (ni / eq_v) if (ni is not None and eq_v and eq_v > 0) else None
            roe = roe_raw if (roe_raw is not None and math.isfinite(roe_raw)) else None
            debt_raw = (
                (liab_v / assets_v)
                if (liab_v is not None and assets_v and assets_v > 0)
                else None
            )
            debt_ratio = debt_raw if (debt_raw is not None and math.isfinite(debt_raw)) else None
            metrics = {
                "revenue_tag": rev_item[1] if rev_item else None,
                "form": (rev_item or ni_item or assets_item or (None, None, ""))[2],
                "operating_income": op_item[0] if op_item else None,
                "assets": assets_v,
                "equity": eq_v,
                "liabilities": liab_v,
            }
            fund_rows.append(
                (
                    sym,
                    "US",
                    r_date,
                    None,
                    None,
                    roe,
                    rev,
                    ni,
                    debt_ratio,
                    json.dumps(metrics, ensure_ascii=False, allow_nan=False),
                    "sec_edgar",
                    now,
                )
            )

        filing_rows = list({r[2]: r for r in filing_rows}.values())
        fund_rows = list({(r[0], r[1], r[2], r[10]): r for r in fund_rows}.values())

        rows_written = 0
        with db_session(db_path, init=True) as con:
            con.execute(
                """
                INSERT INTO assets (symbol, market, name, sector, industry, updated_at)
                VALUES (?, 'US', ?, '', '', ?)
                ON CONFLICT (symbol, market) DO UPDATE SET
                    name = COALESCE(NULLIF(excluded.name, ''), assets.name),
                    updated_at = excluded.updated_at
                """,
                [sym, company_title, now],
            )
            if filing_rows:
                con.executemany(
                    """
                    INSERT INTO edgar_filings (
                        cik, symbol, accession_no, form, filing_date, report_date,
                        primary_doc, title, url
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (accession_no) DO UPDATE SET
                        form = excluded.form,
                        filing_date = excluded.filing_date,
                        report_date = excluded.report_date,
                        primary_doc = excluded.primary_doc,
                        title = excluded.title,
                        url = excluded.url
                    """,
                    filing_rows,
                )
                rows_written += len(filing_rows)
            if fund_rows:
                con.executemany(
                    """
                    INSERT INTO fundamentals (
                        symbol, market, report_date, pe_ttm, pb, roe, revenue, net_income,
                        debt_ratio, metrics_json, source, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (symbol, market, report_date, source) DO UPDATE SET
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

        run_id = record_ingestion_run(
            db_path,
            source="sec_edgar",
            target=sym,
            status="success",
            rows_written=rows_written,
            started_at=started,
        )
        return {
            "run_id": run_id,
            "symbol": sym,
            "cik": cik10,
            "company": company_title,
            "status": "success",
            "filings": len(filing_rows),
            "fundamentals": len(fund_rows),
            "rows_written": rows_written,
        }
    except Exception as exc:
        try:
            record_ingestion_run(
                db_path,
                source="sec_edgar",
                target=sym or "<empty>",
                status="error",
                rows_written=0,
                started_at=started,
                error_msg=str(exc),
            )
        except Exception:
            pass
        raise
