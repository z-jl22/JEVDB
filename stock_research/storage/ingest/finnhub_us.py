"""Finnhub US news and earnings calendar ingestion with 60 req/min rate limiting."""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from stock_research.config import get_settings
from stock_research.storage.ingest import (
    make_event_id,
    make_keys,
    record_ingestion_run,
    utc_now_naive,
)
from stock_research.storage.schema import db_session


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window rate limiter (default 55 calls / 60s under Finnhub's 60/min cap)."""

    def __init__(self, max_calls: int = 55, window_seconds: float = 60.0) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._timestamps: deque[float] = deque()

    def acquire(self) -> float:
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and (now - self._timestamps[0]) >= self.window_seconds:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_calls:
                    self._timestamps.append(now)
                    return waited
                sleep_for = self.window_seconds - (now - self._timestamps[0]) + 0.01
            time.sleep(max(0.01, sleep_for))
            waited += max(0.01, sleep_for)


_FINNHUB_LIMITER = SlidingWindowRateLimiter(max_calls=55, window_seconds=60.0)


def _to_float(val: Any) -> float | None:
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        num = float(val)
        return num if math.isfinite(num) else None
    except (TypeError, ValueError):
        return None


def _to_date(val: Any) -> date | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float, dict, list, tuple, set, bytes)):
        return None
    if type(val).__name__ in {"NaTType", "NAType"}:
        return None
    if isinstance(val, datetime):
        try:
            if val != val:
                return None
            return val.date()
        except Exception:
            return None
    if isinstance(val, date):
        return val
    text = str(val).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null"}:
        return None
    if len(text) <= 10:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    if text[4] != "-" or text[7] != "-" or text[10] not in ("T", " "):
        return None
    iso_text = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        return datetime.fromisoformat(iso_text).date()
    except ValueError:
        return None


def _finnhub_get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    api_key: str | None = None,
    limiter: SlidingWindowRateLimiter | None = None,
) -> Any:
    settings = get_settings()
    token = (api_key if api_key is not None else settings.finnhub_api_key).strip()
    if not token:
        raise ValueError("FINNHUB_API_KEY is required for Finnhub ingestion")

    rl = limiter or _FINNHUB_LIMITER
    url = f"https://finnhub.io/api/v1{path}"
    with httpx.Client(
        timeout=15.0,
        trust_env=False,
        headers={"X-Finnhub-Token": token},
    ) as client:
        for attempt in range(3):
            rl.acquire()
            resp = client.get(url, params=params)
            if resp.status_code == 429 and attempt < 2:
                reset_ts = resp.headers.get("X-Ratelimit-Reset")
                delay = 1.5 * (attempt + 1)
                if reset_ts:
                    try:
                        delay = max(0.5, min(10.0, float(reset_ts) - time.time() + 0.2))
                    except ValueError:
                        pass
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
    raise RuntimeError(f"Finnhub request failed after retries: {path}")


def ingest_finnhub_news(
    symbol: str | None = None,
    category: str = "general",
    days: int = 7,
    db_path: str | Path | None = None,
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Ingest US company or market-general news from Finnhub into `news_events`."""
    started = utc_now_naive()
    sym = str(symbol).strip().upper() if symbol else ""
    target = sym or f"category:{category}"

    try:
        if sym:
            to_dt = date.today()
            from_dt = to_dt - timedelta(days=max(1, days))
            items = _finnhub_get(
                "/company-news",
                {"symbol": sym, "from": from_dt.isoformat(), "to": to_dt.isoformat()},
                api_key=api_key,
            )
        else:
            items = _finnhub_get("/news", {"category": category}, api_key=api_key)

        news_rows: list[tuple[Any, ...]] = []
        for item in (items if isinstance(items, list) else []):
            if not isinstance(item, dict):
                continue
            headline = str(item.get("headline") or "").strip()
            summary = str(item.get("summary") or "").strip()
            url = str(item.get("url") or "").strip()
            if not headline and not summary:
                continue
            epoch = item.get("datetime")
            pub = utc_now_naive()
            if (
                isinstance(epoch, (int, float))
                and not isinstance(epoch, bool)
                and math.isfinite(float(epoch))
            ):
                ep_f = float(epoch)
                if 0.0 <= ep_f <= 4102444800.0:
                    try:
                        pub = datetime.fromtimestamp(ep_f, tz=timezone.utc).replace(tzinfo=None)
                    except (OverflowError, OSError, ValueError):
                        pub = utc_now_naive()
            related = str(item.get("related") or "").strip()
            row_sym = sym or (related.split(",")[0].strip().upper() if related else "")
            src = str(item.get("source") or "finnhub").strip()
            eid = make_event_id("finnhub", str(item.get("id") or url or f"{row_sym}:{headline}"))
            keys = make_keys("US", row_sym, category, related, "finnhub")
            news_rows.append(
                (
                    eid,
                    row_sym,
                    "US",
                    "",
                    pub,
                    headline or summary[:80],
                    summary,
                    url,
                    f"finnhub:{src}",
                    None,
                    json.dumps(keys, ensure_ascii=False),
                    keys,
                )
            )

        dedup_rows = list({r[0]: r for r in news_rows}.values())
        with db_session(db_path, init=True) as con:
            if dedup_rows:
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
                    dedup_rows,
                )

        run_id = record_ingestion_run(
            db_path,
            source="finnhub_news",
            target=target,
            status="success",
            rows_written=len(dedup_rows),
            started_at=started,
        )
        return {
            "run_id": run_id,
            "target": target,
            "status": "success",
            "news": len(dedup_rows),
            "rows_written": len(dedup_rows),
        }
    except Exception as exc:
        try:
            record_ingestion_run(
                db_path,
                source="finnhub_news",
                target=target,
                status="error",
                rows_written=0,
                started_at=started,
                error_msg=str(exc),
            )
        except Exception:
            pass
        raise


def ingest_finnhub_earnings(
    from_date: str,
    to_date: str,
    symbol: str | None = None,
    db_path: str | Path | None = None,
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Ingest US earnings calendar events from Finnhub into `earnings_calendar`."""
    started = utc_now_naive()
    sym = str(symbol).strip().upper() if symbol else ""
    from_clean = str(from_date or "").strip()
    to_clean = str(to_date or "").strip()
    target = f"{from_clean}..{to_clean}" + (f":{sym}" if sym else "")

    try:
        try:
            f_dt = date.fromisoformat(from_clean)
            t_dt = date.fromisoformat(to_clean)
        except ValueError as exc:
            raise ValueError(
                f"Invalid Finnhub earnings date(s): from_date={from_date!r}, to_date={to_date!r}"
            ) from exc
        if f_dt > t_dt:
            raise ValueError(
                f"Finnhub earnings from_date ({f_dt.isoformat()}) must be <= to_date ({t_dt.isoformat()})"
            )

        params: dict[str, Any] = {"from": f_dt.isoformat(), "to": t_dt.isoformat()}
        if sym:
            params["symbol"] = sym

        data = _finnhub_get("/calendar/earnings", params, api_key=api_key)
        entries = (data.get("earningsCalendar") if isinstance(data, dict) else None) or []
        now = utc_now_naive()

        cal_rows: list[tuple[Any, ...]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            row_sym = str(item.get("symbol") or "").strip().upper()
            parsed_date = _to_date(item.get("date"))
            if not row_sym or parsed_date is None:
                continue
            cal_rows.append(
                (
                    row_sym,
                    parsed_date.isoformat(),
                    _to_float(item.get("epsEstimate")),
                    _to_float(item.get("epsActual")),
                    _to_float(item.get("revenueEstimate")),
                    _to_float(item.get("revenueActual")),
                    "finnhub",
                    now,
                )
            )

        # Deduplicate by primary key (symbol, report_date, source)
        dedup_rows = list({(r[0], r[1], r[6]): r for r in cal_rows}.values())
        with db_session(db_path, init=True) as con:
            if dedup_rows:
                con.executemany(
                    """
                    INSERT INTO earnings_calendar (
                        symbol, report_date, eps_estimate, eps_actual,
                        revenue_estimate, revenue_actual, source, fetched_at
                    )
                    VALUES (?, CAST(? AS DATE), ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (symbol, report_date, source) DO UPDATE SET
                        eps_estimate = excluded.eps_estimate,
                        eps_actual = excluded.eps_actual,
                        revenue_estimate = excluded.revenue_estimate,
                        revenue_actual = excluded.revenue_actual,
                        fetched_at = excluded.fetched_at
                    """,
                    dedup_rows,
                )

        run_id = record_ingestion_run(
            db_path,
            source="finnhub_earnings",
            target=target,
            status="success",
            rows_written=len(dedup_rows),
            started_at=started,
        )
        return {
            "run_id": run_id,
            "target": target,
            "status": "success",
            "earnings": len(dedup_rows),
            "rows_written": len(dedup_rows),
        }
    except Exception as exc:
        try:
            record_ingestion_run(
                db_path,
                source="finnhub_earnings",
                target=target,
                status="error",
                rows_written=0,
                started_at=started,
                error_msg=str(exc),
            )
        except Exception:
            pass
        raise
