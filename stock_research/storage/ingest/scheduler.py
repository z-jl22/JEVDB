"""On-demand and optional periodic background monitoring scheduler."""

from __future__ import annotations

import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from stock_research.config import get_settings
from stock_research.storage.ingest import record_ingestion_run, utc_now_naive
from stock_research.storage.ingest.akshare_cn import (
    ingest_a_share_sector,
    ingest_a_share_stock,
)
from stock_research.storage.ingest.finnhub_us import (
    ingest_finnhub_earnings,
    ingest_finnhub_news,
)
from stock_research.storage.ingest.sec_edgar import ingest_sec_edgar
from stock_research.storage.ingest.yfinance_us import ingest_us_stock
from stock_research.storage.schema import db_session

JOB_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "a_share_sector": ingest_a_share_sector,
    "a_share_stock": ingest_a_share_stock,
    "us_stock": ingest_us_stock,
    "sec_edgar": ingest_sec_edgar,
    "finnhub_news": ingest_finnhub_news,
    "finnhub_earnings": ingest_finnhub_earnings,
}


def build_default_jobs() -> list[dict[str, Any]]:
    today = date.today()
    next_week = today + timedelta(days=7)
    return [
        {
            "name": "cn_power_sector",
            "type": "a_share_sector",
            "kwargs": {"sector": "电力", "days": 30},
        },
        {
            "name": "us_watchlist_aapl",
            "type": "us_stock",
            "kwargs": {"symbol": "AAPL", "period": "6mo"},
        },
        {
            "name": "us_edgar_aapl",
            "type": "sec_edgar",
            "kwargs": {"symbol": "AAPL"},
        },
        {
            "name": "finnhub_general_news",
            "type": "finnhub_news",
            "requires_finnhub": True,
            "kwargs": {"category": "general", "days": 3},
        },
        {
            "name": "finnhub_earnings_upcoming",
            "type": "finnhub_earnings",
            "requires_finnhub": True,
            "kwargs": {"from_date": today.isoformat(), "to_date": next_week.isoformat()},
        },
    ]


class SchedulerService:
    """Manages on-demand (`trigger_once`) and optional periodic background ingestion."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        default_jobs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.db_path = db_path
        self.default_jobs = default_jobs
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._interval_seconds: float | None = None
        self._last_run_at: str | None = None
        self._last_results: list[dict[str, Any]] = []

    def _resolve_jobs(self, jobs: list[dict[str, Any] | str] | None) -> list[dict[str, Any]]:
        defaults = self.default_jobs if self.default_jobs is not None else build_default_jobs()
        if jobs is None:
            return [dict(j) for j in defaults]
        by_name = {str(j.get("name")): j for j in defaults if j.get("name")}
        resolved: list[dict[str, Any]] = []
        for entry in jobs:
            if isinstance(entry, str):
                if entry in by_name:
                    resolved.append(dict(by_name[entry]))
                elif entry in JOB_HANDLERS:
                    resolved.append({"name": entry, "type": entry, "kwargs": {}})
                else:
                    raise ValueError(f"Unknown scheduler job name: {entry!r}")
            elif isinstance(entry, dict):
                resolved.append(dict(entry))
            else:
                raise TypeError(f"Unsupported job spec type: {type(entry)}")
        return resolved

    def trigger_once(self, jobs: list[dict[str, Any] | str] | None = None) -> list[dict[str, Any]]:
        """Execute configured ingestion jobs once sequentially without holding persistent DB locks."""
        job_list = self._resolve_jobs(jobs)
        results: list[dict[str, Any]] = []
        settings = get_settings()

        with self._run_lock:
            for job in job_list:
                name = str(job.get("name") or job.get("type") or "unnamed_job")
                fn = job.get("fn")
                if fn is None:
                    job_type = str(job.get("type") or name)
                    fn = JOB_HANDLERS.get(job_type)
                if fn is None:
                    err_msg = f"No handler registered for job {name!r}"
                    run_id = record_ingestion_run(
                        self.db_path,
                        source="scheduler",
                        target=name,
                        status="error",
                        rows_written=0,
                        started_at=utc_now_naive(),
                        error_msg=err_msg,
                    )
                    results.append(
                        {
                            "job": name,
                            "status": "error",
                            "rows_written": 0,
                            "run_id": run_id,
                            "error": err_msg,
                        }
                    )
                    continue

                if job.get("requires_finnhub") and not settings.finnhub_api_key:
                    started = utc_now_naive()
                    run_id = record_ingestion_run(
                        self.db_path,
                        source="scheduler",
                        target=name,
                        status="skipped",
                        rows_written=0,
                        started_at=started,
                        error_msg="FINNHUB_API_KEY not configured",
                    )
                    results.append(
                        {
                            "job": name,
                            "status": "skipped",
                            "rows_written": 0,
                            "run_id": run_id,
                            "error": None,
                        }
                    )
                    continue

                kwargs = dict(job.get("kwargs") or {})
                kwargs.setdefault("db_path", self.db_path)
                started = utc_now_naive()
                try:
                    out = fn(**kwargs)
                    rows = int(out.get("rows_written") or 0) if isinstance(out, dict) else 0
                    status = str(out.get("status") or "success") if isinstance(out, dict) else "success"
                    run_id = (out.get("run_id") if isinstance(out, dict) else None) or ""
                    results.append(
                        {
                            "job": name,
                            "status": status,
                            "rows_written": rows,
                            "run_id": run_id,
                            "error": None,
                            "detail": out,
                        }
                    )
                except Exception as exc:
                    run_id = record_ingestion_run(
                        self.db_path,
                        source="scheduler",
                        target=name,
                        status="error",
                        rows_written=0,
                        started_at=started,
                        error_msg=str(exc),
                    )
                    results.append(
                        {
                            "job": name,
                            "status": "error",
                            "rows_written": 0,
                            "run_id": run_id,
                            "error": str(exc),
                        }
                    )

            with self._lock:
                self._last_run_at = utc_now_naive().isoformat()
                self._last_results = results
        return results

    def start_background(
        self,
        interval_seconds: float = 3600.0,
        jobs: list[dict[str, Any] | str] | None = None,
        *,
        run_immediately: bool = False,
    ) -> None:
        """Start an idempotent daemon background polling thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._interval_seconds = float(interval_seconds)

            def _loop() -> None:
                if run_immediately and not self._stop_event.is_set():
                    self.trigger_once(jobs)
                while not self._stop_event.wait(max(0.05, float(interval_seconds))):
                    if self._stop_event.is_set():
                        break
                    self.trigger_once(jobs)

            self._thread = threading.Thread(target=_loop, name="jevdb-ingest-scheduler", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background polling thread cleanly."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def status(self, recent_limit: int = 10) -> dict[str, Any]:
        """Return scheduler runtime status and recent `ingestion_runs` from DuckDB."""
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            interval = self._interval_seconds
            last_run_at = self._last_run_at
            last_results = list(self._last_results)

        recent_runs: list[dict[str, Any]] = []
        try:
            with db_session(self.db_path, init=True) as con:
                cur = con.execute(
                    """
                    SELECT run_id, source, target, status, rows_written, started_at, finished_at, error_msg
                    FROM ingestion_runs
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    [int(recent_limit)],
                )
                cols = [d[0] for d in cur.description]
                recent_runs = [
                    {
                        k: (v.isoformat() if hasattr(v, "isoformat") else v)
                        for k, v in zip(cols, row)
                    }
                    for row in cur.fetchall()
                ]
        except Exception:
            pass

        return {
            "running": running,
            "interval_seconds": interval,
            "last_run_at": last_run_at,
            "last_results": last_results,
            "recent_runs": recent_runs,
        }
