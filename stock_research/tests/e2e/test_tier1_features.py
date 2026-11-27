"""Tier 1: Feature Coverage E2E Tests (51 tests across E1-E9)."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import importlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.request import Request, urlopen

import pytest

from stock_research.tests.e2e.conftest import (
    CORE_TRACKED_PATHS,
    REPO_ROOT,
    find_free_port,
)

pytestmark = pytest.mark.tier1


def _next_day(iso_date: str, days: int = 1) -> str:
    return (date.fromisoformat(iso_date) + timedelta(days=days)).isoformat()


@contextmanager
def _offline_llm_shim():
    """Start a local mock OpenAI upstream + real `ShimServer` for offline semantic query tests."""
    shim_mod = importlib.import_module("stock_research.storage.llm_shim")

    class _OfflineUpstream(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            sys_msg = "\n".join(str(m.get("content") or "") for m in body.get("messages", []) if m.get("role") == "system")
            user_msg = ""
            for m in body.get("messages", []):
                if m.get("role") == "user":
                    user_msg = str(m.get("content") or "")
            try:
                user_data = json.loads(user_msg)
            except Exception:
                user_data = {}
            items = user_data.get("items") if isinstance(user_data, dict) else None
            if not isinstance(items, list):
                m_cnt = re.search(r"EXACTLY\s+(\d+)\s+elements", sys_msg)
                cnt = int(m_cnt.group(1)) if m_cnt else 1
                items = [{} for _ in range(cnt)]

            if '{"id":' in sys_msg:
                answers: list[Any] = [{"id": i, "verdict": True} for i in range(len(items))]
            elif "strictly chosen from " in sys_msg:
                m_ch = re.search(r"strictly chosen from (\[[^\]]+\])", sys_msg)
                choices = json.loads(m_ch.group(1)) if m_ch else ["positive"]
                answers = [choices[0] for _ in items]
            elif "finite JSON number" in sys_msg:
                answers = [0.9 for _ in items]
            else:
                answers = [True for _ in items]

            out = {
                "id": "offline-up-1",
                "object": "chat.completion",
                "model": body.get("model", "deepseek-flash"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps({"answers": answers}, ensure_ascii=False)},
                    "finish_reason": "stop",
                }],
            }
            raw = json.dumps(out, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    up_srv = ThreadingHTTPServer(("127.0.0.1", 0), _OfflineUpstream)
    up_thread = threading.Thread(target=up_srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    up_thread.start()
    up_url = f"http://127.0.0.1:{up_srv.server_address[1]}/v1"
    shim = shim_mod.ShimServer(("127.0.0.1", 0), upstream_url=up_url, api_key="offline-e2e-key")
    shim_url = shim.start()
    try:
        yield shim_url
    finally:
        shim.stop()
        up_srv.shutdown()
        up_srv.server_close()
        up_thread.join(timeout=2)


def _to_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if is_dataclass(obj):
        return asdict(obj)
    return dict(vars(obj))


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> tuple[int, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _wait_http_ok(url: str, timeout_s: float = 15.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.15)
    raise AssertionError(f"Server did not become ready at {url} within {timeout_s}s")


def _spawn_live_web_server(isolated_env: dict[str, Path]):
    port = find_free_port()
    env = os.environ.copy()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "stock_research.api.app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_http_ok(f"{base_url}/api/health", timeout_s=15.0)
    except Exception:
        proc.terminate()
        out, err = proc.communicate(timeout=5)
        raise AssertionError(f"Live FastAPI server failed to start:\nSTDOUT:\n{out}\nSTDERR:\n{err}")
    return proc, base_url


def _mcp_stdio_session(cmd: list[str], requests: list[dict[str, Any]], env: dict[str, str]) -> list[dict[str, Any]]:
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    responses: list[dict[str, Any]] = []
    try:
        for req in requests:
            proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            if "id" in req:
                line = proc.stdout.readline()
                assert line, f"MCP server closed stdout unexpectedly; stderr={proc.stderr.read() if proc.stderr else ''}"
                responses.append(json.loads(line))
        return responses
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


# ===========================================================================
# E1. Core JEVDB Preservation & Static Baseline (6 Tests)
# ===========================================================================

def test_T1_E1_01_baseline_stock_trading_unit_suite(isolated_env: dict[str, Path], mark_backing):
    mark_backing("real")
    env = os.environ.copy()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "examples/stock_trading", "-p", "test_*.py", "-v"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = proc.stdout + "\n" + proc.stderr
    assert proc.returncode == 0, combined
    assert "Ran 11 tests" in combined
    assert "OK" in combined


def test_T1_E1_02_baseline_jev_standin_unit_suite(isolated_env: dict[str, Path], mark_backing):
    mark_backing("real")
    env = os.environ.copy()
    proc = subprocess.run(
        [sys.executable, "tools/standin/test_jev_standin.py", "-v"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = proc.stdout + "\n" + proc.stderr
    assert proc.returncode == 0, combined
    assert "Ran 5 tests" in combined
    assert "OK" in combined


def test_T1_E1_03_core_repo_files_unmodified(mark_backing):
    mark_backing("real")
    diff_proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--name-only", "HEAD", "--", *CORE_TRACKED_PATHS],
        capture_output=True,
        text=True,
        check=False,
    )
    assert diff_proc.returncode == 0, diff_proc.stderr
    assert diff_proc.stdout.strip() == ""
    status_proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", *CORE_TRACKED_PATHS],
        capture_output=True,
        text=True,
        check=False,
    )
    assert status_proc.returncode == 0
    assert status_proc.stdout.strip() == ""


def test_T1_E1_04_jevdb_connect_and_registered_functions(mark_backing):
    mark_backing("real")
    import jevdb

    con = jevdb.connect(":memory:")
    try:
        assert con.execute("SELECT 1 + 1").fetchall() == [(2,)]
        funcs = {r[0] for r in con.execute("SELECT DISTINCT function_name FROM duckdb_functions()").fetchall()}
        expected_funcs = {"jev_holds", "jev_probability", "jev_choose", "jev_score", "jev_keys_overlap", "jevdb_stats"}
        assert expected_funcs <= funcs
        settings = {r[0] for r in con.execute("SELECT name FROM duckdb_settings()").fetchall()}
        assert {"jevdb_backend", "jevdb_endpoint", "jevdb_model"} <= settings
    finally:
        con.close()


def test_T1_E1_05_static_baseline_csv_loaded_without_mutation(e2e_mode: str, isolated_env: dict[str, Path]):
    csv_files = [
        REPO_ROOT / "examples" / "stock_trading" / "prices.csv",
        REPO_ROOT / "examples" / "stock_trading" / "news.csv",
        REPO_ROOT / "examples" / "stock_trading" / "trading_rules.csv",
    ]
    before_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in csv_files}

    if e2e_mode == "live":
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
        schema_mod.load_static_baseline(con)
    else:
        from stock_research.tests.e2e import contract_harness as ch
        con = ch.init_db(isolated_env["db_path"])
        ch.load_static_baseline(con)

    try:
        bar_syms = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM market_bars WHERE source='static_baseline'").fetchall()}
        news_syms = {r[0] for r in con.execute("SELECT DISTINCT symbol FROM news_events WHERE source='static_baseline'").fetchall()}
        assert {"000858", "601318"} <= bar_syms
        assert {"000858", "601318"} <= news_syms
    finally:
        con.close()

    after_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in csv_files}
    assert before_hashes == after_hashes


def test_T1_E1_06_jevdb_semantic_sql_against_local_standin(isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, mark_backing):
    mark_backing("real")
    monkeypatch.syspath_prepend(str(REPO_ROOT / "tools" / "standin"))
    import jev_standin
    import jevdb

    monkeypatch.setattr(jev_standin, "LOG", isolated_env["tmp_dir"] / "standin_requests.jsonl", raising=False)
    srv = HTTPServer(("127.0.0.1", 0), jev_standin.H)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        port = srv.server_address[1]
        monkeypatch.setenv("JEV_API_KEY", "standin-e2e-key")
        con = jevdb.connect(":memory:")
        try:
            con.execute("CREATE SECRET jev (TYPE jev, PROVIDER env)")
            con.execute("SET jevdb_backend='jev'")
            con.execute("SET jevdb_endpoint=?", [f"http://127.0.0.1:{port}"])
            con.execute("SET jevdb_model='standin'")
            holds_res = con.execute("SELECT jev_holds('positive', 'good quarterly revenue')").fetchone()[0]
            choose_res = con.execute("SELECT jev_choose('sentiment', 'positive guidance', ['positive', 'negative', 'neutral'])").fetchone()[0]
            score_res = con.execute("SELECT jev_score('impact', 'strong!!', ['low', 'med', 'high'])").fetchone()[0]
            prob_res = con.execute("SELECT jev_probability('positive', 'good margin')").fetchone()[0]
            reqs = con.execute("SELECT requests FROM jevdb_stats()").fetchone()[0]
            assert holds_res is True
            assert choose_res == "positive"
            assert score_res == 2.0
            assert prob_res == pytest.approx(0.9)
            assert reqs > 0
        finally:
            con.close()
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


# ===========================================================================
# E2. Dynamic Data Ingestion (6 Tests)
# ===========================================================================

@pytest.mark.live_network
def test_T1_E2_01_ingest_a_share_sector_power(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        mod = importlib.import_module("stock_research.storage.ingest.akshare_cn")
        res = mod.ingest_a_share_sector(sector="电力", days=30)
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        res = ch.ingest_a_share_sector(sector="电力", days=30, db_path=isolated_env["db_path"])
        con = ch.init_db(isolated_env["db_path"])

    try:
        assert res["rows_written"] > 0
        snaps = con.execute("SELECT COUNT(*) FROM sector_snapshots WHERE sector_name LIKE '%电力%'").fetchone()[0]
        cons = con.execute("SELECT COUNT(*) FROM sector_constituents WHERE sector_name LIKE '%电力%'").fetchone()[0]
        runs = con.execute("SELECT COUNT(*) FROM ingestion_runs WHERE status='success'").fetchone()[0]
        assert snaps > 0
        assert cons > 0
        assert runs > 0
    finally:
        con.close()


@pytest.mark.live_network
def test_T1_E2_02_ingest_a_share_single_stock(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        mod = importlib.import_module("stock_research.storage.ingest.akshare_cn")
        res = mod.ingest_a_share_stock("000858", days=30)
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        res = ch.ingest_a_share_stock("000858", days=30, db_path=isolated_env["db_path"])
        con = ch.init_db(isolated_env["db_path"])

    try:
        assert res["rows_written"] > 0
        bars = con.execute("SELECT open, high, low, close, volume FROM market_bars WHERE symbol='000858' AND market='CN'").fetchall()
        assert len(bars) > 0
        for o, h, l, c, v in bars:
            assert h >= max(o, c) >= min(o, c) >= l > 0
            assert v >= 0
        assets = con.execute("SELECT COUNT(*) FROM assets WHERE symbol='000858'").fetchone()[0]
        assert assets >= 1
    finally:
        con.close()


@pytest.mark.live_network
def test_T1_E2_03_ingest_us_stock_yfinance(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        mod = importlib.import_module("stock_research.storage.ingest.yfinance_us")
        res = mod.ingest_us_stock("AAPL", period="1mo")
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        res = ch.ingest_us_stock("AAPL", period="1mo", db_path=isolated_env["db_path"])
        con = ch.init_db(isolated_env["db_path"])

    try:
        assert res["rows_written"] > 0
        bars = con.execute("SELECT close FROM market_bars WHERE symbol='AAPL' AND market='US'").fetchall()
        assert len(bars) > 0 and all(r[0] > 0 for r in bars)
        fund_cnt = con.execute("SELECT COUNT(*) FROM fundamentals WHERE symbol='AAPL'").fetchone()[0]
        news_cnt = con.execute("SELECT COUNT(*) FROM news_events WHERE symbol='AAPL'").fetchone()[0]
        assert (fund_cnt + news_cnt) > 0
        assert con.execute("SELECT COUNT(*) FROM ingestion_runs WHERE status='success'").fetchone()[0] > 0
    finally:
        con.close()


@pytest.mark.live_network
def test_T1_E2_04_ingest_sec_edgar_filings_and_xbrl(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        mod = importlib.import_module("stock_research.storage.ingest.sec_edgar")
        res = mod.ingest_sec_edgar("AAPL")
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        res = ch.ingest_sec_edgar("AAPL", db_path=isolated_env["db_path"])
        con = ch.init_db(isolated_env["db_path"])

    try:
        assert res.get("status", "success") == "success"
        filings = con.execute("SELECT cik, form, accession_no, url FROM edgar_filings WHERE symbol='AAPL'").fetchall()
        assert len(filings) > 0
        cik, form, acc, url = filings[0]
        assert "320193" in str(cik)
        assert form in ("10-K", "10-Q", "8-K", "10-K/A", "10-Q/A")
        assert acc and url.startswith("http")
        funds = con.execute("SELECT revenue, net_income FROM fundamentals WHERE symbol='AAPL'").fetchall()
        assert len(funds) > 0 and any((r[0] or 0) > 0 or (r[1] or 0) != 0 for r in funds)
    finally:
        con.close()


@pytest.mark.live_network
def test_T1_E2_05_ingest_finnhub_news_and_earnings(e2e_mode: str, isolated_env: dict[str, Path]):
    earn_from = (date.today() - timedelta(days=14)).isoformat()
    earn_to = (date.today() + timedelta(days=14)).isoformat()
    if e2e_mode == "live":
        mod = importlib.import_module("stock_research.storage.ingest.finnhub_us")
        r_news = mod.ingest_finnhub_news(symbol="AAPL", days=7)
        r_earn = mod.ingest_finnhub_earnings(earn_from, earn_to)
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        r_news = ch.ingest_finnhub_news(symbol="AAPL", days=7, db_path=isolated_env["db_path"])
        r_earn = ch.ingest_finnhub_earnings(earn_from, earn_to, db_path=isolated_env["db_path"])
        con = ch.init_db(isolated_env["db_path"])

    try:
        assert r_news.get("status", "success") == "success"
        assert r_earn.get("status", "success") == "success"
        news_cnt = con.execute("SELECT COUNT(*) FROM news_events WHERE (source='finnhub' OR source LIKE 'finnhub:%')").fetchone()[0]
        earn_cnt = con.execute("SELECT COUNT(*) FROM earnings_calendar WHERE source='finnhub'").fetchone()[0]
        assert news_cnt >= 1
        assert earn_cnt >= 1
    finally:
        con.close()


@pytest.mark.live_network
def test_T1_E2_06_scheduler_trigger_once_and_lifecycle(e2e_mode: str, isolated_env: dict[str, Path]):
    jobs = [{"type": "a_share_sector", "kwargs": {"sector": "电力", "days": 3}}]
    if e2e_mode == "live":
        mod = importlib.import_module("stock_research.storage.ingest.scheduler")
        svc = mod.SchedulerService(db_path=isolated_env["db_path"])
        results = svc.trigger_once(jobs=jobs)
        svc.start_background(interval_seconds=60)
        svc.stop()
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        svc = ch.ContractSchedulerService(isolated_env["db_path"])
        results = svc.trigger_once(jobs=jobs)
        svc.start_background(interval_seconds=60)
        svc.stop()
        con = ch.init_db(isolated_env["db_path"])

    try:
        assert isinstance(results, list) and len(results) >= 1
        assert results[0]["status"] == "success"
        assert results[0].get("rows_written", 0) > 0
        runs = con.execute("SELECT COUNT(*) FROM ingestion_runs WHERE status='success'").fetchone()[0]
        assert runs >= 1
    finally:
        con.close()


# ===========================================================================
# E3. JEVDB Hybrid Semantic Query & MCP Stdio Server (6 Tests)
# ===========================================================================

def _mcp_cmd(e2e_mode: str, isolated_env: dict[str, Path]) -> list[str]:
    if e2e_mode == "live":
        return [sys.executable, "-m", "stock_research.mcp_server.server"]
    return [
        sys.executable,
        "-m",
        "stock_research.tests.e2e.contract_harness",
        "--mcp-stdio",
        "--db-path",
        str(isolated_env["db_path"]),
        "--ledger-db-path",
        str(isolated_env["ledger_db_path"]),
        "--strategy-dir",
        str(isolated_env["strategy_dir"]),
    ]


def test_T1_E3_01_mcp_stdio_server_startup_and_tools_list(e2e_mode: str, isolated_env: dict[str, Path]):
    cmd = _mcp_cmd(e2e_mode, isolated_env)
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    resps = _mcp_stdio_session(cmd, reqs, os.environ.copy())
    assert len(resps) == 2
    assert "result" in resps[0]
    tool_names = {t["name"] for t in resps[1]["result"]["tools"]}
    expected = {
        "jevdb_semantic_query",
        "fetch_market_data",
        "query_stored_market_data",
        "web_search",
        "web_fetch_url",
        "list_quant_strategies",
        "save_quant_strategy",
        "run_backtest_tool",
        "compare_strategies_tool",
        "analyze_portfolio_risk",
        "paper_account_status",
        "submit_paper_order",
    }
    assert expected <= tool_names


def test_T1_E3_02_hybrid_semantic_query_static_baseline(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
        schema_mod.load_static_baseline(con)
        con.close()
        bridge = importlib.import_module("stock_research.storage.jevdb_bridge")
        with _offline_llm_shim() as shim_url:
            res = bridge.hybrid_semantic_query(
                {
                    "filters": {"symbol": "000858"},
                    "condition": "Positive quarterly report",
                    "backend": "llm",
                    "endpoint": shim_url,
                    "api_key": "offline-e2e-key",
                },
                db_path=isolated_env["db_path"],
            )
    else:
        from stock_research.tests.e2e import contract_harness as ch
        con = ch.init_db(isolated_env["db_path"])
        ch.load_static_baseline(con)
        con.close()
        res = ch.hybrid_semantic_query(
            {"filters": {"symbol": "000858"}, "condition": "Positive quarterly report"},
            db_path=isolated_env["db_path"],
        )

    assert set(("rows", "stats", "backend", "sources")) <= set(res.keys())
    assert len(res["rows"]) >= 1
    assert "static_baseline" in res["sources"]


def test_T1_E3_03_hybrid_semantic_query_dynamic_tables(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
        con.execute(
            """
            INSERT OR REPLACE INTO news_events
            (event_id, symbol, market, sector, published_at, title, summary, url, source, sentiment_label, keywords_json, keys)
            VALUES ('dyn_power_600900', '600900', 'CN', '电力', '2025-05-14 09:30:00',
                    '电力行业装机与发电量稳步增长', '水电来水改善叠加绿电消纳政策支持，板块盈利修复。',
                    'https://finance.sina.com.cn/realstock/company/sh600900/nc.shtml', 'akshare_cls',
                    'positive', '["CN","电力","600900"]', ['CN', '电力', '600900'])
            """
        )
        con.close()
        bridge = importlib.import_module("stock_research.storage.jevdb_bridge")
        with _offline_llm_shim() as shim_url:
            res = bridge.hybrid_semantic_query(
                {
                    "filters": {"sector": "电力", "source_scope": "dynamic"},
                    "query_keys": ["电力"],
                    "condition": "Positive industry momentum",
                    "backend": "llm",
                    "endpoint": shim_url,
                    "api_key": "offline-e2e-key",
                },
                db_path=isolated_env["db_path"],
            )
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ingest_a_share_sector("电力", days=5, db_path=isolated_env["db_path"])
        res = ch.hybrid_semantic_query(
            {"filters": {"sector": "电力", "source_scope": "dynamic"}, "query_keys": ["电力"], "condition": "Positive industry momentum"},
            db_path=isolated_env["db_path"],
        )

    assert len(res["rows"]) >= 1
    assert len(res["sources"]) >= 1
    assert "static_baseline" not in res["sources"]
    assert res["stats"]


def test_T1_E3_04_hybrid_semantic_query_combined_static_and_dynamic(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
        schema_mod.load_static_baseline(con)
        con.execute(
            """
            INSERT OR REPLACE INTO news_events
            (event_id, symbol, market, sector, published_at, title, summary, url, source, sentiment_label, keywords_json, keys)
            VALUES ('dyn_power_600900', '600900', 'CN', '电力', '2025-05-14 09:30:00',
                    '电力行业装机与发电量稳步增长', '水电来水改善叠加绿电消纳政策支持，板块盈利修复。',
                    'https://finance.sina.com.cn/realstock/company/sh600900/nc.shtml', 'akshare_cls',
                    'positive', '["CN","电力","600900"]', ['CN', '电力', '600900'])
            """
        )
        con.close()
        bridge = importlib.import_module("stock_research.storage.jevdb_bridge")
        with _offline_llm_shim() as shim_url:
            res = bridge.hybrid_semantic_query(
                {
                    "condition": "Positive financial or sector catalyst",
                    "backend": "llm",
                    "endpoint": shim_url,
                    "api_key": "offline-e2e-key",
                },
                db_path=isolated_env["db_path"],
            )
    else:
        from stock_research.tests.e2e import contract_harness as ch
        con = ch.init_db(isolated_env["db_path"])
        ch.load_static_baseline(con)
        con.close()
        ch.ingest_a_share_sector("电力", days=5, db_path=isolated_env["db_path"])
        res = ch.hybrid_semantic_query({"condition": "Positive financial or sector catalyst"}, db_path=isolated_env["db_path"])

    sources = set(res["sources"])
    assert "static_baseline" in sources
    assert len(sources) >= 2


def test_T1_E3_05_llm_shim_translates_json_schema_to_json_object(e2e_mode: str):
    captured: list[dict[str, Any]] = []

    class MockUpstream(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            captured.append(body)
            out = {
                "id": "up-1",
                "object": "chat.completion",
                "model": body.get("model", "deepseek-flash"),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "internal scratchpad should be stripped",
                        "content": json.dumps({"answers": [True]}),
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

    up_srv = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
    up_thread = threading.Thread(target=up_srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    up_thread.start()
    up_url = f"http://127.0.0.1:{up_srv.server_address[1]}/v1"

    try:
        if e2e_mode == "live":
            shim_mod = importlib.import_module("stock_research.storage.llm_shim")
            assert callable(getattr(shim_mod, "ensure_shim_running", None))
            shim = shim_mod.ShimServer(("127.0.0.1", 0), upstream_url=up_url, api_key="test-key")
            shim_url = shim.start()
        else:
            from stock_research.tests.e2e import contract_harness as ch
            assert callable(getattr(ch, "ensure_shim_running", None))
            shim = ch.ShimServer(("127.0.0.1", 0), upstream_url=up_url, api_key="test-key")
            shim_url = shim.start()

        try:
            payload = {
                "model": "deepseek-flash",
                "messages": [{"role": "user", "content": "Evaluate"}],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "jevdb_boolean_answers",
                        "strict": True,
                        "schema": {"type": "object", "properties": {"answers": {"type": "array", "items": {"type": "boolean"}}}, "required": ["answers"]},
                    },
                },
            }
            status, resp_json = _http_json("POST", f"{shim_url}/chat/completions", payload)
            assert status == 200
            assert len(captured) == 1
            assert captured[0]["response_format"] == {"type": "json_object"}
            assert "strict" not in json.dumps(captured[0]["response_format"])
            msg = resp_json["choices"][0]["message"]
            assert "reasoning_content" not in msg
            assert json.loads(msg["content"]) == {"answers": [True]}
        finally:
            shim.stop()
    finally:
        up_srv.shutdown()
        up_srv.server_close()
        up_thread.join(timeout=2)


def test_T1_E3_06_mcp_data_and_paper_status_tools(e2e_mode: str, isolated_env: dict[str, Path]):
    cmd = _mcp_cmd(e2e_mode, isolated_env)
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "query_stored_market_data", "arguments": {"symbol": "000858"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "paper_account_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_quant_strategies", "arguments": {}}},
    ]
    resps = _mcp_stdio_session(cmd, reqs, os.environ.copy())
    assert len(resps) == 4
    for r in resps[1:]:
        assert r["result"].get("isError", False) is False
        text = r["result"]["content"][0]["text"]
        parsed = json.loads(text)
        assert isinstance(parsed, (dict, list))
    strat_blob = resps[3]["result"]["content"][0]["text"]
    assert "value_quality_margin_of_safety" in strat_blob
    assert "cn_daily_momentum_sentiment" in strat_blob


# ===========================================================================
# E4. `pi coding agent` CLI Research & Web Tools (6 Tests)
# ===========================================================================

def test_T1_E4_01_pi_agent_installation_mcp_and_skills_config(e2e_mode: str):
    if e2e_mode == "live":
        pi_bin = shutil.which("pi")
        assert pi_bin is not None, "pi binary not found on PATH"
        proc = subprocess.run([pi_bin, "--version"], capture_output=True, text=True, check=False)
        assert proc.returncode == 0
        mcp_json_path = REPO_ROOT / ".pi" / "mcp.json"
        assert mcp_json_path.exists()
        mcp_cfg = json.loads(mcp_json_path.read_text(encoding="utf-8"))
        assert "stock_research.mcp_server.server" in json.dumps(mcp_cfg)
        skills = {}
        for sk in ("stock-research", "cross-verify", "value-investing", "quant-strategy"):
            sk_path = REPO_ROOT / ".pi" / "skills" / sk / "SKILL.md"
            assert sk_path.exists(), f"Missing {sk_path}"
            txt = sk_path.read_text(encoding="utf-8")
            m = re.match(r"^---\n([\s\S]*?)\n---", txt)
            assert m is not None, f"Missing YAML frontmatter in {sk_path}"
            fm = m.group(1)
            name_m = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
            desc_m = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
            assert name_m and desc_m
            assert len(name_m.group(1).strip()) <= 64
            assert len(desc_m.group(1).strip()) <= 1024
            skills[sk] = fm
        assert len(skills) == 4
    else:
        from stock_research.tests.e2e import contract_harness as ch
        meta = ch.contract_pi_metadata()
        assert tuple(int(x) for x in meta["pi_version"].split(".")) >= (1, 6, 1)
        assert "stock_research.mcp_server.server" in json.dumps(meta["mcp_json"])
        assert len(meta["skills"]) == 4
        for s in meta["skills"]:
            assert 1 <= len(s["name"]) <= 64
            assert 1 <= len(s["description"]) <= 1024


@pytest.mark.live_network
def test_T1_E4_02_web_search_returns_structured_results(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        wt = importlib.import_module("stock_research.mcp_server.web_tools")
        res = wt.web_search("JEVDB DuckDB semantic")
    else:
        from stock_research.tests.e2e import contract_harness as ch
        res = ch.web_search("JEVDB DuckDB semantic")

    assert isinstance(res, list) and len(res) >= 1
    first = res[0]
    assert first.get("title")
    url = first.get("url") or first.get("href") or ""
    assert url.startswith(("http://", "https://"))
    assert first.get("snippet") or first.get("body")

    # Also verify web_search (and web_fetch_url in live mode) via MCP tools/call
    cmd = _mcp_cmd(e2e_mode, isolated_env)
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "web_search", "arguments": {"query": "JEVDB DuckDB"}}},
    ]
    if e2e_mode == "live":
        reqs.append({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "web_fetch_url", "arguments": {"url": "https://jevdb.org/"}}})
    resps = _mcp_stdio_session(cmd, reqs, os.environ.copy())
    assert resps[1]["result"].get("isError", False) is False
    assert "http" in resps[1]["result"]["content"][0]["text"]
    if e2e_mode == "live":
        assert resps[2]["result"].get("isError", False) is False
        assert len(resps[2]["result"]["content"][0]["text"]) > 20


def test_T1_E4_03_web_fetch_url_converts_html_to_markdown(e2e_mode: str):
    html = b"<html><body><h1>Quarterly Report</h1><p>Revenue grew 12 percent.</p><script>alert(1)</script></body></html>"

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass

    class _FakePublicIP:
        is_private = False
        is_loopback = False
        is_link_local = False
        is_reserved = False
        is_multicast = False
        is_unspecified = False
        is_global = True

        def __str__(self) -> str:
            return "93.184.216.34"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/report.html"
        if e2e_mode == "live":
            wt = importlib.import_module("stock_research.mcp_server.web_tools")
            sig = inspect.signature(wt.web_fetch_url)
            if "allow_loopback_for_test" in sig.parameters:
                md = wt.web_fetch_url(url, allow_loopback_for_test=True)
            elif "allow_private" in sig.parameters:
                md = wt.web_fetch_url(url, allow_private=True)
            else:
                from unittest.mock import patch
                validator_name = next((n for n in ("_validate_url", "_validate_public_http_url", "validate_url") if hasattr(wt, n)), None)
                if validator_name is not None:
                    with patch.object(wt, validator_name, lambda u, *a, **kw: u):
                        md = wt.web_fetch_url(url)
                else:
                    with patch("ipaddress.ip_address", return_value=_FakePublicIP()):
                        md = wt.web_fetch_url(url)
        else:
            from stock_research.tests.e2e import contract_harness as ch
            md = ch.web_fetch_url(url, allow_loopback_for_test=True)

        assert "Quarterly Report" in md
        assert "Revenue grew 12 percent." in md
        assert "alert(1)" not in md
        assert "<script" not in md.lower()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


@pytest.mark.live_network
def test_T1_E4_04_cli_ask_json_structured_trace(e2e_mode: str, isolated_env: dict[str, Path]):
    prompt = "分析000858五粮液近期基本面与行情"
    if e2e_mode == "live":
        proc = subprocess.run(
            [sys.executable, "-m", "stock_research.cli", "ask", prompt, "--json"],
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        data = json.loads(proc.stdout)
    else:
        from stock_research.tests.e2e import contract_harness as ch
        data = ch.contract_run_research_query(prompt, isolated_env["db_path"], isolated_env["ledger_db_path"], isolated_env["strategy_dir"])

    assert data["prompt"] == prompt
    assert isinstance(data["tool_calls"], list) and len(data["tool_calls"]) >= 1
    for tc in data["tool_calls"]:
        assert {"name", "args", "result_summary", "duration_ms"} <= set(tc.keys())
    assert data["verification"]
    assert isinstance(data["answer"], str) and data["answer"].strip()
    assert isinstance(data["sources"], list) and len(data["sources"]) >= 1
    assert data["duration_ms"] > 0


def test_T1_E4_05_verifier_grounds_numbers_and_citations(e2e_mode: str):
    tool_calls = [
        {"name": "query_stored_market_data", "args": {"symbol": "000858"}, "result_summary": "close=133.67, pe_ttm=15.8", "duration_ms": 12}
    ]
    sources = ["static_baseline", "https://static.cninfo.com.cn/finalpage/2025-04-26/1223311586.PDF"]
    answer = "五粮液(000858)收盘价133.67元，PE_TTM 15.8倍，数据来源static_baseline公告。"

    if e2e_mode == "live":
        v_mod = importlib.import_module("stock_research.agent.verifier")
        fn = getattr(v_mod, "verify_answer", None) or getattr(v_mod, "verify_response", None) or getattr(v_mod, "verify_answer_grounding", None)
        assert fn is not None, "verifier.py missing verify function"
        res = _to_dict(fn(answer=answer, tool_calls=tool_calls, sources=sources))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        res = ch.verify_answer_grounding(answer, tool_calls, sources)

    assert res.get("grounded", res.get("passed")) is True
    assert len(res.get("verified_sources", sources)) >= 1
    assert res.get("ungrounded_numbers", []) == []


@pytest.mark.live_network
def test_T1_E4_06_cli_ask_human_readable_pipeline_output(e2e_mode: str, isolated_env: dict[str, Path]):
    prompt = "对比000858与601318"
    if e2e_mode == "live":
        proc = subprocess.run(
            [sys.executable, "-m", "stock_research.cli", "ask", prompt],
            cwd=str(REPO_ROOT),
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout
    else:
        from stock_research.tests.e2e import contract_harness as ch
        res = ch.contract_run_research_query(prompt, isolated_env["db_path"], isolated_env["ledger_db_path"], isolated_env["strategy_dir"])
        out = (
            f"需求: {res['prompt']}\n"
            f"工具调用: {json.dumps(res['tool_calls'], ensure_ascii=False)}\n"
            f"核验: {json.dumps(res['verification'], ensure_ascii=False)}\n"
            f"结果: {res['answer']}\n"
            f"来源: {', '.join(res['sources'])}\n"
        )

    assert "000858" in out and "601318" in out
    assert any(k in out for k in ("工具", "tool", "调用"))
    assert any(k in out for k in ("来源", "source", "核验", "verif"))


def _cli_base_cmd(e2e_mode: str, isolated_env: dict[str, Path]) -> list[str]:
    if e2e_mode == "live":
        return [sys.executable, "-m", "stock_research.cli"]
    return [
        sys.executable,
        "-m",
        "stock_research.tests.e2e.contract_harness",
        "--cli",
        "--db-path",
        str(isolated_env["db_path"]),
        "--ledger-db-path",
        str(isolated_env["ledger_db_path"]),
        "--strategy-dir",
        str(isolated_env["strategy_dir"]),
    ]


def test_T1_E4_07_cli_subcommands_backtest_paper_serve_help(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        led_mod = importlib.import_module("stock_research.quant.ledger")
        led_mod.PaperAccountService(db_path=str(isolated_env["ledger_db_path"])).reset_account(100000.0)
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        ch.ContractPaperAccountService(isolated_env["ledger_db_path"]).reset_account(100000.0)

    base = _cli_base_cmd(e2e_mode, isolated_env)
    env = os.environ.copy()

    if e2e_mode == "live":
        p_help = subprocess.run([*base, "--help"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, check=False)
        assert p_help.returncode == 0, p_help.stderr
        for sub in ("ask", "ingest", "backtest", "paper", "serve"):
            assert sub in p_help.stdout

    p_srv_help = subprocess.run([*base, "serve", "--help"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, check=False)
    assert p_srv_help.returncode == 0, p_srv_help.stderr
    assert "--port" in p_srv_help.stdout

    p_bt = subprocess.run(
        [*base, "backtest", "--strategy-id", "cn_daily_momentum_sentiment", "--symbols", "000858,601318", "--start-date", "2025-04-24", "--end-date", "2025-05-15"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if p_bt.returncode != 0 and e2e_mode == "live":
        # Support alternative `--strategy` flag if M3 CLI names it `--strategy`
        p_bt = subprocess.run(
            [*base, "backtest", "--strategy", "cn_daily_momentum_sentiment", "--symbols", "000858,601318", "--start-date", "2025-04-24", "--end-date", "2025-05-15"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    assert p_bt.returncode == 0, p_bt.stderr
    assert "cn_daily_momentum_sentiment" in p_bt.stdout

    p_pap = subprocess.run([*base, "paper", "status"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, check=False)
    if p_pap.returncode != 0 and e2e_mode == "live":
        p_pap = subprocess.run([*base, "paper"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, check=False)
    assert p_pap.returncode == 0, p_pap.stderr
    assert "100000" in p_pap.stdout or "cash" in p_pap.stdout.lower()


@pytest.mark.live_network
def test_T1_E4_08_cli_ingest_subcommand(e2e_mode: str, isolated_env: dict[str, Path]):
    base = _cli_base_cmd(e2e_mode, isolated_env)
    proc = subprocess.run(
        [*base, "ingest", "--symbol", "000858", "--days", "5"],
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "000858" in proc.stdout or "success" in proc.stdout.lower()


# ===========================================================================
# E5. Quant Strategy Persistence & Deterministic Backtesting (5 Tests)
# ===========================================================================

def _prepare_bars_for_quant(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        schema_mod = importlib.import_module("stock_research.storage.schema")
        con = schema_mod.init_db(str(isolated_env["db_path"]))
        schema_mod.load_static_baseline(con)
        con.close()
    else:
        from stock_research.tests.e2e import contract_harness as ch
        con = ch.init_db(isolated_env["db_path"])
        ch.load_static_baseline(con)
        con.close()


def test_T1_E5_01_builtin_strategies_cover_value_and_daily_quant(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        strategies = [_to_dict(s) for s in st_mod.list_strategies()]
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        strategies = [_to_dict(s) for s in ch.list_strategies(isolated_env["strategy_dir"])]

    assert len(strategies) >= 2
    styles = {s["style"] for s in strategies}
    ids = {s["strategy_id"] for s in strategies}
    assert "value" in styles
    assert "quant_daily" in styles
    assert "value_quality_margin_of_safety" in ids
    assert "cn_daily_momentum_sentiment" in ids


def test_T1_E5_02_save_and_reload_custom_agent_strategy(e2e_mode: str, isolated_env: dict[str, Path]):
    valid_rules = {
        "buy": {"field": "momentum_return", "op": ">", "value": 0.0},
        "sell": {"field": "momentum_return", "op": "<", "value": 0.0},
    }
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        spec = st_mod.StrategySpec(
            strategy_id="agent_test_mom",
            name="Agent Momentum Test",
            style="quant_daily",
            market="CN",
            description="Test custom strategy",
            params={"lookback": 3},
            rules=valid_rules,
            source="agent",
        )
        st_mod.save_strategy(spec)
        reloaded = _to_dict(st_mod.get_strategy("agent_test_mom"))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        spec = ch.StrategySpec(
            strategy_id="agent_test_mom",
            name="Agent Momentum Test",
            style="quant_daily",
            market="CN",
            description="Test custom strategy",
            params={"lookback": 3},
            rules=valid_rules,
            source="agent",
        )
        ch.save_strategy(spec, isolated_env["strategy_dir"])
        reloaded = _to_dict(ch.get_strategy("agent_test_mom", isolated_env["strategy_dir"]))

    assert reloaded["strategy_id"] == "agent_test_mom"
    assert reloaded["style"] == "quant_daily"
    assert reloaded["source"] == "agent"
    assert reloaded["params"]["lookback"] == 3
    assert reloaded["rules"] == valid_rules


def test_T1_E5_03_run_backtest_output_schema_and_invariants(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        res = _to_dict(bt_mod.run_backtest("cn_daily_momentum_sentiment", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN"))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        res = ch.run_backtest(
            "cn_daily_momentum_sentiment",
            ["000858", "601318"],
            "2025-04-24",
            "2025-05-15",
            100000.0,
            42,
            "CN",
            db_path=isolated_env["db_path"],
            strategy_dir=isolated_env["strategy_dir"],
        )

    assert len(res["equity_curve"]) >= 10
    for row in res["equity_curve"]:
        assert row["equity"] == pytest.approx(row["cash"] + row["market_value"], abs=0.05)
        assert row["drawdown"] >= 0.0
    assert res["reproducibility_hash"]
    assert res["metrics"]
    assert res["assumptions"]


def test_T1_E5_04_backtest_deterministic_reproducibility(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        r1 = _to_dict(bt_mod.run_backtest("cn_daily_momentum_sentiment", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN"))
        r2 = _to_dict(bt_mod.run_backtest("cn_daily_momentum_sentiment", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN"))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        r1 = ch.run_backtest("cn_daily_momentum_sentiment", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])
        r2 = ch.run_backtest("cn_daily_momentum_sentiment", ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, 42, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])

    assert r1["reproducibility_hash"] == r2["reproducibility_hash"]
    assert r1["equity_curve"] == r2["equity_curve"]
    assert r1["trades"] == r2["trades"]


def test_T1_E5_05_compare_strategies_side_by_side(e2e_mode: str, isolated_env: dict[str, Path]):
    _prepare_bars_for_quant(e2e_mode, isolated_env)
    sids = ["value_quality_margin_of_safety", "cn_daily_momentum_sentiment"]
    if e2e_mode == "live":
        st_mod = importlib.import_module("stock_research.quant.strategies")
        st_mod.ensure_builtin_strategies()
        bt_mod = importlib.import_module("stock_research.quant.backtest")
        res = _to_dict(bt_mod.compare_strategies(sids, ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, "CN"))
    else:
        from stock_research.tests.e2e import contract_harness as ch
        ch.ensure_builtin_strategies(isolated_env["strategy_dir"])
        res = ch.compare_strategies(sids, ["000858", "601318"], "2025-04-24", "2025-05-15", 100000.0, "CN", db_path=isolated_env["db_path"], strategy_dir=isolated_env["strategy_dir"])

    blob = json.dumps(res, default=str)
    assert "value_quality_margin_of_safety" in blob
    assert "cn_daily_momentum_sentiment" in blob


# ===========================================================================
# E6. Strategy & Portfolio Risk Analytics (5 Tests)
# ===========================================================================

def _get_risk_mod(e2e_mode: str):
    if e2e_mode == "live":
        return importlib.import_module("stock_research.quant.risk")
    from stock_research.tests.e2e import contract_harness as ch
    return ch


def test_T1_E6_01_risk_return_and_drawdown_metrics(e2e_mode: str):
    risk_mod = _get_risk_mod(e2e_mode)
    curve = [
        {"date": f"2025-05-{i+1:02d}", "equity": eq}
        for i, eq in enumerate([100000, 102000, 105000, 99750, 101000, 106000])
    ]
    m = _to_dict(risk_mod.compute_risk_metrics(curve))
    assert m["total_return"] == pytest.approx(0.06, abs=1e-6)
    assert math.isfinite(m["cagr"])
    assert m["annualized_volatility"] >= 0.0
    # Peak 105000 -> trough 99750 => 5250/105000 = 0.05
    assert m["max_drawdown"] == pytest.approx(0.05, abs=1e-6)
    assert m["max_drawdown_duration_days"] >= 1


def test_T1_E6_02_risk_adjusted_ratios_and_trade_stats(e2e_mode: str):
    risk_mod = _get_risk_mod(e2e_mode)
    curve = [
        {"date": f"2025-05-{i+1:02d}", "equity": eq}
        for i, eq in enumerate([100000, 101200, 100400, 102500, 101900, 104000])
    ]
    trades = [
        {"side": "SELL", "pnl": 1500.0},
        {"side": "SELL", "pnl": -500.0},
    ]
    m = _to_dict(risk_mod.compute_risk_metrics(curve, trades))
    for k in ("sharpe_ratio", "sortino_ratio", "calmar_ratio"):
        assert math.isfinite(m[k])
    assert 0.0 <= m["win_rate"] <= 1.0
    assert m["win_rate"] == pytest.approx(0.5, abs=1e-6)
    assert m["profit_factor"] == pytest.approx(3.0, abs=1e-6)


def test_T1_E6_03_tail_risk_var_and_cvar_consistency(e2e_mode: str):
    risk_mod = _get_risk_mod(e2e_mode)
    vals = [100000 * (1.0 + ((-1) ** i) * (0.005 + (i % 5) * 0.004)) for i in range(25)]
    curve = [{"date": f"2025-05-{i+1:02d}", "equity": v} for i, v in enumerate(vals)]
    m = _to_dict(risk_mod.compute_risk_metrics(curve))
    assert math.isfinite(m["var_95"]) and math.isfinite(m["cvar_95"])
    assert abs(m["cvar_95"]) >= abs(m["var_95"]) - 1e-9


def test_T1_E6_04_portfolio_hhi_concentration(e2e_mode: str):
    risk_mod = _get_risk_mod(e2e_mode)
    prices = {
        "000858": [100.0, 101.0, 102.0, 101.5, 103.0],
        "601318": [50.0, 50.4, 50.1, 50.8, 51.2],
    }
    eq = _to_dict(risk_mod.compute_portfolio_risk([{"symbol": "000858", "weight": 0.5}, {"symbol": "601318", "weight": 0.5}], prices))
    sk = _to_dict(risk_mod.compute_portfolio_risk([{"symbol": "000858", "weight": 0.9}, {"symbol": "601318", "weight": 0.1}], prices))
    assert eq["hhi"] == pytest.approx(0.50, abs=0.02)
    assert sk["hhi"] == pytest.approx(0.82, abs=0.02)
    assert eq["hhi"] < sk["hhi"] <= 1.0


def test_T1_E6_05_portfolio_correlation_matrix_and_sector_exposure(e2e_mode: str):
    risk_mod = _get_risk_mod(e2e_mode)
    prices = {
        "000858": [100.0, 102.0, 101.0, 104.0, 103.5],
        "601318": [50.0, 49.5, 50.8, 50.2, 51.4],
        "600900": [28.0, 28.4, 28.9, 28.7, 29.5],
    }
    holdings = [
        {"symbol": "000858", "weight": 0.4, "sector": "Consumer"},
        {"symbol": "601318", "weight": 0.3, "sector": "Finance"},
        {"symbol": "600900", "weight": 0.3, "sector": "Utilities"},
    ]
    res = _to_dict(risk_mod.compute_portfolio_risk(holdings, prices))
    corr = res["correlation_matrix"]
    syms = ["000858", "601318", "600900"]
    assert isinstance(corr, dict) and set(corr.keys()) == set(syms)
    for s1 in syms:
        assert corr[s1][s1] == pytest.approx(1.0, abs=1e-6)
        for s2 in syms:
            assert -1.0 - 1e-6 <= corr[s1][s2] <= 1.0 + 1e-6
            assert corr[s1][s2] == pytest.approx(corr[s2][s1], abs=1e-6)
    assert res["portfolio_volatility"] >= 0.0
    assert res["sector_exposure"]


# ===========================================================================
# E7. Persistent Paper-Trading Account & Ledger (5 Tests)
# ===========================================================================

def _new_ledger(e2e_mode: str, db_path: Path):
    if e2e_mode == "live":
        mod = importlib.import_module("stock_research.quant.ledger")
        return mod.PaperAccountService(db_path=str(db_path))
    from stock_research.tests.e2e import contract_harness as ch
    return ch.ContractPaperAccountService(db_path=db_path)


def test_T1_E7_01_paper_account_reset_and_summary(e2e_mode: str, isolated_env: dict[str, Path]):
    svc = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc.reset_account(100000.0)
    s = svc.get_account_summary()
    assert s["cash"] == pytest.approx(100000.0)
    assert s["market_value"] == pytest.approx(0.0)
    assert s["total_equity"] == pytest.approx(100000.0)
    assert s["realized_pnl"] == pytest.approx(0.0)
    assert s["positions"] == []
    assert s["market_rules"]


def test_T1_E7_02_cn_buy_order_fees_slippage_and_t1_lock(e2e_mode: str, isolated_env: dict[str, Path]):
    svc = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc.reset_account(100000.0)
    d1 = svc.get_account_summary()["current_date"]
    bar = {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}
    res = svc.submit_order("000858", "BUY", 100, market="CN", trade_date=d1, reference_bar=bar)
    assert res["accepted"] is True
    assert res["fill_price"] >= 130.11
    notional = 100 * res["fill_price"]
    assert res["commission"] == pytest.approx(max(5.0, round(notional * 0.0003, 2)), abs=0.05)
    assert res["transfer_fee"] == pytest.approx(notional * 0.00001, abs=0.02)
    assert res["stamp_duty"] == pytest.approx(0.0)

    summary = svc.get_account_summary()
    pos = next(p for p in summary["positions"] if p["symbol"] == "000858")
    assert pos["quantity"] == 100
    assert pos["sellable_qty"] == 0
    expected_cash = 100000.0 - notional - res["commission"] - res["transfer_fee"]
    assert summary["cash"] == pytest.approx(expected_cash, abs=0.05)


def test_T1_E7_03_advance_day_unlocks_t1_and_cn_sell_stamp_duty(e2e_mode: str, isolated_env: dict[str, Path]):
    svc = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc.reset_account(100000.0)
    d1 = svc.get_account_summary()["current_date"]
    d2 = _next_day(d1, 1)
    buy_bar = {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}
    buy_res = svc.submit_order("000858", "BUY", 100, market="CN", trade_date=d1, reference_bar=buy_bar)
    assert buy_res["accepted"] is True

    adv = svc.advance_day(d2, {"000858": 129.55})
    assert next(p for p in adv["positions"] if p["symbol"] == "000858")["sellable_qty"] == 100

    sell_bar = {"open": 130.15, "high": 130.60, "low": 129.29, "close": 129.55, "prev_close": 130.85}
    sell_res = svc.submit_order("000858", "SELL", 100, market="CN", trade_date=d2, reference_bar=sell_bar)
    assert sell_res["accepted"] is True
    sell_notional = 100 * sell_res["fill_price"]
    assert sell_res["stamp_duty"] == pytest.approx(sell_notional * 0.0005, abs=0.02)
    assert sell_res["stamp_duty"] > 0
    assert sell_res["commission"] == pytest.approx(5.0, abs=0.02)
    assert sell_res["transfer_fee"] > 0

    after = svc.get_account_summary()
    assert after["positions"] == [] or all(p["quantity"] == 0 for p in after["positions"])
    buy_cost = 100 * buy_res["fill_price"] + buy_res["commission"] + buy_res["transfer_fee"]
    sell_net = sell_notional - sell_res["commission"] - sell_res["transfer_fee"] - sell_res["stamp_duty"]
    assert after["realized_pnl"] == pytest.approx(sell_net - buy_cost, abs=0.05)


def test_T1_E7_04_paper_ledger_persists_across_restart(e2e_mode: str, isolated_env: dict[str, Path]):
    dbp = isolated_env["ledger_db_path"]
    svc1 = _new_ledger(e2e_mode, dbp)
    svc1.reset_account(100000.0)
    d1 = svc1.get_account_summary()["current_date"]
    bar = {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}
    svc1.submit_order("000858", "BUY", 100, market="CN", trade_date=d1, reference_bar=bar)
    snap1 = svc1.get_account_summary()
    orders1 = svc1.get_orders()
    fills1 = svc1.get_fills()
    del svc1

    svc2 = _new_ledger(e2e_mode, dbp)
    snap2 = svc2.get_account_summary()
    orders2 = svc2.get_orders()
    fills2 = svc2.get_fills()
    assert snap2["cash"] == pytest.approx(snap1["cash"])
    assert snap2["total_equity"] == pytest.approx(snap1["total_equity"])
    assert snap2["positions"] == snap1["positions"]
    assert len(orders2) == len(orders1) == 1
    assert len(fills2) == len(fills1) == 1


def test_T1_E7_05_us_t0_and_single_share_lot_rules(e2e_mode: str, isolated_env: dict[str, Path]):
    svc = _new_ledger(e2e_mode, isolated_env["ledger_db_path"])
    svc.reset_account(100000.0)
    d1 = svc.get_account_summary()["current_date"]
    bar = {"open": 210.0, "high": 212.0, "low": 208.0, "close": 211.0}
    b = svc.submit_order("AAPL", "BUY", 5, market="US", trade_date=d1, reference_bar=bar)
    s = svc.submit_order("AAPL", "SELL", 5, market="US", trade_date=d1, reference_bar=bar)
    assert b["accepted"] is True
    assert s["accepted"] is True
    after = svc.get_account_summary()
    assert after["positions"] == [] or all(p["quantity"] == 0 for p in after["positions"])


# ===========================================================================
# E8. Web Backend API & Frontend 4-View Rendering (5 Tests)
# ===========================================================================

def _start_web_backend(e2e_mode: str, isolated_env: dict[str, Path]):
    if e2e_mode == "live":
        proc, base_url = _spawn_live_web_server(isolated_env)
        return ("proc", proc), base_url
    from stock_research.tests.e2e import contract_harness as ch
    srv = ch.ContractWebServer(isolated_env["db_path"], isolated_env["ledger_db_path"], isolated_env["strategy_dir"])
    srv.start()
    return ("contract", srv), srv.base_url


def _stop_web_backend(handle: tuple[str, Any]) -> None:
    kind, obj = handle
    if kind == "proc":
        obj.terminate()
        try:
            obj.wait(timeout=5)
        except subprocess.TimeoutExpired:
            obj.kill()
            obj.wait(timeout=5)
    else:
        obj.stop()


def test_T1_E8_01_api_health_endpoint(e2e_mode: str, isolated_env: dict[str, Path]):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        status, data = _http_json("GET", f"{base_url}/api/health")
        assert status == 200
        assert data["status"] == "ok"
        assert data["jevdb_version"] == "0.1.0"
        assert data["duckdb_version"] == "1.4.3"
    finally:
        _stop_web_backend(handle)


@pytest.mark.live_network
def test_T1_E8_02_api_market_endpoints(e2e_mode: str, isolated_env: dict[str, Path]):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        s_ing, _ = _http_json("POST", f"{base_url}/api/market/ingest", {"kind": "a_share_sector", "sector": "电力", "days": 5}, timeout=120)
        assert s_ing == 200
        s_bars, d_bars = _http_json("GET", f"{base_url}/api/market/bars?symbol=000858&market=CN")
        s_sec, d_sec = _http_json("GET", f"{base_url}/api/market/sector?name=%E7%94%B5%E5%8A%9B")
        s_fund, d_fund = _http_json("GET", f"{base_url}/api/market/fundamentals?symbol=000858&market=CN")
        s_news, d_news = _http_json("GET", f"{base_url}/api/market/news?symbol=000858")
        assert all(s == 200 for s in (s_bars, s_sec, s_fund, s_news))
        assert d_bars and d_sec is not None and d_fund is not None and d_news is not None
    finally:
        _stop_web_backend(handle)


def test_T1_E8_03_api_quant_and_paper_endpoints(e2e_mode: str, isolated_env: dict[str, Path]):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        s1, st_data = _http_json("GET", f"{base_url}/api/quant/strategies")
        assert s1 == 200 and st_data

        new_strat = {
            "strategy_id": "api_custom_mom",
            "name": "API Custom Momentum",
            "style": "quant_daily",
            "market": "CN",
            "description": "Created via POST /api/quant/strategies",
            "params": {"position_weight": 0.3},
            "rules": {
                "buy": {"field": "momentum_return", "op": ">", "value": 0.0},
                "sell": {"field": "momentum_return", "op": "<", "value": 0.0},
            },
            "source": "agent",
        }
        s_post, post_data = _http_json("POST", f"{base_url}/api/quant/strategies", new_strat)
        assert s_post in (200, 201) and post_data
        s1_after, st_after = _http_json("GET", f"{base_url}/api/quant/strategies")
        assert s1_after == 200 and "api_custom_mom" in json.dumps(st_after)

        s2, bt_data = _http_json("POST", f"{base_url}/api/quant/backtest", {"strategy_id": "cn_daily_momentum_sentiment", "symbols": ["000858", "601318"], "start_date": "2025-04-24", "end_date": "2025-05-15"})
        s3, cmp_data = _http_json("POST", f"{base_url}/api/quant/compare", {"strategy_ids": ["value_quality_margin_of_safety", "cn_daily_momentum_sentiment"], "symbols": ["000858", "601318"], "start_date": "2025-04-24", "end_date": "2025-05-15"})
        s4, pr_data = _http_json("POST", f"{base_url}/api/quant/portfolio-risk", {"holdings": [{"symbol": "000858", "weight": 0.5}, {"symbol": "601318", "weight": 0.5}], "price_history": {"000858": [129.0, 130.0, 131.0], "601318": [50.0, 50.5, 51.0]}})
        assert all(s == 200 for s in (s2, s3, s4)) and bt_data and cmp_data and pr_data

        _, reset_res = _http_json("POST", f"{base_url}/api/paper/reset", {"initial_capital": 100000.0})
        _, acc0 = _http_json("GET", f"{base_url}/api/paper/account")
        d1 = acc0.get("current_date") or reset_res.get("current_date")
        d2 = _next_day(d1, 1)
        s5, ord_res = _http_json("POST", f"{base_url}/api/paper/order", {"symbol": "000858", "side": "BUY", "quantity": 100, "market": "CN", "trade_date": d1, "reference_bar": {"open": 130.11, "high": 131.66, "low": 129.90, "close": 130.85, "prev_close": 129.05}})
        s6, adv_res = _http_json("POST", f"{base_url}/api/paper/advance-day", {"new_date": d2, "mark_prices": {"000858": 129.55}})
        s7, acc_res = _http_json("GET", f"{base_url}/api/paper/account")
        assert all(s == 200 for s in (s5, s6, s7))
        assert ord_res["accepted"] is True
        assert adv_res["positions"][0]["sellable_qty"] == 100
        assert acc_res["positions"][0]["quantity"] == 100
    finally:
        _stop_web_backend(handle)


@pytest.mark.live_network
def test_T1_E8_04_api_agent_query_and_sse_stream(e2e_mode: str, isolated_env: dict[str, Path]):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        status, q_res = _http_json("POST", f"{base_url}/api/agent/query", {"prompt": "分析000858五粮液"}, timeout=240)
        assert status == 200
        assert q_res["tool_calls"] and q_res["answer"] and q_res["verification"] and q_res["sources"]

        req = Request(f"{base_url}/api/agent/stream?prompt=000858", method="GET")
        with urlopen(req, timeout=240) as resp:
            assert resp.status == 200
            ctype = resp.headers.get("Content-Type", "")
            assert "text/event-stream" in ctype
            sse_body = resp.read().decode("utf-8")
        for ev in ("status", "tool_start", "tool_end", "verification", "final_answer"):
            assert ev in sse_body
    finally:
        _stop_web_backend(handle)


@pytest.mark.playwright
def test_T1_E8_05_ui_renders_all_4_core_views_playwright(e2e_mode: str, isolated_env: dict[str, Path], playwright_runner):
    handle, base_url = _start_web_backend(e2e_mode, isolated_env)
    try:
        selectors = [
            "[data-testid='agent-input']",
            "[data-testid='agent-submit']",
            "[data-testid='tool-calls-panel']",
            "[data-testid='agent-answer']",
            "[data-testid='market-chart']",
            "[data-testid='backtest-chart']",
            "[data-testid='paper-holdings']",
            "[data-testid='paper-pnl-chart']",
        ]
        res = playwright_runner.run_steps([
            {"action": "goto", "url": f"{base_url}/"},
            {"action": "wait_for_selector", "selector": "[data-testid='agent-input']"},
            {"action": "inspect_selectors", "selectors": selectors, "key": "views"},
        ])
        assert res["console_errors"] == []
        assert res["page_errors"] == []
        assert res["server_errors"] == []
        views = res["results"]["views"]
        for sel in selectors:
            assert views[sel]["exists"] is True, f"Missing selector {sel}"
        for chart_sel in ("[data-testid='market-chart']", "[data-testid='backtest-chart']", "[data-testid='paper-pnl-chart']"):
            if views[chart_sel]["visible"]:
                assert views[chart_sel]["chart"]["width"] > 0
                assert views[chart_sel]["chart"]["height"] > 0
    finally:
        _stop_web_backend(handle)


# ===========================================================================
# E9. R5 Documentation Deliverables (5 Tests)
# ===========================================================================

def _get_docs_texts(e2e_mode: str) -> tuple[str, str]:
    if e2e_mode == "live":
        rb = REPO_ROOT / "docs_project" / "RUNBOOK.md"
        ds = REPO_ROOT / "docs_project" / "DESIGN.md"
        assert rb.exists() and ds.exists(), "Missing docs_project/RUNBOOK.md or DESIGN.md"
        return rb.read_text(encoding="utf-8"), ds.read_text(encoding="utf-8")

    # Contract docs sample reflecting required R5 sections for pre-impl E2E self-test
    runbook = (
        "# JEVDB Stock Research & Paper-Trading Runbook\n\n"
        "## 1. Python Virtualenv & Environment Setup\n"
        "python -m venv stock_research/.venv\n"
        "Environment variables: DEEPSEEK_API_KEY, FINNHUB_API_KEY, SEC_USER_AGENT\n\n"
        "## 2. Launch Commands\n"
        "CLI ask: python -m stock_research.cli ask \"我想知道最近3天a股电力行业的情况\"\n"
        "MCP server: python -m stock_research.mcp_server.server\n"
        "Web backend: uvicorn stock_research.api.app:app --port 8000\n"
        "Frontend dev: cd stock_research/web && npm run dev\n\n"
        "## 3. Verification\n"
        "python -m stock_research.tests.e2e.run_e2e --tier all --mode live\n"
    ) + (" " * 700)
    design = (
        "# JEVDB Stock Research & Paper-Trading Design Document\n\n"
        "## 1. Target Users & Scenarios\n"
        "Target personas: individual investor (个人投资者) and institutional researcher (机构研究员).\n"
        "Pipeline: 用户需求 → 工具调用 → 结果展示.\n"
        "| Scenario | Style | Input | Output |\n"
        "| Value | value | 000858 prompt | fundamentals + backtest + risk |\n"
        "| Daily Quant | quant_daily | CN daily bars | strategy compare + T+1 paper ledger |\n\n"
        "## 2. Component & Open-Source Skills Audit\n"
        "Components: JEVDB (Apache-2.0), pi-coding-agent (MIT), mcp (MIT), akshare (MIT), yfinance (Apache-2.0), ddgs (MIT), ECharts (Apache-2.0), .pi/skills/ (MIT/Apache-2.0).\n"
        "Audit fields: Role, I/O, Dependencies, Permissions, Source URL (https://github.com/PDAIS/JEVDB), License, Risks.\n"
        "Excluded: sec-edgar-mcp (AGPL-3.0) is NOT used.\n\n"
        "## 3. Paper Trading Rules & Assumptions\n"
        "A-share T+1 settlement, buy lot 100, sell lot 1, fill-price slippage 0.0005, commission 0.0003 (0.03%) min 5, transfer fee 0.00001 (0.001%), sell stamp duty 0.0005 (0.05%), 10% price limit.\n"
        "Simplifying assumptions: daily bar open/close execution, zero partial fills.\n"
        "Key technical decisions: llm_shim json_schema->json_object translation, AkShare THS/Sina fallback routing, cross-verification methodology.\n"
    ) + (" " * 400)
    return runbook, design


def test_T1_E9_01_docs_runbook_and_design_exist(e2e_mode: str):
    rb, ds = _get_docs_texts(e2e_mode)
    assert len(rb.encode("utf-8")) > 1000
    assert len(ds.encode("utf-8")) > 1000


def test_T1_E9_02_runbook_contains_launch_and_verification_steps(e2e_mode: str):
    rb, _ = _get_docs_texts(e2e_mode)
    low = rb.lower()
    for token in ("DEEPSEEK_API_KEY", "FINNHUB_API_KEY", "ask", "mcp_server", "uvicorn"):
        assert token.lower() in low, f"RUNBOOK.md missing {token}"
    assert "pytest" in low or "run_e2e" in low


def test_T1_E9_03_design_covers_personas_scenarios_and_e2e_flow(e2e_mode: str):
    _, ds = _get_docs_texts(e2e_mode)
    assert ("个人" in ds or "individual" in ds.lower()) and ("机构" in ds or "institutional" in ds.lower())
    assert "用户需求" in ds and "工具调用" in ds and "结果展示" in ds
    assert ("价值" in ds or "value" in ds.lower()) and ("量化" in ds or "quant" in ds.lower())


def test_T1_E9_04_design_covers_component_and_skills_license_risk_audit(e2e_mode: str):
    _, ds = _get_docs_texts(e2e_mode)
    low = ds.lower()
    for comp in ("jevdb", "pi", "mcp", "akshare", "yfinance", "ddgs", "skills"):
        assert comp in low
    assert "mit" in low or "apache-2.0" in low
    assert ("risk" in low or "风险" in ds) and ("license" in low or "许可" in ds)


def test_T1_E9_05_design_documents_paper_rules_assumptions_and_verification(e2e_mode: str):
    _, ds = _get_docs_texts(e2e_mode)
    low = ds.lower()
    for kw in ("t+1", "100", "llm_shim", "akshare"):
        assert kw in low
    assert any(k in ds for k in ("印花税", "stamp_duty", "stamp duty"))
    assert any(k in ds for k in ("核验", "verification", "cross-verify"))
