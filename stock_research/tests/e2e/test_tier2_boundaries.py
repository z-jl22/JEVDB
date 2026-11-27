"""Tier 2: Boundary, Negative & Corner-Case E2E Tests (46 tests across E1-E9)."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from stock_research.tests.e2e.conftest import REPO_ROOT
from stock_research.tests.e2e.test_tier1_features import (
    _get_docs_texts,
    _get_risk_mod,
    _http_json,
    _mcp_cmd,
    _mcp_stdio_session,
    _new_ledger,
    _next_day,
    _offline_llm_shim,
    _prepare_bars_for_quant,
    _start_web_backend,
    _stop_web_backend,
    _to_dict,
)

pytestmark = pytest.mark.tier2


# ===========================================================================
# E1. Core JEVDB & Baseline Boundaries (5 Tests)
# ===========================================================================

def test_T2_E1_01_jevdb_null_input_skips_http_calls(mark_backing):
    mark_backing("real")
    import jevdb

    con = jevdb.connect(":memory:")
    try:
        res = con.execute("SELECT jev_holds('positive', NULL)").fetchall()
        reqs = con.execute("SELECT requests FROM jevdb_stats()").fetchone()[0]
        assert res == [(None,)]
        assert reqs == 0
    finally:
        con.close()


def test_T2_E1_02_jevdb_explain_and_prepare_zero_requests(mark_backing):
    mark_backing("real")
    import jevdb

    con = jevdb.connect(":memory:")
    try:
        plan = con.execute("EXPLAIN SELECT jev_holds('pos', 'good')").fetchall()
        assert len(plan) >= 1
        con.execute("PREPARE q_prep AS SELECT jev_holds('pos', 'good')")
        reqs = con.execute("SELECT requests FROM jevdb_stats()").fetchone()[0]
        assert reqs == 0
    finally:
        con.close()


def test_T2_E1_03_jev_choose_invalid_choices_count_raises(mark_backing):
    mark_backing("real")
    import jevdb

    con = jevdb.connect(":memory:")
    try:
        with pytest.raises(Exception):
            con.execute("SELECT jev_choose('c', 'text', ['only_one'])").fetchall()
        with pytest.raises(Exception):
            con.execute("SELECT jev_choose('c', 'text', ['a', 'a'])").fetchall()
    finally:
        con.close()


def test_T2_E1_04_jev_keys_overlap_null_and_disjoint(mark_backing):
    mark_backing("real")
    import jevdb

    con = jevdb.connect(":memory:")
    try:
        null_res = con.execute("SELECT jev_keys_overlap(NULL, ['a'])").fetchone()[0]
        disjoint_res = con.execute("SELECT jev_keys_overlap(['a'], ['b'])").fetchone()[0]
        overlap_res = con.execute("SELECT jev_keys_overlap(['a', 'b'], ['b', 'c'])").fetchone()[0]
        assert null_res in (True, None)
        assert disjoint_res is False
        assert overlap_res is True
    finally:
        con.close()


def test_T2_E1_05_invalid_ohlc_rejected_in_baseline_and_schema(e2e_mode: str, isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "examples" / "stock_trading"))
    import trade

    bad_csv = isolated_env["tmp_dir"] / "bad_prices.csv"
    bad_csv.write_text("date,symbol,open,high,low,close,volume_lots\n2025-04-28,000858,10,9,11,10,100\n", encoding="utf-8")
    with pytest.raises(ValueError):
        trade.read_prices(bad_csv)

    if e2e_mode == "live":
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        with pytest.raises(ValueError):
            bt_mod._normalize_bars(
                [{"date": "2025-04-28", "symbol": "000858", "open": 10.0, "high": 9.0, "low": 11.0, "close": 10.0, "volume": 100}],
                ["000858"],
                "CN",
                __import__("datetime").date(2025, 4, 28),
                None,
                None,
            )
        led_mod = importlib.import_module("stock_research.quant.ledger")
        svc = led_mod.PaperAccountService(db_path=str(isolated_env["ledger_db_path"]))
        rej = svc.submit_order("000858", "BUY", 100, market="CN", reference_bar={"open": 10.0, "high": 9.0, "low": 11.0, "close": 10.0})
        assert rej["accepted"] is False
    else:
        from stock_research.tests.e2e import contract_harness as ch
        with pytest.raises(ValueError):
            ch.validate_ohlc_bar(10.0, 9.0, 11.0, 10.0, 100.0)


# ===========================================================================
# E2. Dynamic Data Ingestion Boundaries (5 Tests)
# ===========================================================================

@pytest.mark.live_network
def test_T2_E2_01_ingest_empty_or_nonexistent_symbol(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        ak = importlib.import_module("stock_research.storage.ingest.akshare_cn")
        yf_mod = importlib.import_module("stock_research.storage.ingest.yfinance_us")
        with pytest.raises(ValueError):
            ak.ingest_a_share_stock("")
        r_us = yf_mod.ingest_us_stock("INVALID_SYM_999999")
        assert r_us.get("rows_written", 0) == 0
    else:
        from stock_research.tests.e2e import contract_harness as ch
        r_cn = ch.ingest_a_share_stock("", db_path=isolated_env["db_path"])
        r_us = ch.ingest_us_stock("INVALID_SYM_999999", db_path=isolated_env["db_path"])
        assert r_cn.get("rows_written", 0) == 0
        assert r_us.get("rows_written", 0) == 0


@pytest.mark.live_network
def test_T2_E2_02_akshare_push2_failure_routes_to_fallback(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        import inspect as _ins
        ak = importlib.import_module("stock_research.storage.ingest.akshare_cn")
        src_text = _ins.getsource(ak)
        assert "stock_zh_a_hist(" not in src_text and "stock_zh_a_spot_em" not in src_text
        from unittest.mock import patch
        with patch.object(ak.ak, "stock_zh_a_hist_tx", side_effect=ConnectionError("primary bar endpoint blocked")):
            res = ak.ingest_a_share_stock("000858", days=5, include_fundamentals=False, include_news=False)
        assert res["bar_source"] == "akshare_sina"
        assert res["bars"] > 0
        assert res["rows_written"] > 0
        assert res.get("status") == "success"
    else:
        from stock_research.tests.e2e import contract_harness as ch
        res = ch.ingest_a_share_sector("电力", days=5, db_path=isolated_env["db_path"], simulate_push2_error=True)
        assert res["rows_written"] > 0
        assert res.get("status", "success") == "success"


@pytest.mark.live_network
def test_T2_E2_03_sec_edgar_unknown_ticker_and_403_handling(e2e_mode: str, isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch):
    if e2e_mode == "live":
        sec = importlib.import_module("stock_research.storage.ingest.sec_edgar")
        with pytest.raises(ValueError):
            sec.ingest_sec_edgar("ZZZZZZ")
        monkeypatch.setenv("SEC_USER_AGENT", "")
        from unittest.mock import patch
        import httpx
        req = httpx.Request("GET", "https://data.sec.gov/files/company_tickers.json")
        resp = httpx.Response(403, request=req)
        with patch.object(sec, "_sec_get_json", side_effect=httpx.HTTPStatusError("403 Forbidden", request=req, response=resp)):
            try:
                r_403 = sec.ingest_sec_edgar("AAPL")
                assert r_403.get("status") != "success" or r_403.get("rows_written", 0) == 0
            except Exception as exc:
                assert "403" in str(exc) or "User-Agent" in str(exc)
    else:
        from stock_research.tests.e2e import contract_harness as ch
        r_unk = ch.ingest_sec_edgar("ZZZZZZ", db_path=isolated_env["db_path"])
        r_403 = ch.ingest_sec_edgar("AAPL", db_path=isolated_env["db_path"], simulate_403=True)
        assert r_unk["status"] == "failed" and r_unk["error_msg"]
        assert r_403["status"] == "failed" and "403" in r_403["error_msg"]


@pytest.mark.live_network
def test_T2_E2_04_finnhub_invalid_dates_and_429_handling(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        fh = importlib.import_module("stock_research.storage.ingest.finnhub_us")
        try:
            r_bad = fh.ingest_finnhub_earnings("2025-05-20", "2025-05-01")
            assert r_bad.get("status") != "success" or r_bad.get("rows_written", 0) == 0
        except ValueError:
            pass
    else:
        from stock_research.tests.e2e import contract_harness as ch
        with pytest.raises(ValueError):
            ch.ingest_finnhub_earnings("2025-05-20", "2025-05-01", db_path=isolated_env["db_path"])
        r_429 = ch.ingest_finnhub_news(symbol="AAPL", db_path=isolated_env["db_path"], simulate_429=True)
        assert r_429["status"] == "success" and r_429["rate_limit_retries"] >= 1


@pytest.mark.live_network
def test_T2_E2_05_reingestion_idempotent_no_duplicate_bars(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        ak = importlib.import_module("stock_research.storage.ingest.akshare_cn")
        ak.ingest_a_share_stock("000858", days=10)
        ak.ingest_a_share_stock("000858", days=10)
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ingest_a_share_stock("000858", days=10, db_path=isolated_env["db_path"])
        ch.ingest_a_share_stock("000858", days=10, db_path=isolated_env["db_path"])
        con = ch.init_db(isolated_env["db_path"])

    try:
        dups = con.execute(
            "SELECT trade_date, COUNT(*) AS cnt FROM market_bars WHERE symbol='000858' GROUP BY trade_date HAVING COUNT(*) > 1"
        ).fetchall()
        assert dups == []
    finally:
        con.close()


# ===========================================================================
# E3. JEVDB Hybrid Semantic Query & MCP Boundaries (5 Tests)
# ===========================================================================

def test_T2_E3_01_hybrid_query_empty_filter_returns_empty_rows(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
        con.close()
        bridge = importlib.import_module("stock_research.storage.jevdb_bridge")
        res = bridge.hybrid_semantic_query(
            {"filters": {"symbol": "999999"}, "condition": "Any", "endpoint": "http://127.0.0.1:1/v1"},
            db_path=isolated_env["db_path"],
        )
    else:
        from stock_research.tests.e2e import contract_harness as ch
        res = ch.hybrid_semantic_query({"filters": {"symbol": "999999"}, "condition": "Any"}, db_path=isolated_env["db_path"])

    assert res["rows"] == []


def test_T2_E3_02_llm_shim_handles_reasoning_content_and_bad_json(e2e_mode: str):
    mode_holder = {"kind": "fenced"}

    class FlakyUpstream(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            _ = self.rfile.read(n)
            if mode_holder["kind"] == "fenced":
                content = "```json\n{\"answers\": [true]}\n```"
            else:
                content = "NOT VALID JSON AT ALL {{{{"
            out = {
                "id": "up-flaky",
                "object": "chat.completion",
                "model": "deepseek-flash",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "long reasoning chain",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }],
            }
            raw = json.dumps(out).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), FlakyUpstream)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    up_url = f"http://127.0.0.1:{srv.server_address[1]}/v1"

    try:
        if e2e_mode == "live":
            shim_mod = importlib.import_module("stock_research.storage.llm_shim")
            shim = shim_mod.ShimServer(("127.0.0.1", 0), upstream_url=up_url, api_key="test-key")
            shim_url = shim.start()
        else:
            from stock_research.tests.e2e import contract_harness as ch
            shim = ch.ShimServer(("127.0.0.1", 0), upstream_url=up_url, api_key="test-key")
            shim_url = shim.start()

        try:
            payload = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "hi"}]}
            st1, body1 = _http_json("POST", f"{shim_url}/chat/completions", payload)
            assert st1 == 200
            msg = body1["choices"][0]["message"]
            assert "reasoning_content" not in msg
            assert json.loads(msg["content"]) == {"answers": [True]}

            mode_holder["kind"] = "malformed"
            with pytest.raises(HTTPError) as exc_info:
                _http_json("POST", f"{shim_url}/chat/completions", payload)
            assert exc_info.value.code in (400, 502)
        finally:
            shim.stop()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_T2_E3_03_llm_shim_never_forwards_strict_json_schema(e2e_mode: str):
    captured: list[dict[str, Any]] = []

    class CaptureUpstream(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            captured.append(json.loads(self.rfile.read(n).decode("utf-8")))
            raw = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "{\"answers\": [false]}"}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), CaptureUpstream)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    up_url = f"http://127.0.0.1:{srv.server_address[1]}/v1"

    try:
        if e2e_mode == "live":
            shim_mod = importlib.import_module("stock_research.storage.llm_shim")
            shim = shim_mod.ShimServer(("127.0.0.1", 0), upstream_url=up_url, api_key="test-key")
            shim_url = shim.start()
        else:
            from stock_research.tests.e2e import contract_harness as ch
            shim = ch.ShimServer(("127.0.0.1", 0), upstream_url=up_url, api_key="test-key")
            shim_url = shim.start()

        try:
            payload = {
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "test"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "s", "strict": True, "schema": {"type": "object"}},
                },
            }
            _http_json("POST", f"{shim_url}/chat/completions", payload)
            assert len(captured) == 1
            assert captured[0]["response_format"] == {"type": "json_object"}
            assert "json_schema" not in captured[0]["response_format"]
            assert "strict" not in captured[0]["response_format"]
        finally:
            shim.stop()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_T2_E3_04_mcp_unknown_tool_and_bad_args_keeps_server_alive(e2e_mode: str, isolated_env: dict[str, Path]):
    cmd = _mcp_cmd(e2e_mode, isolated_env)
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "submit_paper_order", "arguments": {"symbol": "000858", "side": "BUY", "quantity": -10}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "web_fetch_url", "arguments": {"url": "http://127.0.0.1:8000/"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "paper_account_status", "arguments": {}}},
    ]
    resps = _mcp_stdio_session(cmd, reqs, os.environ.copy())
    assert len(resps) == 5
    assert resps[1].get("error") or resps[1]["result"].get("isError") is True
    assert resps[2].get("error") or resps[2]["result"].get("isError") is True
    assert resps[3].get("error") or resps[3]["result"].get("isError") is True
    assert resps[4]["result"].get("isError", False) is False


def test_T2_E3_05_sbf_pruning_filters_non_overlapping_pairs(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        con = ch.init_db(isolated_env["db_path"])

    try:
        for idx in range(5):
            keys_list = ["target_key" if idx == 0 else f"other_{idx}"]
            kw = json.dumps(keys_list)
            con.execute(
                """
                INSERT OR REPLACE INTO news_events
                (event_id, symbol, market, sector, published_at, title, summary, url, source, sentiment_label, keywords_json, keys)
                VALUES (?, '000858', 'CN', '白酒', '2025-05-01 09:00:00', ?, ?, '', 'sbf_test', 'positive', ?, ?)
                """,
                [f"sbf_{idx}", f"Title {idx}", f"Summary {idx}", kw, keys_list],
            )
    finally:
        con.close()

    if e2e_mode == "live":
        bridge = importlib.import_module("stock_research.storage.jevdb_bridge")
        with _offline_llm_shim() as shim_url:
            res = bridge.hybrid_semantic_query(
                {
                    "filters": {"symbol": "000858"},
                    "query_keys": ["target_key"],
                    "condition": "Positive",
                    "backend": "llm",
                    "endpoint": shim_url,
                    "api_key": "offline-e2e-key",
                },
                db_path=isolated_env["db_path"],
            )
    else:
        from stock_research.tests.e2e import contract_harness as ch
        res = ch.hybrid_semantic_query(
            {"filters": {"symbol": "000858"}, "query_keys": ["target_key"], "condition": "Positive"},
            db_path=isolated_env["db_path"],
        )

    assert len(res["rows"]) == 1
    row0 = res["rows"][0]
    assert (row0.get("record_id") or row0.get("event_id")) == "sbf_0"


# ===========================================================================
# E4. Agent, CLI & Web Tools Boundaries (5 Tests)
# ===========================================================================

def _get_web_tools(e2e_mode: str):
    if e2e_mode == "live":
        return importlib.import_module("stock_research.mcp_server.web_tools")
    from stock_research.tests.e2e import contract_harness as ch
    return ch


def test_T2_E4_01_web_fetch_blocks_ssrf_private_and_loopback_ips(e2e_mode: str):
    wt = _get_web_tools(e2e_mode)
    ssrf_urls = [
        "http://127.0.0.1:8000/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://[::1]/",
    ]
    for url in ssrf_urls:
        with pytest.raises(Exception):
            wt.web_fetch_url(url)


def test_T2_E4_02_web_fetch_blocks_non_http_schemes_and_bad_urls(e2e_mode: str):
    wt = _get_web_tools(e2e_mode)
    for bad in ("file:///etc/passwd", "ftp://example.com", "javascript:alert(1)", "", "not-a-url"):
        with pytest.raises(Exception):
            wt.web_fetch_url(bad)


def test_T2_E4_03_web_search_empty_query_rejected(e2e_mode: str):
    wt = _get_web_tools(e2e_mode)
    for q in ("", "   "):
        with pytest.raises(Exception):
            wt.web_search(q)


def test_T2_E4_04_cli_ask_empty_prompt_rejected(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        proc = subprocess.run(
            [sys.executable, "-m", "stock_research.cli", "ask", ""],
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
    else:
        from stock_research.tests.e2e import contract_harness as ch
        with pytest.raises(ValueError):
            ch.contract_run_research_query("", isolated_env["db_path"], isolated_env["ledger_db_path"], isolated_env["strategy_dir"])


def test_T2_E4_05_verifier_flags_ungrounded_numbers_or_missing_sources(e2e_mode: str):
    tool_calls = [{"name": "query_stored_market_data", "args": {"symbol": "000858"}, "result_summary": "close=133.67", "duration_ms": 10}]
    bad_answer = "五粮液收盘价999.99元，虚构盈利888.88亿元。"
    if e2e_mode == "live":
        v_mod = importlib.import_module("stock_research.agent.verifier")
        fn = getattr(v_mod, "verify_answer", None) or getattr(v_mod, "verify_response", None) or getattr(v_mod, "verify_answer_grounding", None)
        res = _to_dict(fn(answer=bad_answer, tool_calls=tool_calls, sources=[]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        res = ch.verify_answer_grounding(bad_answer, tool_calls, [])

    assert res.get("grounded", res.get("passed")) is False or len(res.get("ungrounded_numbers", [])) > 0


# ===========================================================================
# E5. Quant Strategy & Backtest Boundaries (5 Tests)
# ===========================================================================

def test_T2_E5_01_backtest_zero_trades_flat_equity(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    never_rules = {"buy": {"const": False}, "sell": {"const": False}}
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        spec = st_mod.StrategySpec(
            strategy_id="never_trigger_strat",
            name="Never Trigger",
            style="quant_daily",
            market="CN",
            description="No trades",
            params={"position_weight": 0.0},
            rules=never_rules,
            source="user",
        )
        st_mod.save_strategy(spec)
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        res = _to_dict(bt_mod.run_backtest("never_trigger_strat", ["000858"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN"))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        spec = ch.StrategySpec(
            strategy_id="never_trigger_strat",
            name="Never Trigger",
            style="quant_daily",
            market="CN",
            description="No trades",
            params={"position_weight": 0.0},
            rules=never_rules,
            source="user",
        )
        ch.save_strategy(spec, isolated_env["strategy_dir"])
        res = ch.run_backtest("never_trigger_strat", ["000858"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])

    assert res["trades"] == []
    assert all(row["equity"] == pytest.approx(100000.0) for row in res["equity_curve"])
    assert res["metrics"]["total_return"] == pytest.approx(0.0)
    assert res["metrics"]["max_drawdown"] == pytest.approx(0.0)


def test_T2_E5_02_backtest_single_trading_day(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        res = _to_dict(bt_mod.run_backtest("cn_daily_momentum_sentiment", ["000858"], "2025-04-24", "2025-04-24", 100000.0, 42, "CN"))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        res = ch.run_backtest("cn_daily_momentum_sentiment", ["000858"], "2025-04-24", "2025-04-24", 100000.0, 42, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])

    assert len(res["equity_curve"]) == 1
    assert res["metrics"]["max_drawdown"] == pytest.approx(0.0)
    assert math.isfinite(res["metrics"]["sharpe_ratio"])


def test_T2_E5_03_backtest_missing_strategy_or_empty_bars_error(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        with pytest.raises((KeyError, ValueError)):
            bt_mod.run_backtest("nonexistent_strat", ["000858"], "2025-04-24", "2025-05-15")
        with pytest.raises((KeyError, ValueError)):
            bt_mod.run_backtest("cn_daily_momentum_sentiment", ["000858"], "2099-01-01", "2099-01-05")
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        with pytest.raises((KeyError, ValueError)):
            ch.run_backtest("nonexistent_strat", ["000858"], "2025-04-24", "2025-05-15", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])
        with pytest.raises((KeyError, ValueError)):
            ch.run_backtest("cn_daily_momentum_sentiment", ["000858"], "2099-01-01", "2099-01-05", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])


def test_T2_E5_04_strategy_invalid_spec_rejected(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        with pytest.raises(Exception):
            st_mod.StrategySpec(strategy_id="bad_mkt", name="Bad", style="value", market="INVALID", description="Bad")
        with pytest.raises(ValueError):
            bt_mod.run_backtest("cn_daily_momentum_sentiment", ["000858"], "2025-04-24", "2025-05-15", initial_capital=-1000.0)
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        with pytest.raises(ValueError):
            ch.StrategySpec(strategy_id="bad_mkt", name="Bad", style="value", market="INVALID", description="Bad")
        with pytest.raises(ValueError):
            ch.run_backtest("cn_daily_momentum_sentiment", ["000858"], "2025-04-24", "2025-05-15", initial_capital=-1000.0, db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])


def test_T2_E5_05_backtest_determinism_invariant_to_unrelated_db_writes(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        r1 = _to_dict(bt_mod.run_backtest("cn_daily_momentum_sentiment", ["000858"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN"))
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
        con.execute(
            """
            INSERT OR REPLACE INTO market_bars (symbol, market, trade_date, open, high, low, close, volume, amount, source)
            VALUES ('AAPL', 'US', '2025-05-01', 210.0, 212.0, 209.0, 211.0, 50000000, 10550000000, 'yfinance')
            """
        )
        con.close()
        r2 = _to_dict(bt_mod.run_backtest("cn_daily_momentum_sentiment", ["000858"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN"))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        r1 = ch.run_backtest("cn_daily_momentum_sentiment", ["000858"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])
        ch.ingest_us_stock("AAPL", period="1mo", db_path=isolated_env["db_path"])
        r2 = ch.run_backtest("cn_daily_momentum_sentiment", ["000858"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])

    assert r1["reproducibility_hash"] == r2["reproducibility_hash"]
    assert r1["equity_curve"] == r2["equity_curve"]


# ===========================================================================
# E6. Risk Analytics Boundaries (5 Tests)
# ===========================================================================

def test_T2_E6_01_risk_flat_equity_curve_zero_division_safe(e2e_mode: str):
    risk_mod = _get_risk_mod(e2e_mode)
    curve = [{"date": f"2025-05-{i+1:02d}", "equity": 100000.0} for i in range(10)]
    m = _to_dict(risk_mod.compute_risk_metrics(curve))
    assert m["annualized_volatility"] == pytest.approx(0.0)
    assert m["sharpe_ratio"] == pytest.approx(0.0)
    assert m["max_drawdown"] == pytest.approx(0.0)
    for val in m.values():
        if isinstance(val, float):
            assert math.isfinite(val)


def test_T2_E6_02_risk_monotonically_declining_curve(e2e_mode: str):
    risk_mod = _get_risk_mod(e2e_mode)
    curve = [{"date": f"2025-05-{i+1:02d}", "equity": eq} for i, eq in enumerate([100000, 95000, 90000, 85000])]
    m = _to_dict(risk_mod.compute_risk_metrics(curve))
    assert m["total_return"] == pytest.approx(-0.15, abs=1e-6)
    assert m["max_drawdown"] == pytest.approx(0.15, abs=1e-6)
    assert m["sharpe_ratio"] < 0


def test_T2_E6_03_portfolio_risk_single_asset(e2e_mode: str):
    risk_mod = _get_risk_mod(e2e_mode)
    res = _to_dict(risk_mod.compute_portfolio_risk([{"symbol": "000858", "weight": 1.0}], {"000858": [100.0, 101.0, 99.5, 102.0]}))
    assert res["hhi"] == pytest.approx(1.0)
    assert res["correlation_matrix"] == {"000858": {"000858": 1.0}}
    assert math.isfinite(res["var_95"]) and math.isfinite(res["cvar_95"])


def test_T2_E6_04_portfolio_risk_empty_holdings_or_missing_prices(e2e_mode: str):
    risk_mod = _get_risk_mod(e2e_mode)
    r_empty = _to_dict(risk_mod.compute_portfolio_risk([], {"000858": [100.0, 101.0]}))
    assert r_empty["warnings"]
    assert r_empty["hhi"] == pytest.approx(0.0)
    assert r_empty["portfolio_volatility"] == pytest.approx(0.0)

    r_missing = _to_dict(risk_mod.compute_portfolio_risk([{"symbol": "MISSING", "weight": 1.0}], {"000858": [100.0, 101.0]}))
    assert any("MISSING" in w for w in r_missing["warnings"])
    assert r_missing["portfolio_volatility"] == pytest.approx(0.0)


def test_T2_E6_05_portfolio_risk_negatively_correlated_diversification(e2e_mode: str):
    risk_mod = _get_risk_mod(e2e_mode)
    rets = [0.02, -0.02, 0.03, -0.03, 0.015, -0.015, 0.025, -0.025]
    pa = [100.0]
    pb = [100.0]
    for r in rets:
        pa.append(pa[-1] * (1.0 + r))
        pb.append(pb[-1] * (1.0 - r))
    res = _to_dict(risk_mod.compute_portfolio_risk(
        [{"symbol": "A", "weight": 0.5}, {"symbol": "B", "weight": 0.5}],
        {"A": pa, "B": pb},
    ))
    assert res["correlation_matrix"]["A"]["B"] <= -0.95
    vol_a = _to_dict(risk_mod.compute_risk_metrics([{"date": f"2025-05-{i+1:02d}", "equity": p} for i, p in enumerate(pa)]))["annualized_volatility"]
    vol_b = _to_dict(risk_mod.compute_risk_metrics([{"date": f"2025-05-{i+1:02d}", "equity": p} for i, p in enumerate(pb)]))["annualized_volatility"]
    assert res["portfolio_volatility"] < 0.5 * (vol_a + vol_b)


# ===========================================================================
# E7. Paper-Trading Ledger Boundaries (6 Tests)
# ===========================================================================

def test_T2_E7_01_paper_reject_insufficient_cash(e2e_mode: str, isolated_env: dict[str, Path]):
    svc = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc.reset_account(10000.0)
    d1 = svc.get_account_summary()["current_date"]
    bar = {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}
    res = svc.submit_order("000858", "BUY", 100, market="CN", trade_date=d1, reference_bar=bar)
    assert res["accepted"] is False
    assert any(w in res["reason"].lower() for w in ("cash", "fund", "资金", "余额"))
    s = svc.get_account_summary()
    assert s["cash"] == pytest.approx(10000.0)
    assert s["positions"] == []


def test_T2_E7_02_paper_reject_oversell(e2e_mode: str, isolated_env: dict[str, Path]):
    svc = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc.reset_account(100000.0)
    d1 = svc.get_account_summary()["current_date"]
    d2 = _next_day(d1, 1)
    bar = {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}
    svc.submit_order("000858", "BUY", 100, market="CN", trade_date=d1, reference_bar=bar)
    svc.advance_day(d2, {"000858": 129.55})
    res = svc.submit_order("000858", "SELL", 200, market="CN", trade_date=d2, reference_bar=bar)
    assert res["accepted"] is False
    combined_reason = f"{res.get('reason', '')} {res.get('reject_code', '')}".lower()
    assert any(w in combined_reason for w in ("position", "holding", "held", "sellable", "exceeds", "持仓", "可卖"))
    assert svc.get_account_summary()["positions"][0]["quantity"] == 100


def test_T2_E7_03_paper_reject_cn_t_plus_one_same_day_sell(e2e_mode: str, isolated_env: dict[str, Path]):
    svc = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc.reset_account(100000.0)
    d1 = svc.get_account_summary()["current_date"]
    bar = {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}
    svc.submit_order("000858", "BUY", 100, market="CN", trade_date=d1, reference_bar=bar)
    res = svc.submit_order("000858", "SELL", 100, market="CN", trade_date=d1, reference_bar=bar)
    assert res["accepted"] is False
    assert "t+1" in res["reason"].lower() or "sellable" in res["reason"].lower()
    pos = svc.get_account_summary()["positions"][0]
    assert pos["quantity"] == 100
    assert pos["sellable_qty"] == 0


def test_T2_E7_04_paper_reject_non_100_cn_buy_lot_and_nonpositive_qty(e2e_mode: str, isolated_env: dict[str, Path]):
    svc = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc.reset_account(100000.0)
    d1 = svc.get_account_summary()["current_date"]
    bar = {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}
    for qty in (150, 50, 0, -100):
        res = svc.submit_order("000858", "BUY", qty, market="CN", trade_date=d1, reference_bar=bar)
        assert res["accepted"] is False
    assert svc.get_account_summary()["cash"] == pytest.approx(100000.0)


def test_T2_E7_05_paper_reject_price_limit_breach_and_out_of_range_limit(e2e_mode: str, isolated_env: dict[str, Path]):
    svc = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc.reset_account(100000.0)
    d1 = svc.get_account_summary()["current_date"]
    bar = {"open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0, "prev_close": 100.0}
    r_hi = svc.submit_order("000858", "BUY", 100, order_type="limit", limit_price=112.0, market="CN", trade_date=d1, reference_bar=bar)
    r_lo = svc.submit_order("000858", "BUY", 100, order_type="limit", limit_price=88.0, market="CN", trade_date=d1, reference_bar=bar)
    r_out = svc.submit_order("000858", "BUY", 100, order_type="limit", limit_price=95.0, market="CN", trade_date=d1, reference_bar=bar)
    assert all(r["accepted"] is False for r in (r_hi, r_lo, r_out))
    s = svc.get_account_summary()
    assert s["cash"] == pytest.approx(100000.0)
    assert s["positions"] == []
    assert len(svc.get_orders()) == 3


def test_T2_E7_06_paper_reject_invalid_side_or_missing_limit_price(e2e_mode: str, isolated_env: dict[str, Path]):
    svc = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc.reset_account(100000.0)
    r_side = svc.submit_order("000858", "SHORT", 100, market="CN")
    r_lim = svc.submit_order("000858", "BUY", 100, order_type="limit", limit_price=None, market="CN")
    assert r_side["accepted"] is False
    assert r_lim["accepted"] is False
    assert svc.get_account_summary()["cash"] == pytest.approx(100000.0)


# ===========================================================================
# E8. Web API & UI Boundaries (5 Tests)
# ===========================================================================

def test_T2_E8_01_api_agent_query_empty_prompt_returns_4xx(e2e_mode: str, isolated_env: dict[str, Path]):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        for body in ({"prompt": ""}, {}):
            with pytest.raises(HTTPError) as exc:
                _http_json("POST", f"{base_url}/api/agent/query", body)
            assert exc.value.code in (400, 422)
    finally:
        _stop_web_backend(handle)


def test_T2_E8_02_api_backtest_invalid_params_returns_4xx(e2e_mode: str, isolated_env: dict[str, Path]):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        for body in (
            {"strategy_id": "missing_strat_xyz", "symbols": ["000858"], "start_date": "2025-04-24", "end_date": "2025-05-15"},
            {"strategy_id": "cn_daily_momentum_sentiment", "symbols": ["000858"], "start_date": "2025-04-24", "end_date": "2025-05-15", "initial_capital": -1.0},
        ):
            with pytest.raises(HTTPError) as exc:
                _http_json("POST", f"{base_url}/api/quant/backtest", body)
            assert exc.value.code in (400, 404, 422)
    finally:
        _stop_web_backend(handle)


def test_T2_E8_03_api_paper_invalid_order_preserves_account_state(e2e_mode: str, isolated_env: dict[str, Path]):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        _http_json("POST", f"{base_url}/api/paper/reset", {"initial_capital": 100000.0})
        with pytest.raises(HTTPError) as exc1:
            _http_json("POST", f"{base_url}/api/paper/order", {})
        assert exc1.value.code in (400, 422)

        try:
            st, body = _http_json("POST", f"{base_url}/api/paper/order", {"symbol": "000858", "side": "BUY", "quantity": 50, "market": "CN"})
            assert st == 200 and body["accepted"] is False
        except HTTPError as exc2:
            assert exc2.code in (400, 422)

        _, acc = _http_json("GET", f"{base_url}/api/paper/account")
        assert acc["cash"] == pytest.approx(100000.0)
        assert acc["positions"] == []
    finally:
        _stop_web_backend(handle)


def test_T2_E8_04_api_market_unknown_symbol_no_500(e2e_mode: str, isolated_env: dict[str, Path]):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        try:
            st, body = _http_json("GET", f"{base_url}/api/market/bars?symbol=UNKNOWN_999&market=CN")
            assert st == 200
            bars = body.get("bars", body) if isinstance(body, dict) else body
            assert bars == []
        except HTTPError as exc:
            assert exc.code == 404
    finally:
        _stop_web_backend(handle)


@pytest.mark.playwright
def test_T2_E8_05_ui_empty_agent_submit_shows_validation_no_crash(e2e_mode: str, isolated_env: dict[str, Path], playwright_runner):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        res = playwright_runner.run_steps([
            {"action": "goto", "url": f"{base_url}/"},
            {"action": "wait_for_selector", "selector": "[data-testid='agent-input']"},
            {"action": "fill", "selector": "[data-testid='agent-input']", "value": ""},
            {"action": "click", "selector": "[data-testid='agent-submit']"},
            {"action": "inspect_validation", "key": "validation"},
            {"action": "inspect_selectors", "selectors": ["[data-testid='agent-answer']"], "key": "after"},
        ])
        assert res["console_errors"] == []
        assert res["page_errors"] == []
        assert res["server_errors"] == []
        assert not any("/api/agent/query" in r["url"] or "/api/agent/stream" in r["url"] for r in res.get("api_requests", []))
        val_info = res["results"]["validation"]
        has_msg = bool(val_info.get("native_validation")) or len(val_info.get("visible_msgs", [])) > 0
        assert has_msg, f"Empty submit must display validation feedback, got: {val_info}"
    finally:
        _stop_web_backend(handle)


# ===========================================================================
# E9. R5 Documentation Boundaries (5 Tests)
# ===========================================================================

def test_T2_E9_01_docs_no_broken_local_path_references(e2e_mode: str):
    rb, ds = _get_docs_texts(e2e_mode)
    combined = rb + "\n" + ds
    paths = set(re.findall(r"(?:stock_research|docs_project)/[A-Za-z0-9_./-]+", combined))
    assert len(paths) >= 1
    if e2e_mode == "live":
        for rel in paths:
            p = REPO_ROOT / rel.rstrip(".,;:)`'\"")
            if "*" not in str(p) and not str(p).endswith("/"):
                assert p.exists(), f"Broken doc path reference: {rel}"


def test_T2_E9_02_docs_no_agpl_runtime_dependency(e2e_mode: str):
    _, ds = _get_docs_texts(e2e_mode)
    if "sec-edgar-mcp" in ds.lower():
        assert any(w in ds.lower() for w in ("excluded", "not used", "rejected", "排除", "未使用"))
    if e2e_mode == "live":
        pp = REPO_ROOT / "stock_research" / "pyproject.toml"
        assert pp.exists()
        assert "sec-edgar-mcp" not in pp.read_text(encoding="utf-8").lower()


def test_T2_E9_03_docs_no_unfinished_placeholders(e2e_mode: str):
    rb, ds = _get_docs_texts(e2e_mode)
    for name, txt in (("RUNBOOK.md", rb), ("DESIGN.md", ds)):
        assert not re.search(r"\b(TODO|TBD|FIXME)\b|<insert", txt), f"Unfinished placeholder in {name}"


def test_T2_E9_04_docs_required_env_vars_documented(e2e_mode: str):
    rb, _ = _get_docs_texts(e2e_mode)
    for var in ("DEEPSEEK_API_KEY", "FINNHUB_API_KEY", "SEC_USER_AGENT"):
        assert var in rb


def test_T2_E9_05_docs_paper_rules_match_contract_constants(e2e_mode: str):
    _, ds = _get_docs_texts(e2e_mode)
    low = ds.lower()
    assert "100" in ds
    assert "t+1" in low
    assert "0.03%" in ds or "0.0003" in ds
    assert "0.001%" in ds or "0.00001" in ds
    assert "0.05%" in ds or "0.0005" in ds
    assert "10%" in ds or "0.10" in ds
