"""Shared ingestion utilities and public ingestor exports."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from stock_research.config import get_settings
from stock_research.storage.schema import db_session

T = TypeVar("T")


def utc_now_naive() -> datetime:
    """Return current UTC timestamp without tzinfo for DuckDB TIMESTAMP columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_event_id(source: str, unique_key: str) -> str:
    """Create a deterministic 32-char event ID from source and unique content key."""
    raw = f"{source.strip().lower()}::{unique_key.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def make_keys(*tokens: str | Iterable[str] | None) -> list[str]:
    """Normalize and deduplicate string tokens into a non-empty VARCHAR[] key list."""
    result: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        clean = tok.strip()
        if not clean:
            return
        lowered = clean.lower()
        if lowered not in seen:
            seen.add(lowered)
            result.append(clean)

    for item in tokens:
        if item is None:
            continue
        if isinstance(item, str):
            for part in item.split(","):
                _add(part)
        else:
            for sub in item:
                if isinstance(sub, str):
                    for part in sub.split(","):
                        _add(part)
    return result


def retry_call(
    fn: Callable[[], T],
    *,
    retries: int = 2,
    backoff: float = 0.6,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """Call `fn()` with simple linear/exponential backoff on transient errors."""
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except exceptions as exc:
            last_exc = exc
            if attempt >= retries:
                raise
            time.sleep(backoff * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def read_json_cache(cache_name: str, ttl_seconds: float) -> Any | None:
    """Read a cached JSON payload if present and younger than `ttl_seconds`."""
    path = get_settings().cache_dir / cache_name
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > ttl_seconds:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_cache(cache_name: str, payload: Any) -> Path:
    """Write a JSON payload to the peripheral cache directory."""
    path = get_settings().cache_dir / cache_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def record_ingestion_run(
    db_path: str | Path | None,
    *,
    source: str,
    target: str,
    status: str,
    rows_written: int,
    started_at: datetime,
    finished_at: datetime | None = None,
    error_msg: str | None = None,
) -> str:
    """Record an ingestion execution audit entry using a short-lived DB session."""
    run_id = uuid.uuid4().hex[:16]
    fin = finished_at or utc_now_naive()
    with db_session(db_path, init=True) as con:
        con.execute(
            """
            INSERT INTO ingestion_runs (
                run_id, source, target, status, rows_written, started_at, finished_at, error_msg
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                source,
                target,
                status,
                int(rows_written),
                started_at,
                fin,
                error_msg[:1000] if error_msg else None,
            ],
        )
    return run_id


from stock_research.storage.ingest.akshare_cn import (
    ingest_a_share_sector,
    ingest_a_share_stock,
    normalize_cn_symbol,
    resolve_cn_sector,
)
from stock_research.storage.ingest.finnhub_us import (
    ingest_finnhub_earnings,
    ingest_finnhub_news,
)
from stock_research.storage.ingest.scheduler import SchedulerService
from stock_research.storage.ingest.sec_edgar import ingest_sec_edgar, resolve_cik
from stock_research.storage.ingest.yfinance_us import ingest_us_stock

__all__ = [
    "SchedulerService",
    "ingest_a_share_sector",
    "ingest_a_share_stock",
    "ingest_finnhub_earnings",
    "ingest_finnhub_news",
    "ingest_sec_edgar",
    "ingest_us_stock",
    "make_event_id",
    "make_keys",
    "normalize_cn_symbol",
    "read_json_cache",
    "record_ingestion_run",
    "resolve_cik",
    "resolve_cn_sector",
    "retry_call",
    "utc_now_naive",
    "write_json_cache",
]
