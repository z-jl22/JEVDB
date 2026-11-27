"""Offline unit/integration tests and live smoke tests for M1 storage & ingestion."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import akshare as ak
import jevdb
import pandas as pd
import pytest

from stock_research.storage.ingest import (
    SchedulerService,
    ingest_a_share_sector,
    ingest_a_share_stock,
    ingest_finnhub_earnings,
    ingest_finnhub_news,
    ingest_sec_edgar,
    ingest_us_stock,
    normalize_cn_symbol,
)
from stock_research.storage.ingest.finnhub_us import SlidingWindowRateLimiter
from stock_research.storage.ingest.sec_edgar import _SecRateLimiter
from stock_research.storage.jevdb_bridge import hybrid_semantic_query
from stock_research.storage.llm_shim import (
    SchemaSpec,
    ShimServer,
    ensure_shim_running,
    validate_and_normalize_answers,
)
from stock_research.storage.schema import connect_db, db_session, init_db, load_static_baseline

LIVE_ENABLED = os.getenv("RUN_LIVE_TESTS", "0") == "1"


def _make_mock_deepseek_server(
    *, fail_first_for_repair: bool = False
) -> tuple[HTTPServer, list[dict[str, Any]], str]:
    captured_requests: list[dict[str, Any]] = []
    state = {"calls": 0}

    class MockDeepSeekHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            captured_requests.append(body)
            state["calls"] += 1

            if fail_first_for_repair and state["calls"] == 1:
                bad_payload = {
                    "id": "mock-bad",
                    "object": "chat.completion",
                    "model": body.get("model", "deepseek-flash"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": '{"answers": []}',
                                "reasoning_content": "oops truncated",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
                raw = json.dumps(bad_payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            user_msg = next(
                (m["content"] for m in body["messages"] if m["role"] == "user"), "{}"
            )
            user_obj = json.loads(user_msg)
            items = user_obj.get("items") or [user_obj]
            n = len(items)
            sys_text = " ".join(m["content"] for m in body["messages"] if m["role"] == "system")

            if "strictly chosen from" in sys_text:
                # extract enum list from system prompt
                start = sys_text.index("strictly chosen from ") + len("strictly chosen from ")
                end = sys_text.index("]", start) + 1
                choices = json.loads(sys_text[start:end])
                answers = [
                    choices[0] if ("增长" in json.dumps(it, ensure_ascii=False) or "positive" in json.dumps(it, ensure_ascii=False).lower()) else choices[-1]
                    for it in items
                ]
            elif '"verdict"' in sys_text:
                answers = [{"id": i, "verdict": True} for i in range(n)]
            elif "JSON boolean" in sys_text:
                answers = [True for _ in range(n)]
            else:
                # jev_holds / jev_probability / jev_score
                is_score = "between 0.0 and " in sys_text and "between 0.0 and 1.0" not in sys_text and "between 0 and 1 " not in sys_text
                answers = []
                for it in items:
                    txt = json.dumps(it, ensure_ascii=False)
                    if is_score:
                        answers.append(3.0 if "增长" in txt else 1.0)
                    else:
                        answers.append(0.9 if ("增长" in txt or "电力" in txt) else 0.1)

            resp_payload = {
                "id": f"mock-{state['calls']}",
                "object": "chat.completion",
                "model": body.get("model", "deepseek-flash"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"answers": answers}, ensure_ascii=False),
                            "reasoning_content": "internal thought chain that must be stripped",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            }
            raw = json.dumps(resp_payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = HTTPServer(("127.0.0.1", 0), MockDeepSeekHandler)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    host, port = httpd.server_address
    return httpd, captured_requests, f"http://{host}:{port}/v1"


# ---------------------------------------------------------------------------
# Offline Unit Tests
# ---------------------------------------------------------------------------


def test_cn_symbol_normalization() -> None:
    assert normalize_cn_symbol("000858") == ("000858", "sz000858", "000858.SZ")
    assert normalize_cn_symbol("sz000858") == ("000858", "sz000858", "000858.SZ")
    assert normalize_cn_symbol("000858.SZ") == ("000858", "sz000858", "000858.SZ")
    assert normalize_cn_symbol("600900") == ("600900", "sh600900", "600900.SH")
    assert normalize_cn_symbol("SH600011") == ("600011", "sh600011", "600011.SH")
    assert normalize_cn_symbol("920002") == ("920002", "bj920002", "920002.BJ")
    for bad in ("AAPL", "", "   ", "00085", "1234567", "AAPL123456", "sh600900.SH"):
        with pytest.raises(ValueError):
            normalize_cn_symbol(bad)


def test_akshare_routing_and_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "m1_ak_fallback.duckdb"
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_RESEARCH_CACHE_DIR", str(tmp_path / "cache"))

    # Ensure push2 endpoints are never invoked
    def _forbid_push2(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("push2 endpoint must never be called")

    for name in ("stock_zh_a_hist_em", "stock_board_industry_cons_em", "stock_zh_a_spot_em"):
        if hasattr(ak, name):
            monkeypatch.setattr(ak, name, _forbid_push2)

    monkeypatch.setattr(
        ak,
        "stock_zh_a_hist_tx",
        lambda **kwargs: (_ for _ in ()).throw(TimeoutError("simulated Tencent timeout")),
    )
    monkeypatch.setattr(
        ak,
        "stock_zh_a_hist_sina",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "date": date(2026, 11, 24),
                    "open": 120.0,
                    "high": 122.0,
                    "low": 119.5,
                    "close": 121.0,
                    "volume": 5000000.0,
                    "amount": 605000000.0,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        ak,
        "stock_financial_analysis_indicator_em",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "SECURITY_NAME_ABBR": "五粮液",
                    "REPORT_DATE": "2026-09-30 00:00:00",
                    "REPORT_TYPE": "三季报",
                    "EPSJB": 5.1,
                    "BPS": 34.2,
                    "TOTALOPERATEREVE": 67916000000.0,
                    "PARENTNETPROFIT": 24931000000.0,
                    "ROEJQ": 8.09,
                    "ZCFZL": 29.66,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        ak,
        "stock_zh_valuation_baidu",
        lambda symbol, indicator, period: pd.DataFrame(
            [{"date": date(2026, 11, 25), "value": 22.8 if "市盈率" in indicator else 3.5}]
        ),
    )
    monkeypatch.setattr(
        ak,
        "stock_news_em",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "关键词": "五粮液",
                    "新闻标题": "五粮液三季报稳健增长",
                    "新闻内容": "公司三季度营收和净利润保持稳健。",
                    "发布时间": "2026-11-06 21:21:00",
                    "文章来源": "东方财富",
                    "新闻链接": "https://example.com/wly",
                }
            ]
        ),
    )

    res = ingest_a_share_stock("000858", days=5, db_path=db_path)
    assert res["status"] == "success"
    assert res["bar_source"] == "akshare_sina"
    assert res["bars"] == 1
    assert res["fundamentals"] == 1
    assert res["news"] == 1

    with db_session(db_path, init=False) as con:
        bar = con.execute(
            "SELECT volume, source FROM market_bars WHERE symbol='000858'"
        ).fetchone()
        assert bar == (5000000.0, "akshare_sina")
        fund = con.execute(
            "SELECT pe_ttm, pb, roe, debt_ratio FROM fundamentals WHERE symbol='000858'"
        ).fetchone()
        assert fund[0] == pytest.approx(22.8)
        assert fund[1] == pytest.approx(3.5)
        assert fund[2] == pytest.approx(0.0809)
        assert fund[3] == pytest.approx(0.2966)
        runs = con.execute("SELECT status, rows_written FROM ingestion_runs").fetchall()
        assert runs == [("success", 3)]


def test_akshare_sector_mapping_and_news_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "m1_ak_sector.duckdb"
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_RESEARCH_CACHE_DIR", str(tmp_path / "cache"))

    monkeypatch.setattr(
        ak,
        "stock_board_industry_summary_ths",
        lambda: pd.DataFrame(
            [
                {
                    "板块": "电力",
                    "涨跌幅": 1.25,
                    "总成交量": 2500.0,
                    "总成交额": 194.85,
                    "净流入": 5.2,
                    "上涨家数": 45,
                    "下跌家数": 15,
                    "均价": 8.1,
                    "领涨股": "闽东电力",
                    "领涨股-最新价": 10.5,
                    "领涨股-涨跌幅": 10.0,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        ak,
        "stock_board_industry_index_ths",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "日期": date(2026, 11, 24),
                    "开盘价": 1000.0,
                    "最高价": 1020.0,
                    "最低价": 995.0,
                    "收盘价": 1010.0,
                    "成交量": 1000000.0,
                    "成交额": 200000000.0,
                },
                {
                    "日期": date(2026, 11, 25),
                    "开盘价": 1012.0,
                    "最高价": 1030.0,
                    "最低价": 1010.0,
                    "收盘价": 1025.0,
                    "成交量": 1200000.0,
                    "成交额": 250000000.0,
                },
            ]
        ),
    )
    monkeypatch.setattr(
        ak,
        "stock_sector_detail",
        lambda sector: pd.DataFrame(
            [
                {
                    "symbol": "sh600900",
                    "code": "600900",
                    "name": "长江电力",
                    "trade": 28.5,
                    "changepercent": 0.9,
                    "per": 21.0,
                    "pb": 3.1,
                    "mktcap": 69700000.0,  # 万元
                },
                {
                    "symbol": "sh600011",
                    "code": "600011",
                    "name": "华能国际",
                    "trade": 7.78,
                    "changepercent": 1.4,
                    "per": 10.5,
                    "pb": 1.85,
                    "mktcap": 12213116.63,
                },
            ]
        ),
    )
    monkeypatch.setattr(
        ak,
        "stock_zh_a_hist_tx",
        lambda symbol, **kwargs: pd.DataFrame(
            [
                {
                    "date": date(2026, 11, 25),
                    "open": 28.1,
                    "close": 28.5,
                    "high": 28.7,
                    "low": 28.0,
                    "volume": 3000000.0,
                    "amount": 85000000.0,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        ak,
        "stock_news_em",
        lambda symbol: pd.DataFrame(
            [
                {
                    "关键词": "电力",
                    "新闻标题": f"{symbol}发电量同比增长",
                    "新闻内容": "水电与火电装机利用小时数提升。",
                    "发布时间": "2026-11-25 10:00:00",
                    "文章来源": "证券时报",
                    "新闻链接": f"https://example.com/{symbol}",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        ak,
        "stock_news_global_cls",
        lambda symbol="全部": pd.DataFrame(
            [
                {
                    "标题": "",
                    "内容": "国家能源局发布电力行业迎峰度冬保供方案，电网投资加速。",
                    "发布日期": date(2026, 11, 25),
                    "发布时间": "16:30:00",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        ak,
        "stock_news_global_sina",
        lambda: pd.DataFrame(
            [
                {"时间": "2026-11-25 18:00:00", "内容": "绿电交易规模持续扩大，火电盈利改善。"}
            ]
        ),
    )

    res = ingest_a_share_sector("电力", days=10, db_path=db_path, top_constituents=2)
    assert res["status"] == "success"
    assert res["sina_label"] == "new_dlhy"
    assert res["constituents"] == 2
    assert res["bars"] == 2
    assert res["news"] >= 3

    with db_session(db_path, init=False) as con:
        mktcap = con.execute(
            "SELECT market_cap FROM sector_constituents WHERE symbol='600900'"
        ).fetchone()[0]
        assert mktcap == pytest.approx(69700000.0 * 10000.0)
        keys_rows = con.execute(
            "SELECT keys FROM news_events WHERE sector='电力'"
        ).fetchall()
        assert keys_rows
        assert any("电力" in row[0] and "CN" in row[0] for row in keys_rows)


def test_yfinance_nested_news_and_units(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "m1_yf.duckdb"
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))

    class FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            self.info = {
                "shortName": "Apple Inc.",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "trailingPE": 39.5,
                "priceToBook": 46.1,
                "returnOnEquity": 1.42257,
                "debtToEquity": 84.286,
            }
            self.financials = pd.DataFrame(
                {pd.Timestamp("2026-09-30"): [478834000000.0, 132175000000.0, 158063000000.0]},
                index=["Total Revenue", "Net Income", "Operating Income"],
            )
            self.quarterly_financials = pd.DataFrame()
            self.news = [
                {
                    "id": "uuid-1",
                    "content": {
                        "id": "uuid-1",
                        "title": "Apple expands services revenue",
                        "description": "<p>Services hit <b>record</b> high.</p>",
                        "pubDate": "2026-11-25T21:27:04Z",
                        "canonicalUrl": {"url": "https://finance.yahoo.com/news/aapl-1"},
                        "provider": {"displayName": "Reuters"},
                    },
                }
            ]

        def history(self, period: str = "6mo", auto_adjust: bool = False) -> pd.DataFrame:
            idx = pd.DatetimeIndex(
                ["2026-11-24", "2026-11-25"], tz="America/New_York", name="Date"
            )
            return pd.DataFrame(
                {
                    "Open": [228.0, 230.0],
                    "High": [231.0, 232.5],
                    "Low": [227.5, 229.0],
                    "Close": [230.0, 232.0],
                    "Volume": [40000000.0, 42000000.0],
                },
                index=idx,
            )

    monkeypatch.setattr("stock_research.storage.ingest.yfinance_us.yf.Ticker", FakeTicker)

    res = ingest_us_stock("aapl", period="1mo", db_path=db_path)
    assert res["status"] == "success"
    assert res["bars"] == 2
    assert res["fundamentals"] == 1
    assert res["news"] == 1

    with db_session(db_path, init=False) as con:
        roe, debt_ratio, rev = con.execute(
            "SELECT roe, debt_ratio, revenue FROM fundamentals WHERE symbol='AAPL'"
        ).fetchone()
        assert roe == pytest.approx(1.42257)
        assert debt_ratio == pytest.approx(0.84286)
        assert rev == pytest.approx(478834000000.0)
        title, summary, src = con.execute(
            "SELECT title, summary, source FROM news_events WHERE symbol='AAPL'"
        ).fetchone()
        assert title == "Apple expands services revenue"
        assert "<p>" not in summary and "record" in summary
        assert src == "yfinance:Reuters"


def test_sec_edgar_revenue_tag_priority_and_rate_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "m1_sec.duckdb"
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_RESEARCH_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SEC_USER_AGENT", "TestAgent/1.0 (qa@example.org)")

    seen_headers: list[dict[str, str]] = []

    class FakeResp:
        def __init__(self, payload: Any, status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code
            self.headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self) -> Any:
            return self._payload

    class FakeClient:
        def __init__(self, *args: Any, headers: dict[str, str] | None = None, **kwargs: Any) -> None:
            self.headers = headers or {}
            seen_headers.append(self.headers)

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return

        def get(self, url: str) -> FakeResp:
            if "company_tickers.json" in url:
                return FakeResp({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}})
            if "submissions/CIK0000320193.json" in url:
                return FakeResp(
                    {
                        "filings": {
                            "recent": {
                                "accessionNumber": ["0000320193-26-000106", "0000320193-26-000099"],
                                "form": ["10-K", "4"],
                                "filingDate": ["2026-11-03", "2026-11-01"],
                                "reportDate": ["2026-09-26", "2026-11-01"],
                                "primaryDocument": ["aapl-20260926.htm", "form4.xml"],
                                "primaryDocDescription": ["10-K", "Form 4"],
                            }
                        }
                    }
                )
            if "companyfacts/CIK0000320193.json" in url:
                return FakeResp(
                    {
                        "facts": {
                            "us-gaap": {
                                "Revenues": {
                                    "units": {
                                        "USD": [
                                            {
                                                "start": "2017-10-01",
                                                "end": "2018-09-29",
                                                "val": 265595000000.0,
                                                "form": "10-K",
                                                "filed": "2018-11-05",
                                            }
                                        ]
                                    }
                                },
                                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                                    "units": {
                                        "USD": [
                                            {
                                                "start": "2025-09-28",
                                                "end": "2026-09-26",
                                                "val": 478834000000.0,
                                                "form": "10-K",
                                                "filed": "2026-11-03",
                                            }
                                        ]
                                    }
                                },
                                "NetIncomeLoss": {
                                    "units": {
                                        "USD": [
                                            {
                                                "start": "2025-09-28",
                                                "end": "2026-09-26",
                                                "val": 132175000000.0,
                                                "form": "10-K",
                                                "filed": "2026-11-03",
                                            }
                                        ]
                                    }
                                },
                                "Assets": {
                                    "units": {
                                        "USD": [
                                            {
                                                "end": "2026-09-26",
                                                "val": 404548000000.0,
                                                "form": "10-K",
                                                "filed": "2026-11-03",
                                            }
                                        ]
                                    }
                                },
                                "StockholdersEquity": {
                                    "units": {
                                        "USD": [
                                            {
                                                "end": "2026-09-26",
                                                "val": 112093000000.0,
                                                "form": "10-K",
                                                "filed": "2026-11-03",
                                            }
                                        ]
                                    }
                                },
                                "Liabilities": {
                                    "units": {
                                        "USD": [
                                            {
                                                "end": "2026-09-26",
                                                "val": 292455000000.0,
                                                "form": "10-K",
                                                "filed": "2026-11-03",
                                            }
                                        ]
                                    }
                                },
                            }
                        }
                    }
                )
            raise AssertionError(f"Unexpected URL: {url}")

        def close(self) -> None:
            return

    monkeypatch.setattr("stock_research.storage.ingest.sec_edgar.httpx.Client", FakeClient)

    limiter = _SecRateLimiter(min_interval=0.05)
    t0 = time.monotonic()
    limiter.wait()
    limiter.wait()
    assert (time.monotonic() - t0) >= 0.045

    res = ingest_sec_edgar("AAPL", db_path=db_path)
    assert res["cik"] == "0000320193"
    assert res["filings"] == 1  # Form 4 filtered out
    assert seen_headers[0]["User-Agent"] == "TestAgent/1.0 (qa@example.org)"

    with db_session(db_path, init=False) as con:
        latest = con.execute(
            "SELECT report_date, revenue, net_income FROM fundamentals WHERE symbol='AAPL' ORDER BY report_date DESC LIMIT 1"
        ).fetchone()
        assert latest[0] == date(2026, 9, 26)
        assert latest[1] == pytest.approx(478834000000.0)
        assert latest[2] == pytest.approx(132175000000.0)


def test_finnhub_rate_limiter_and_parsing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "m1_finnhub.duckdb"
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("FINNHUB_API_KEY", "test-finnhub-token")

    limiter = SlidingWindowRateLimiter(max_calls=2, window_seconds=0.12)
    assert limiter.acquire() == 0.0
    assert limiter.acquire() == 0.0
    waited = limiter.acquire()
    assert waited >= 0.08

    seen_tokens: list[str] = []

    class FakeResp:
        def __init__(self, payload: Any) -> None:
            self._payload = payload
            self.status_code = 200
            self.headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return

        def json(self) -> Any:
            return self._payload

    class FakeClient:
        def __init__(self, *args: Any, headers: dict[str, str] | None = None, **kwargs: Any) -> None:
            seen_tokens.append((headers or {}).get("X-Finnhub-Token", ""))

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return

        def get(self, url: str, params: dict[str, Any] | None = None) -> FakeResp:
            if url.endswith("/news"):
                return FakeResp(
                    [
                        {
                            "id": 9001,
                            "category": "general",
                            "datetime": 1764100000,
                            "headline": "Fed signals steady rates",
                            "summary": "Market rallies on macro stability.",
                            "source": "CNBC",
                            "related": "SPY,AAPL",
                            "url": "https://example.com/fh-news",
                        }
                    ]
                )
            if url.endswith("/calendar/earnings"):
                return FakeResp(
                    {
                        "earningsCalendar": [
                            {
                                "symbol": "NVDA",
                                "date": "2026-11-26",
                                "epsEstimate": 0.85,
                                "epsActual": 0.91,
                                "revenueEstimate": 35000000000,
                                "revenueActual": 36200000000,
                            }
                        ]
                    }
                )
            raise AssertionError(f"Unexpected Finnhub URL: {url}")

    monkeypatch.setattr("stock_research.storage.ingest.finnhub_us.httpx.Client", FakeClient)

    n_res = ingest_finnhub_news(category="general", db_path=db_path)
    e_res = ingest_finnhub_earnings("2026-11-25", "2026-11-30", db_path=db_path)
    assert n_res["news"] == 1
    assert e_res["earnings"] == 1
    assert seen_tokens == ["test-finnhub-token", "test-finnhub-token"]

    with db_session(db_path, init=False) as con:
        row = con.execute(
            "SELECT symbol, eps_estimate, eps_actual FROM earnings_calendar WHERE symbol='NVDA'"
        ).fetchone()
        assert row == ("NVDA", pytest.approx(0.85), pytest.approx(0.91))


def test_llm_shim_protocol_and_validation() -> None:
    httpd, captured, upstream_url = _make_mock_deepseek_server(fail_first_for_repair=True)
    try:
        with ShimServer(
            ("127.0.0.1", 0),
            upstream_url=upstream_url,
            api_key="test-key",
            default_model="deepseek-flash",
        ) as shim:
            con = jevdb.connect()
            try:
                con.execute("CREATE SECRET llm (TYPE openai, PROVIDER config, API_KEY 'local')")
                con.execute("SET jevdb_backend='llm'")
                con.execute("SET jevdb_endpoint=?", [shim.url])
                con.execute("SET jevdb_model='deepseek-flash'")
                con.execute("CREATE TABLE items (id VARCHAR, txt VARCHAR)")
                con.execute("INSERT INTO items VALUES ('a', '公司营收大幅增长'), ('b', '行业竞争加剧承压')")

                holds_res = con.execute(
                    "SELECT id FROM items WHERE jev_holds('是否营收增长', {'txt': txt})"
                ).fetchall()
                assert holds_res == [("a",)]

                prob_res = con.execute(
                    "SELECT id, jev_probability('是否营收增长', {'txt': txt}) FROM items ORDER BY id"
                ).fetchall()
                assert prob_res[0][1] == pytest.approx(0.9)
                assert prob_res[1][1] == pytest.approx(0.1)

                choose_res = con.execute(
                    "SELECT id, jev_choose('判断情感', {'txt': txt}, ['positive', 'negative', 'neutral']) FROM items ORDER BY id"
                ).fetchall()
                assert choose_res == [("a", "positive"), ("b", "neutral")]

                score_res = con.execute(
                    "SELECT id, jev_score('景气评分', {'txt': txt}, ['差', '弱', '中', '好', '极佳']) FROM items ORDER BY id"
                ).fetchall()
                assert score_res == [("a", 3.0), ("b", 1.0)]
            finally:
                con.close()

        # Verify first call was repaired and every forwarded request translated json_schema -> json_object
        assert len(captured) >= 5
        for req in captured:
            assert req["response_format"] == {"type": "json_object"}
            assert req["max_tokens"] >= 4096
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------------------
# Offline Integration Tests
# ---------------------------------------------------------------------------


def test_schema_and_static_baseline_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "m1_baseline.duckdb"
    with db_session(db_path, init=True) as con:
        first = load_static_baseline(con)
        second = load_static_baseline(con)
        assert first == {"assets": 2, "market_bars": 26, "news_events": 2, "static_trading_rules": 2}
        assert second == first
        assert con.execute("SELECT COUNT(*) FROM market_bars WHERE source='static_baseline'").fetchone()[0] == 26
        assert con.execute("SELECT COUNT(*) FROM news_events WHERE source='static_baseline'").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM static_trading_rules").fetchone()[0] == 2
        # Verify volume normalized from lots to shares (131524 * 100)
        vol = con.execute(
            "SELECT volume FROM market_bars WHERE symbol='000858' AND trade_date=DATE '2025-04-24'"
        ).fetchone()[0]
        assert vol == pytest.approx(13152400.0)


def test_hybrid_semantic_query_static_and_dynamic_with_sbf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "m1_hybrid.duckdb"
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-key")

    with db_session(db_path, init=True) as con:
        load_static_baseline(con)
        con.execute(
            """
            INSERT INTO news_events (
                event_id, symbol, market, sector, published_at, title, summary,
                url, source, sentiment_label, keywords_json, keys
            )
            VALUES
                (
                    'DYN-PWR-1', '600900', 'CN', '电力', TIMESTAMP '2026-11-25 10:00:00',
                    '长江电力装机与营收增长', '电力行业迎来用电高峰，水电发电量同比增长8%。',
                    'https://example.com/pwr1', 'akshare_cls', NULL, '["CN","电力","600900"]',
                    ['CN', '电力', '600900']
                ),
                (
                    'DYN-BANK-1', '600036', 'CN', '银行', TIMESTAMP '2026-11-25 11:00:00',
                    '银行息差企稳', '银行业资产质量保持稳定。',
                    'https://example.com/bank1', 'akshare_cls', NULL, '["CN","银行","600036"]',
                    ['CN', '银行', '600036']
                )
            """
        )

    httpd, _, upstream_url = _make_mock_deepseek_server()
    try:
        with ShimServer(
            ("127.0.0.1", 0),
            upstream_url=upstream_url,
            api_key="mock-key",
            default_model="deepseek-flash",
        ) as shim:
            # 1. SBF + jev_holds over dynamic + static rows
            sbf_res = hybrid_semantic_query(
                {
                    "condition": "是否涉及电力行业或发电营收增长",
                    "op": "holds",
                    "target": "news_events",
                    "query_keys": ["电力", "600900"],
                    "endpoint": shim.url,
                    "backend": "llm",
                    "model": "deepseek-flash",
                },
                db_path=db_path,
            )
            assert sbf_res["sbf_pruned"] >= 3  # 2 static + 1 bank row pruned by SBF
            assert len(sbf_res["rows"]) == 1
            assert sbf_res["rows"][0]["record_id"] == "DYN-PWR-1"
            assert sbf_res["rows"][0]["semantic_result"] is True
            assert sbf_res["stats"].get("records", 0) == 1
            assert sbf_res["stats"].get("index_candidate_pairs", 0) == 1
            assert sbf_res["stats"].get("candidate_left_retained", 0) == 1
            assert sbf_res["stats"].get("candidate_left_rows", 0) == 4
            assert sbf_res["stats"].get("cross_product_pairs", 0) == 0

            # 2. Combined static + dynamic choose query + cache hit verification
            choose_res_1 = hybrid_semantic_query(
                {
                    "condition": "判断新闻对公司业务的影响",
                    "op": "choose",
                    "choices": ["positive", "negative", "neutral"],
                    "target": "news_events",
                    "endpoint": shim.url,
                    "backend": "llm",
                    "model": "deepseek-flash",
                },
                db_path=db_path,
            )
            assert "static_baseline" in choose_res_1["sources"]
            assert "akshare_cls" in choose_res_1["sources"]
            assert choose_res_1["llm_evaluated"] == 4

            choose_res_2 = hybrid_semantic_query(
                {
                    "condition": "判断新闻对公司业务的影响",
                    "op": "choose",
                    "choices": ["positive", "negative", "neutral"],
                    "target": "news_events",
                    "endpoint": shim.url,
                    "backend": "llm",
                    "model": "deepseek-flash",
                },
                db_path=db_path,
            )
            assert choose_res_2["cache_hits"] == 4
            assert choose_res_2["llm_evaluated"] == 0
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_scheduler_trigger_and_background_concurrency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "m1_sched.duckdb"
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    counter = {"calls": 0}

    def ok_job(db_path: Path) -> dict[str, Any]:
        counter["calls"] += 1
        with db_session(db_path, init=True) as con:
            con.execute(
                """
                INSERT INTO assets (symbol, market, name, sector, industry, updated_at)
                VALUES ('000001', 'CN', '平安银行', '银行', '银行', CURRENT_TIMESTAMP)
                ON CONFLICT (symbol, market) DO UPDATE SET updated_at = excluded.updated_at
                """
            )
        return {"status": "success", "rows_written": 1, "run_id": f"ok-{counter['calls']}"}

    def failing_job(db_path: Path) -> dict[str, Any]:
        raise RuntimeError("simulated upstream error")

    svc = SchedulerService(
        db_path=db_path,
        default_jobs=[
            {"name": "ok_job", "fn": ok_job, "kwargs": {}},
            {"name": "failing_job", "fn": failing_job, "kwargs": {}},
            {"name": "needs_fh", "type": "finnhub_news", "requires_finnhub": True, "kwargs": {}},
        ],
    )
    results = svc.trigger_once()
    assert [r["status"] for r in results] == ["success", "error", "skipped"]

    svc.start_background(interval_seconds=0.15, jobs=["ok_job"], run_immediately=True)
    try:
        time.sleep(0.35)
        with connect_db(db_path) as con:
            cnt = con.execute("SELECT COUNT(*) FROM assets WHERE symbol='000001'").fetchone()[0]
            assert cnt == 1
        st = svc.status()
        assert st["running"] is True
        assert counter["calls"] >= 2
        assert len(st["recent_runs"]) >= 2
    finally:
        svc.stop()
    assert svc.status()["running"] is False


def test_multithread_db_session_barrier_and_scheduler_concurrency(tmp_path: Path) -> None:
    db_mt = tmp_path / "mt_barrier.duckdb"
    barrier = threading.Barrier(8)
    errors: list[tuple[int, str]] = []
    ok_count = {"n": 0}
    lock = threading.Lock()

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            with db_session(db_mt, init=True) as con:
                con.execute(
                    "INSERT INTO assets (symbol, market, name) VALUES (?, 'CN', ?) ON CONFLICT DO NOTHING",
                    [f"60000{idx}", f"Stock{idx}"],
                )
            with lock:
                ok_count["n"] += 1
        except Exception as exc:
            with lock:
                errors.append((idx, f"{type(exc).__name__}: {exc}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)

    assert errors == []
    assert ok_count["n"] == 8

    # Deleting and recreating DB at same path still initializes cleanly
    db_mt.unlink()
    with db_session(db_mt, init=True) as con:
        assert con.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0

    # Concurrent start_background + manual trigger_once on fresh DB
    sched_db = tmp_path / "sched_concurrent.duckdb"
    run_counter = {"n": 0}
    run_lock = threading.Lock()

    def fresh_job(db_path: Path) -> dict[str, Any]:
        with run_lock:
            run_counter["n"] += 1
            idx = run_counter["n"]
        with db_session(db_path, init=True) as con:
            con.execute(
                "INSERT INTO assets (symbol, market, name) VALUES (?, 'CN', 'Test') ON CONFLICT DO NOTHING",
                [f"6010{idx:02d}"],
            )
        time.sleep(0.02)
        return {"status": "success", "rows_written": 1, "run_id": f"r-{idx}"}

    svc = SchedulerService(
        db_path=sched_db,
        default_jobs=[{"name": "fresh_job", "fn": fresh_job, "kwargs": {}}],
    )
    svc.start_background(interval_seconds=0.05, run_immediately=True)
    try:
        for _ in range(5):
            res = svc.trigger_once()
            assert len(res) == 1
            assert res[0]["status"] == "success"
    finally:
        svc.stop(timeout=5.0)
    assert svc.status()["running"] is False


def test_akshare_sector_cache_duckdb_and_fundamentals_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_RESEARCH_CACHE_DIR", str(tmp_path / "cache"))
    db_path = tmp_path / "sector_resilience.duckdb"

    # Common mocks for THS / bars / news
    monkeypatch.setattr(
        ak,
        "stock_board_industry_summary_ths",
        lambda: pd.DataFrame([{"板块": "电力", "涨跌幅": 0.5, "总成交量": 100.0, "总成交额": 100.0, "领涨股": "长江电力"}]),
    )
    monkeypatch.setattr(
        ak,
        "stock_board_industry_index_ths",
        lambda **kwargs: pd.DataFrame([{"日期": date(2026, 11, 25), "开盘价": 1000.0, "最高价": 1010.0, "最低价": 990.0, "收盘价": 1005.0, "成交量": 1000.0, "成交额": 10000.0}]),
    )
    monkeypatch.setattr(
        ak,
        "stock_zh_a_hist_tx",
        lambda symbol, **kwargs: pd.DataFrame([{"date": date(2026, 11, 25), "open": 28.0, "high": 28.5, "low": 27.9, "close": 28.3, "volume": 1000.0, "amount": 28300.0}]),
    )
    monkeypatch.setattr(ak, "stock_news_em", lambda symbol: pd.DataFrame())
    monkeypatch.setattr(ak, "stock_news_global_cls", lambda symbol="全部": pd.DataFrame())
    monkeypatch.setattr(ak, "stock_news_global_sina", lambda: pd.DataFrame())

    # 1. Initial live success populates disk cache + DuckDB
    detail_calls = {"n": 0}

    def ok_detail(sector: str) -> pd.DataFrame:
        detail_calls["n"] += 1
        return pd.DataFrame(
            [
                {"symbol": "sh600900", "code": "600900", "name": "长江电力", "trade": 28.3, "changepercent": 0.5, "per": 20.0, "pb": 3.0, "mktcap": 70000000.0},
                {"symbol": "sh600011", "code": "600011", "name": "华能国际", "trade": 7.8, "changepercent": 1.1, "per": 11.0, "pb": 1.8, "mktcap": 12000000.0},
            ]
        )

    monkeypatch.setattr(ak, "stock_sector_detail", ok_detail)
    res1 = ingest_a_share_sector("电力", days=5, db_path=db_path, top_constituents=1)
    assert res1["constituents"] == 2
    assert res1["bars"] == 1
    assert detail_calls["n"] == 1

    # 2. Timeout on force=True falls back to stale disk cache (avoiding 0 constituents)
    monkeypatch.setattr("stock_research.storage.ingest.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        ak,
        "stock_sector_detail",
        lambda sector: (_ for _ in ()).throw(TimeoutError("simulated Sina timeout")),
    )
    res2 = ingest_a_share_sector("电力", days=5, db_path=db_path, top_constituents=1, force=True)
    assert res2["constituents"] == 2
    assert res2["bars"] == 1

    # 3. Remove disk cache -> falls back to existing DuckDB sector_constituents rows
    for p in (tmp_path / "cache").glob("cn_sector_constituents_*.json"):
        p.unlink()
    res3 = ingest_a_share_sector("电力", days=5, db_path=db_path, top_constituents=1, force=True)
    assert res3["constituents"] == 2
    assert res3["bars"] == 1

    # 3b. Cold DB + no disk cache + Sina timeout -> falls back to Shenwan secondary source (non-push2)
    monkeypatch.setattr(
        ak,
        "index_second_info_sw",
        lambda: pd.DataFrame([{"行业代码": "801161.SI", "行业名称": "电力"}]),
    )
    monkeypatch.setattr(
        ak,
        "index_component_sw",
        lambda symbol="801161": pd.DataFrame(
            [
                {"证券代码": "600900", "证券名称": "长江电力", "最新权重": 12.5},
                {"证券代码": "600011", "证券名称": "华能国际", "最新权重": 5.2},
            ]
        ),
    )
    cold_db = tmp_path / "sector_cold_sw.duckdb"
    res3b = ingest_a_share_sector("电力", days=5, db_path=cold_db, top_constituents=1, force=True)
    assert res3b["constituents"] == 2
    assert res3b["bars"] == 1
    with db_session(cold_db, init=False) as con:
        sw_src = con.execute(
            "SELECT DISTINCT source FROM sector_constituents WHERE sector_name='电力'"
        ).fetchall()
        assert sw_src == [("akshare_sw",)]

    # 4. Fundamentals: skipped first row with bad REPORT_DATE still attaches pe_ttm/pb to first valid row
    monkeypatch.setattr(
        ak,
        "stock_zh_valuation_baidu",
        lambda symbol, indicator, period: pd.DataFrame([{"date": date(2026, 11, 25), "value": 18.5 if "市盈率" in indicator else 2.4}]),
    )
    monkeypatch.setattr(
        ak,
        "stock_financial_analysis_indicator_em",
        lambda **kwargs: pd.DataFrame(
            [
                {"SECURITY_NAME_ABBR": "长江电力", "REPORT_DATE": "", "REPORT_TYPE": "", "ROEJQ": None, "ZCFZL": None, "TOTALOPERATEREVE": None, "PARENTNETPROFIT": None},
                {"SECURITY_NAME_ABBR": "长江电力", "REPORT_DATE": "2026-09-30", "REPORT_TYPE": "三季报", "ROEJQ": 12.0, "ZCFZL": 60.0, "TOTALOPERATEREVE": 6e10, "PARENTNETPROFIT": 2e10},
            ]
        ),
    )
    f_res = ingest_a_share_stock("600900", days=3, db_path=db_path, include_news=False)
    assert f_res["fundamentals"] == 1
    with db_session(db_path, init=False) as con:
        pe, pb = con.execute("SELECT pe_ttm, pb FROM fundamentals WHERE symbol='600900'").fetchone()
        assert pe == pytest.approx(18.5)
        assert pb == pytest.approx(2.4)


def test_shim_key_rotation_and_numbered_id_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    import stock_research.storage.llm_shim as llm_shim_mod

    seen_auths: list[str] = []

    class AuthCaptureHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            _ = self.rfile.read(length)
            seen_auths.append(self.headers.get("Authorization", ""))
            payload = {
                "id": "mock-auth",
                "object": "chat.completion",
                "model": "deepseek-flash",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"answers": [true]}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            }
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = HTTPServer(("127.0.0.1", 0), AuthCaptureHandler)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    host, port = httpd.server_address
    upstream = f"http://{host}:{port}/v1"

    try:
        monkeypatch.setenv("DEEPSEEK_BASE_URL", upstream)
        monkeypatch.setenv("JEVDB_SHIM_PORT", "0")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "first-key-111")
        url1 = ensure_shim_running()

        import httpx
        req_body = {
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": '{"items": ["x"]}'}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"schema": {"properties": {"answers": {"type": "array", "minItems": 1, "items": {"type": "boolean"}}}}},
            },
        }
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            r1 = client.post(f"{url1}/chat/completions", headers={"Authorization": "Bearer local"}, json=req_body)
            assert r1.status_code == 200

            monkeypatch.setenv("DEEPSEEK_API_KEY", "second-key-222")
            url2 = ensure_shim_running()
            r2 = client.post(f"{url2}/chat/completions", headers={"Authorization": "Bearer local"}, json=req_body)
            assert r2.status_code == 200

        assert seen_auths == ["Bearer first-key-111", "Bearer second-key-222"]
    finally:
        if llm_shim_mod._GLOBAL_SHIM is not None:
            llm_shim_mod._GLOBAL_SHIM.stop()
            llm_shim_mod._GLOBAL_SHIM = None
        httpd.shutdown()
        httpd.server_close()

    # Reject non-integral float id and bool id in numbered_boolean
    spec = SchemaSpec("numbered_boolean", 2)
    with pytest.raises(ValueError):
        validate_and_normalize_answers(
            '{"answers": [{"id": 1.5, "verdict": true}, {"id": 0, "verdict": false}]}',
            spec,
        )
    with pytest.raises(ValueError):
        validate_and_normalize_answers(
            '{"answers": [{"id": true, "verdict": true}, {"id": false, "verdict": false}]}',
            spec,
        )


def test_hybrid_query_bounds_threshold_cache_and_symbol_normalization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "m1_hybrid_bounds.duckdb"
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "mock-key")

    with db_session(db_path, init=True) as con:
        load_static_baseline(con)
        con.execute(
            """
            INSERT INTO news_events (
                event_id, symbol, market, sector, published_at, title, summary,
                url, source, sentiment_label, keywords_json, keys
            )
            VALUES
                (
                    'PWR-1', '600900', 'CN', '电力', TIMESTAMP '2026-11-25 10:00:00',
                    '长江电力装机与营收增长', '电力行业发电量增长。',
                    'https://example.com/p1', 'akshare_cls', NULL, '["CN","电力","600900"]',
                    ['CN', '电力', '600900']
                ),
                (
                    'BANK-1', '600036', 'CN', '银行', TIMESTAMP '2026-11-25 11:00:00',
                    '银行息差企稳', '银行业稳定。',
                    'https://example.com/b1', 'akshare_cls', NULL, '["CN","银行","600036"]',
                    ['CN', '银行', '600036']
                )
            """
        )

    httpd, _, upstream_url = _make_mock_deepseek_server()
    try:
        with ShimServer(
            ("127.0.0.1", 0),
            upstream_url=upstream_url,
            api_key="mock-key",
            default_model="deepseek-flash",
        ) as shim:
            # 1. only_matching on probability excludes rows below threshold (mock returns 0.9 for 增长/电力, 0.1 otherwise)
            prob_res = hybrid_semantic_query(
                {
                    "condition": "是否涉及电力增长",
                    "op": "probability",
                    "threshold": 0.5,
                    "only_matching": True,
                    "target": "news_events",
                    "filters": {"source_scope": "dynamic"},
                    "endpoint": shim.url,
                    "backend": "llm",
                },
                db_path=db_path,
            )
            assert [r["record_id"] for r in prob_res["rows"]] == ["PWR-1"]

            # 2. threshold cache isolation for op="holds"
            h_low = hybrid_semantic_query(
                {
                    "condition": "是否涉及电力增长",
                    "op": "holds",
                    "threshold": 0.05,
                    "target": "news_events",
                    "filters": {"source_scope": "dynamic"},
                    "endpoint": shim.url,
                    "backend": "llm",
                },
                db_path=db_path,
            )
            assert h_low["cache_hits"] == 0
            assert sorted(r["record_id"] for r in h_low["rows"] if r["semantic_result"]) == ["BANK-1", "PWR-1"]

            h_high = hybrid_semantic_query(
                {
                    "condition": "是否涉及电力增长",
                    "op": "holds",
                    "threshold": 0.8,
                    "target": "news_events",
                    "filters": {"source_scope": "dynamic"},
                    "endpoint": shim.url,
                    "backend": "llm",
                },
                db_path=db_path,
            )
            assert h_high["cache_hits"] == 0
            assert [r["record_id"] for r in h_high["rows"] if r["semantic_result"]] == ["PWR-1"]

            # 3. filters["limit"] == 0 returns empty; limit < 0 raises ValueError
            lim_zero = hybrid_semantic_query(
                {
                    "condition": "是否涉及电力增长",
                    "op": "holds",
                    "filters": {"limit": 0},
                    "endpoint": shim.url,
                    "backend": "llm",
                },
                db_path=db_path,
            )
            assert lim_zero["rows"] == []
            with pytest.raises(ValueError):
                hybrid_semantic_query(
                    {
                        "condition": "是否涉及电力增长",
                        "op": "holds",
                        "filters": {"limit": -1},
                        "endpoint": shim.url,
                        "backend": "llm",
                    },
                    db_path=db_path,
                )

            # 4. CN symbol normalization in filters["symbol"] ("600900.SH" / "sh600900" -> matches "600900")
            for sym_variant in ("600900.SH", "sh600900"):
                sym_res = hybrid_semantic_query(
                    {
                        "condition": "是否涉及电力增长",
                        "op": "holds",
                        "filters": {"symbol": sym_variant},
                        "endpoint": shim.url,
                        "backend": "llm",
                    },
                    db_path=db_path,
                )
                assert [r["record_id"] for r in sym_res["rows"]] == ["PWR-1"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_yfinance_prefers_balance_sheet_liabilities_over_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "m1_yf_bs.duckdb"
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))

    class FakeTickerWithBS:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            self.info = {
                "shortName": "Apple Inc.",
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "trailingPE": 35.0,
                "priceToBook": 40.0,
                "returnOnEquity": 1.35,
                "debtToEquity": 152.0,
            }
            col = pd.Timestamp("2026-09-30")
            self.financials = pd.DataFrame(
                {col: [400000000000.0, 100000000000.0]},
                index=["Total Revenue", "Net Income"],
            )
            self.quarterly_financials = pd.DataFrame()
            self.balance_sheet = pd.DataFrame(
                {col: [500000000000.0, 400000000000.0]},
                index=["Total Assets", "Total Liabilities Net Minority Interest"],
            )
            self.quarterly_balance_sheet = pd.DataFrame()
            self.news = []

        def history(self, period: str = "6mo", auto_adjust: bool = False) -> pd.DataFrame:
            return pd.DataFrame()

    monkeypatch.setattr("stock_research.storage.ingest.yfinance_us.yf.Ticker", FakeTickerWithBS)
    res = ingest_us_stock("AAPL", period="1mo", db_path=db_path)
    assert res["fundamentals"] == 1
    with db_session(db_path, init=False) as con:
        debt_ratio, metrics_json = con.execute(
            "SELECT debt_ratio, metrics_json FROM fundamentals WHERE symbol='AAPL'"
        ).fetchone()
        assert debt_ratio == pytest.approx(0.80)
        metrics = json.loads(metrics_json)
        assert metrics["debt_to_equity_pct"] == pytest.approx(152.0)
        assert metrics["debt_ratio_basis"] == "liabilities_over_assets"


# ---------------------------------------------------------------------------
# Live Smoke Tests (RUN_LIVE_TESTS=1)
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason="Set RUN_LIVE_TESTS=1 to run live network tests")
def test_live_akshare_power_sector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_RESEARCH_CACHE_DIR", str(tmp_path / "cache"))
    db_path = tmp_path / "live_ak.duckdb"
    res = ingest_a_share_sector("电力", days=7, db_path=db_path, top_constituents=3)
    assert res["status"] in ("success", "partial")
    assert res["constituents"] > 10
    assert res["bars"] >= 1
    with db_session(db_path, init=False) as con:
        assert con.execute("SELECT COUNT(*) FROM sector_snapshots WHERE sector_name='电力'").fetchone()[0] >= 1
        assert con.execute("SELECT COUNT(*) FROM sector_constituents WHERE sector_name='电力'").fetchone()[0] > 10
        assert con.execute("SELECT COUNT(*) FROM news_events WHERE sector='电力'").fetchone()[0] >= 1


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason="Set RUN_LIVE_TESTS=1 to run live network tests")
def test_live_us_yfinance_and_sec_edgar_aapl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_RESEARCH_CACHE_DIR", str(tmp_path / "cache"))
    db_path = tmp_path / "live_us.duckdb"
    yf_res = ingest_us_stock("AAPL", period="1mo", db_path=db_path)
    sec_res = ingest_sec_edgar("AAPL", max_filings=15, db_path=db_path)
    assert yf_res["bars"] >= 5
    assert sec_res["filings"] >= 5
    assert sec_res["fundamentals"] >= 1
    with db_session(db_path, init=False) as con:
        rev = con.execute(
            "SELECT MAX(revenue) FROM fundamentals WHERE symbol='AAPL' AND source='sec_edgar'"
        ).fetchone()[0]
        assert rev and rev > 1e11


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason="Set RUN_LIVE_TESTS=1 to run live network tests")
def test_live_finnhub_news_and_earnings(tmp_path: Path) -> None:
    if not os.getenv("FINNHUB_API_KEY"):
        pytest.skip("FINNHUB_API_KEY not set")
    db_path = tmp_path / "live_fh.duckdb"
    to_dt = date.today() + timedelta(days=7)
    from_dt = date.today() - timedelta(days=7)
    n_res = ingest_finnhub_news(category="general", db_path=db_path)
    e_res = ingest_finnhub_earnings(from_dt.isoformat(), to_dt.isoformat(), db_path=db_path)
    assert n_res["news"] >= 1
    assert e_res["earnings"] >= 1


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason="Set RUN_LIVE_TESTS=1 to run live network tests")
def test_live_jevdb_deepseek_hybrid_query(tmp_path: Path) -> None:
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY not set")
    db_path = tmp_path / "live_jevdb_ds.duckdb"
    with db_session(db_path, init=True) as con:
        load_static_baseline(con)
        con.execute(
            """
            INSERT INTO news_events (
                event_id, symbol, market, sector, published_at, title, summary,
                url, source, sentiment_label, keywords_json, keys
            )
            VALUES (
                'LIVE-PWR-1', '600900', 'CN', '电力', TIMESTAMP '2026-11-25 09:30:00',
                '长江电力发电量创阶段新高', '来水偏丰叠加电网调度优化，公司发电量和营业收入稳步增长。',
                'https://example.com/live-pwr', 'live_smoke', NULL, '["CN","电力","600900"]',
                ['CN', '电力', '600900']
            )
            """
        )

    # 1. Live jev_holds + jev_keys_overlap SBF pruning over static + dynamic rows
    holds_res = hybrid_semantic_query(
        {
            "condition": "Does this news discuss the electric power sector or hydro power generation growth?",
            "op": "holds",
            "target": "news_events",
            "query_keys": ["电力", "600900"],
            "backend": "llm",
            "model": "deepseek-flash",
        },
        db_path=db_path,
    )
    assert holds_res["sbf_pruned"] >= 2  # static baseline rows pruned by jev_keys_overlap
    assert len(holds_res["rows"]) == 1
    assert holds_res["rows"][0]["record_id"] == "LIVE-PWR-1"
    assert holds_res["rows"][0]["semantic_result"] is True
    assert holds_res["stats"].get("index_candidate_pairs", 0) == 1
    assert holds_res["stats"].get("cross_product_pairs", 0) == 0

    # 2. Live jev_choose across combined static + dynamic rows
    res = hybrid_semantic_query(
        {
            "condition": "Classify the impact of this news on the company's business",
            "op": "choose",
            "choices": ["positive", "negative", "neutral"],
            "target": "news_events",
            "backend": "llm",
            "model": "deepseek-flash",
        },
        db_path=db_path,
    )
    assert len(res["rows"]) == 3
    assert "static_baseline" in res["sources"]
    assert "live_smoke" in res["sources"]
    assert all(r["semantic_result"] in ("positive", "negative", "neutral") for r in res["rows"])


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason="Set RUN_LIVE_TESTS=1 to run live network tests")
def test_live_scheduler_trigger_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if not os.getenv("FINNHUB_API_KEY"):
        pytest.skip("FINNHUB_API_KEY not set")
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("STOCK_RESEARCH_CACHE_DIR", str(tmp_path / "cache"))
    db_path = tmp_path / "live_sched.duckdb"
    svc = SchedulerService(db_path=db_path)
    results = svc.trigger_once(
        [
            {"name": "live_cn_power", "type": "a_share_sector", "kwargs": {"sector": "电力", "days": 5, "top_constituents": 2}},
            {"name": "live_us_aapl", "type": "us_stock", "kwargs": {"symbol": "AAPL", "period": "5d"}},
            {"name": "live_fh_news", "type": "finnhub_news", "requires_finnhub": True, "kwargs": {"category": "general"}},
        ]
    )
    assert len(results) == 3
    assert all(r["status"] in ("success", "partial") for r in results)
    assert all(r["rows_written"] >= 1 for r in results)
    cn_detail = results[0]["detail"]
    assert cn_detail["constituents"] > 10
    assert cn_detail["bars"] >= 1
    us_detail = results[1]["detail"]
    assert us_detail["bars"] >= 1
    fh_detail = results[2]["detail"]
    assert fh_detail["news"] >= 1
    st = svc.status(recent_limit=10)
    assert len(st["recent_runs"]) >= 3
