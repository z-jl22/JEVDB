"""Offline unit/integration tests and live end-to-end tests for M3 (MCP, Skills, Web, Pi Agent, CLI)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from stock_research.agent.pi_runner import run_research_query
from stock_research.agent.verifier import verify_answer, verify_answer_grounding
from stock_research.mcp_server import web_tools
from stock_research.mcp_server.web_tools import SSRFError, web_fetch_url, web_search
from stock_research.storage.schema import db_session, load_static_baseline

REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_ENABLED = os.getenv("RUN_LIVE_TESTS", "0") == "1"


def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch | None = None) -> dict[str, str]:
    rt = tmp_path / "runtime"
    db_path = rt / "market_research.duckdb"
    ledger_path = rt / "paper_ledger.duckdb"
    strat_dir = rt / "strategies"
    rt.mkdir(parents=True, exist_ok=True)
    strat_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    overrides = {
        "STOCK_RESEARCH_RUNTIME_DIR": str(rt),
        "STOCK_RESEARCH_CACHE_DIR": str(rt / "cache"),
        "STOCK_RESEARCH_DB_PATH": str(db_path),
        "STOCK_RESEARCH_LEDGER_DB_PATH": str(ledger_path),
        "STOCK_RESEARCH_STRATEGY_DIR": str(strat_dir),
    }
    env.update(overrides)
    if monkeypatch is not None:
        for k, v in overrides.items():
            monkeypatch.setenv(k, v)
    return env


# ---------------------------------------------------------------------------
# 1. Pi Installation, .pi/mcp.json & 4 Open-Source Adapted Skills
# ---------------------------------------------------------------------------


def test_pi_binary_mcp_json_and_skills_metadata() -> None:
    pi_bin = shutil.which("pi")
    assert pi_bin is not None, "pi binary must be on PATH"
    proc = subprocess.run([pi_bin, "--version"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "1.6.1" in proc.stdout

    mcp_path = REPO_ROOT / ".pi" / "mcp.json"
    assert mcp_path.exists()
    mcp_cfg = json.loads(mcp_path.read_text(encoding="utf-8"))
    srv = mcp_cfg["mcpServers"]["stock_research"]
    assert (REPO_ROOT / srv["command"]).absolute() == REPO_ROOT / "stock_research/.venv/bin/python"
    assert srv["args"] == ["-m", "stock_research.mcp_server.server"]
    assert srv["exposure"] == "direct"
    assert srv["timeout"] == 120

    expected_skills = ("stock-research", "cross-verify", "value-investing", "quant-strategy")
    for sk in expected_skills:
        sk_file = REPO_ROOT / ".pi" / "skills" / sk / "SKILL.md"
        assert sk_file.exists(), f"Missing skill file: {sk_file}"
        txt = sk_file.read_text(encoding="utf-8")
        m = re.match(r"^---\n([\s\S]*?)\n---", txt)
        assert m is not None, f"Missing YAML frontmatter in {sk_file}"
        fm = m.group(1)
        name_m = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
        desc_m = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
        lic_m = re.search(r"^license:\s*(.+)$", fm, re.MULTILINE)
        assert name_m and name_m.group(1).strip() == sk
        assert len(name_m.group(1).strip()) <= 64
        assert desc_m and 10 <= len(desc_m.group(1).strip()) <= 1024
        assert lic_m and any(x in lic_m.group(1) for x in ("MIT", "Apache-2.0"))
        assert "PROVENANCE" in txt
        assert "github.com/" in txt


# ---------------------------------------------------------------------------
# 2. Web Tools (DuckDuckGo Normalization + SSRF & Redirect Guards)
# ---------------------------------------------------------------------------


def test_web_search_validation_and_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(ValueError):
            web_search(bad)
    with pytest.raises(ValueError):
        web_search("电力", search_type="invalid")

    class FakeDDGS:
        def text(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
            return [
                {"title": "Relative Redirect", "href": "/go/v1/abc", "body": "skip me"},
                {"title": "Jina Proxy", "href": "https://r.jina.ai/http://example.com", "body": "skip jina"},
                {"title": "Power Sector Report", "href": "https://finance.example.com/power", "body": "A股电力板块上涨1.85%"},
            ]

        def news(self, query: str, max_results: int = 5) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(web_tools, "DDGS", FakeDDGS)
    res = web_search("A股 电力", max_results=5)
    assert len(res) == 1
    assert res[0]["title"] == "Power Sector Report"
    assert res[0]["url"] == "https://finance.example.com/power"
    assert "1.85%" in res[0]["snippet"]


def test_web_fetch_ssrf_blocks_private_schemes_and_redirects() -> None:
    blocked_urls = [
        "http://127.0.0.1:8000/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://metadata.internal/",
        "http://printer.local/",
        "https://r.jina.ai/https://example.com",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "javascript:alert(1)",
        "",
        "not-a-url",
    ]
    for url in blocked_urls:
        with pytest.raises(SSRFError):
            web_fetch_url(url)


def test_web_fetch_loopback_test_mode_html_markdown_and_redirect_guard() -> None:
    html = (
        b"<html><head><style>body{color:red}</style></head>"
        b"<body><h1>Quarterly Report</h1><p>Revenue grew 12 percent.</p>"
        b"<script>alert(1)</script></body></html>"
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.path == "/redirect-ssrf":
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        md = web_fetch_url(f"{base}/report.html", allow_loopback_for_test=True, max_chars=200)
        assert "Quarterly Report" in md
        assert "Revenue grew 12 percent." in md
        assert "alert(1)" not in md
        assert "<script" not in md.lower()

        with pytest.raises(SSRFError):
            web_fetch_url(f"{base}/redirect-ssrf", allow_loopback_for_test=True)
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 3. MCP Stdio Server (All 12 Tools + Deduplication + Error Persistence)
# ---------------------------------------------------------------------------


def test_mcp_stdio_all_12_tools_dedup_and_error_persistence(tmp_path: Path) -> None:
    env = _isolated_env(tmp_path)
    env.pop("DEEPSEEK_API_KEY", None)
    db_path = Path(env["STOCK_RESEARCH_DB_PATH"])

    # Seed baseline + duplicate sector_constituents rows from two sources to verify QUALIFY ROW_NUMBER() dedup
    with db_session(db_path, init=True) as con:
        load_static_baseline(con)
        con.execute(
            """
            INSERT INTO sector_constituents
            (sector_name, symbol, name, price, change_pct, pe, pb, market_cap, updated_at, source)
            VALUES
              ('电力', '600900', '长江电力', 29.10, 1.10, 20.1, 2.9, 700000000000, TIMESTAMP '2026-11-24 15:00:00', 'akshare_sina'),
              ('电力', '600900', '长江电力', 29.45, 2.18, 20.5, 3.0, 710000000000, TIMESTAMP '2026-11-25 15:00:00', 'akshare_ths')
            """
        )

    async def _run() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "stock_research.mcp_server.server"],
            cwd=str(REPO_ROOT),
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == "stock_research"

                tools_res = await session.list_tools()
                names = {t.name for t in tools_res.tools}
                expected_12 = {
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
                assert expected_12 <= names

                # 1. Deduplication check on sector_constituents
                q_cons = await session.call_tool(
                    "query_stored_market_data",
                    {"table": "sector_constituents", "sector": "电力"},
                )
                assert not q_cons.is_error
                cons_data = json.loads(q_cons.content[0].text)
                assert cons_data["count"] == 1
                assert cons_data["rows"][0]["price"] == pytest.approx(29.45)
                assert cons_data["rows"][0]["source"] == "akshare_ths"

                # 2. JEVDB semantic query (auto -> mock when DEEPSEEK_API_KEY unset)
                sem = await session.call_tool(
                    "jevdb_semantic_query",
                    {"condition": "Positive quarterly growth", "filters": {"symbol": "000858"}},
                )
                assert not sem.is_error
                sem_data = json.loads(sem.content[0].text)
                assert sem_data["backend"] == "mock"
                assert len(sem_data["rows"]) >= 1
                assert "static_baseline" in sem_data["sources"]

                # 3. Quant tools chain: list -> save -> backtest -> compare -> risk
                listed = await session.call_tool("list_quant_strategies", {})
                assert not listed.is_error
                assert len(json.loads(listed.content[0].text)["strategies"]) >= 2

                saved = await session.call_tool(
                    "save_quant_strategy",
                    {
                        "strategy_id": "m3_test_daily_quant",
                        "name": "M3 Test Daily Quant",
                        "style": "quant_daily",
                        "market": "CN",
                        "description": "Test strategy",
                        "params": {"position_weight": 0.35},
                        "rules": {
                            "buy": {"field": "momentum_return", "op": ">", "value": 0.0},
                            "sell": {"field": "momentum_return", "op": "<", "value": 0.0},
                        },
                    },
                )
                assert not saved.is_error

                bt = await session.call_tool(
                    "run_backtest_tool",
                    {
                        "strategy_id": "m3_test_daily_quant",
                        "symbols": ["000858", "601318"],
                        "start_date": "2025-04-24",
                        "end_date": "2025-05-15",
                    },
                )
                assert not bt.is_error
                bt_data = json.loads(bt.content[0].text)
                assert bt_data["reproducibility_hash"]

                cmp_res = await session.call_tool(
                    "compare_strategies_tool",
                    {
                        "strategy_ids": ["value_quality_margin_of_safety", "m3_test_daily_quant"],
                        "symbols": ["000858", "601318"],
                        "start_date": "2025-04-24",
                        "end_date": "2025-05-15",
                    },
                )
                assert not cmp_res.is_error
                assert len(json.loads(cmp_res.content[0].text)["results"]) == 2

                risk = await session.call_tool(
                    "analyze_portfolio_risk",
                    {
                        "holdings": [
                            {"symbol": "000858", "weight": 0.5},
                            {"symbol": "601318", "weight": 0.5},
                        ]
                    },
                )
                assert not risk.is_error
                risk_data = json.loads(risk.content[0].text)
                assert risk_data["hhi"] == pytest.approx(0.5, abs=0.05)

                # 4. Paper trading order + status
                buy_ok = await session.call_tool(
                    "submit_paper_order",
                    {"symbol": "000858", "side": "BUY", "quantity": 100, "market": "CN"},
                )
                assert not buy_ok.is_error
                assert json.loads(buy_ok.content[0].text)["accepted"] is True

                # 5. Error handling & server liveness after ToolErrors
                bad_order = await session.call_tool(
                    "submit_paper_order",
                    {"symbol": "000858", "side": "BUY", "quantity": -10, "market": "CN"},
                )
                assert bad_order.is_error is True

                t1_viol = await session.call_tool(
                    "submit_paper_order",
                    {"symbol": "000858", "side": "SELL", "quantity": 100, "market": "CN"},
                )
                assert t1_viol.is_error is True

                bad_ssrf = await session.call_tool(
                    "web_fetch_url",
                    {"url": "http://127.0.0.1:8000/"},
                )
                assert bad_ssrf.is_error is True

                bad_search = await session.call_tool("web_search", {"query": "   "})
                assert bad_search.is_error is True

                # Server MUST still be alive and reflect the 100-share 000858 position
                status_after = await session.call_tool("paper_account_status", {})
                assert not status_after.is_error
                st_data = json.loads(status_after.content[0].text)
                assert st_data["positions"][0]["symbol"] == "000858"
                assert st_data["positions"][0]["quantity"] == 100

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 4. Verifier & Pi Runner (Mocked JSONL + Missing Key Failure)
# ---------------------------------------------------------------------------


def test_verifier_grounds_valid_and_rejects_ungrounded() -> None:
    tool_calls = [
        {
            "name": "query_stored_market_data",
            "args": {"symbol": "000858"},
            "result_summary": "close=133.67, pe_ttm=15.8, roe=0.224, revenue=36940000000",
            "duration_ms": 10,
        }
    ]
    sources = ["static_baseline", "https://static.cninfo.com.cn/finalpage/2025-04-26/1223311586.PDF"]

    good = verify_answer_grounding(
        "五粮液(000858)收盘价133.67元，PE_TTM 15.8倍，ROE 22.4%，营收369.40亿元，数据来源static_baseline。",
        tool_calls,
        sources,
    )
    assert good["verified"] is True
    assert good["grounded"] is True
    assert good["passed"] is True
    assert good["ungrounded_numbers"] == []

    bad = verify_answer("五粮液收盘价999.99元，虚构盈利888.88亿元。", tool_calls, [])
    assert bad["verified"] is False
    assert bad["grounded"] is False
    assert bad["passed"] is False
    assert "999.99" in bad["ungrounded_numbers"]
    assert "888.88" in bad["ungrounded_numbers"]
    assert len(bad["unverified_claims"]) >= 2


def test_pi_runner_validation_missing_key_and_jsonl_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_env(tmp_path, monkeypatch)

    with pytest.raises(ValueError):
        run_research_query("   ")

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        run_research_query("我想知道最近3天a股电力行业的情况")

    # Mock subprocess.Popen with strict LF-delimited JSONL containing U+2028 inside JSON string
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-mock-key")
    jsonl_events = [
        {"type": "session", "version": 3, "id": "sess-1", "cwd": str(REPO_ROOT)},
        {"type": "agent_start"},
        {
            "type": "tool_execution_start",
            "toolCallId": "tc1",
            "toolName": "mcp__stock_research__fetch_market_data",
            "args": {"source": "akshare_sector", "sector": "电力", "days": 3},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "tc1",
            "toolName": "mcp__stock_research__fetch_market_data",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "source": "akshare_ths",
                                "sector": "电力",
                                "change_pct": 1.85,
                                "leader": "600900",
                                "note": "line1\u2028line2",
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
            "isError": False,
            "durationMs": 22,
        },
        {
            "type": "tool_execution_start",
            "toolCallId": "tc2",
            "toolName": "mcp__stock_research__web_search",
            "args": {"query": "最近3天 A股 电力行业"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "tc2",
            "toolName": "mcp__stock_research__web_search",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"results": [{"title": "电力研报", "url": "https://finance.example.com/power", "snippet": "涨1.85%"}]},
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
            "isError": False,
            "durationMs": 15,
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [
                    {
                        "type": "text",
                        "text": "最近3天A股电力行业涨幅1.85%，领涨股600900。来源：akshare_ths 与 https://finance.example.com/power",
                    }
                ],
            },
        },
        {"type": "agent_settled", "aborted": False},
    ]
    raw_stdout = ("\n".join(json.dumps(e, ensure_ascii=False) for e in jsonl_events) + "\n").encode("utf-8")

    class FakePipe:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._pos = 0

        def read1(self, n: int = 8192) -> bytes:
            if self._pos >= len(self._data):
                return b""
            chunk = self._data[self._pos:self._pos + n]
            self._pos += len(chunk)
            return chunk

        def read(self, n: int = 4096) -> bytes:
            return self.read1(n)

    captured_cmd: list[str] = []

    class FakePopen:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            captured_cmd.extend(cmd)
            self.stdout = FakePipe(raw_stdout)
            self.stderr = FakePipe(b"")

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return

    monkeypatch.setattr("stock_research.agent.pi_runner.subprocess.Popen", FakePopen)

    events: list[dict[str, Any]] = []
    res = run_research_query("我想知道最近3天a股电力行业的情况", on_event=events.append)

    assert "--mode" in captured_cmd and "json" in captured_cmd
    assert "--approve" in captured_cmd
    assert "read,mcp__*" in captured_cmd
    assert res["duration_ms"] > 0
    assert len(res["tool_calls"]) == 2
    assert res["tool_calls"][0]["name"] == "fetch_market_data"
    assert res["tool_calls"][1]["name"] == "web_search"
    assert "akshare_ths" in res["sources"]
    assert "https://finance.example.com/power" in res["sources"]
    assert res["verification"]["grounded"] is True
    assert [e["type"] for e in events] == ["status", "tool_start", "tool_end", "tool_start", "tool_end", "verification", "final_answer"]


# ---------------------------------------------------------------------------
# 5. CLI Subcommands (help, ask, ingest, backtest, paper, serve)
# ---------------------------------------------------------------------------


def test_cli_subcommands_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    env = _isolated_env(tmp_path, monkeypatch)

    # 1. --help & serve --help (subprocess)
    p_help = subprocess.run([sys.executable, "-m", "stock_research.cli", "--help"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
    assert p_help.returncode == 0
    for sub in ("ask", "ingest", "backtest", "paper", "serve"):
        assert sub in p_help.stdout

    p_srv = subprocess.run([sys.executable, "-m", "stock_research.cli", "serve", "--help"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
    assert p_srv.returncode == 0
    assert "--port" in p_srv.stdout

    # 2. Empty ask rejected
    p_empty = subprocess.run([sys.executable, "-m", "stock_research.cli", "ask", "   "], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
    assert p_empty.returncode != 0

    # 3. Backtest CLI (--strategy-id and --strategy alias)
    p_bt1 = subprocess.run(
        [sys.executable, "-m", "stock_research.cli", "backtest", "--strategy-id", "cn_daily_momentum_sentiment", "--symbols", "000858,601318", "--start-date", "2025-04-24", "--end-date", "2025-05-15"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert p_bt1.returncode == 0, p_bt1.stderr
    assert "cn_daily_momentum_sentiment" in p_bt1.stdout

    p_bt2 = subprocess.run(
        [sys.executable, "-m", "stock_research.cli", "backtest", "--strategy", "value_quality_margin_of_safety", "--symbols", "000858,601318", "--start-date", "2025-04-24", "--end-date", "2025-05-15"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert p_bt2.returncode == 0, p_bt2.stderr
    assert "value_quality_margin_of_safety" in p_bt2.stdout

    # 4. Paper CLI (default status, reset, buy, invalid sell T+1, advance, sell)
    p_pap0 = subprocess.run([sys.executable, "-m", "stock_research.cli", "paper"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
    assert p_pap0.returncode == 0
    assert "100000" in p_pap0.stdout

    p_rst = subprocess.run([sys.executable, "-m", "stock_research.cli", "paper", "reset", "--capital", "100000", "--date", "2025-05-15"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
    assert p_rst.returncode == 0

    p_buy = subprocess.run([sys.executable, "-m", "stock_research.cli", "paper", "buy", "--symbol", "000858", "--quantity", "100", "--market", "CN", "--date", "2025-05-15"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
    assert p_buy.returncode == 0, p_buy.stderr

    p_t1 = subprocess.run([sys.executable, "-m", "stock_research.cli", "paper", "sell", "--symbol", "000858", "--quantity", "100", "--market", "CN", "--date", "2025-05-15"], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True)
    assert p_t1.returncode != 0  # T+1 violation rejected

    # 5. In-process ask & ingest CLI dispatch
    from stock_research import cli as cli_mod

    monkeypatch.setattr(
        cli_mod,
        "run_research_query",
        lambda prompt: {
            "prompt": prompt,
            "tool_calls": [{"name": "query_stored_market_data", "args": {"symbol": "000858"}, "result_summary": "close=133.67", "duration_ms": 10}],
            "verification": {"grounded": True, "passed": True, "ungrounded_numbers": []},
            "answer": "五粮液(000858)收盘价133.67元。",
            "sources": ["static_baseline"],
            "duration_ms": 12,
        },
    )
    assert cli_mod.main(["ask", "分析000858", "--json"]) == 0
    ask_json = json.loads(capsys.readouterr().out)
    assert ask_json["prompt"] == "分析000858"

    assert cli_mod.main(["ask", "分析000858"]) == 0
    ask_txt = capsys.readouterr().out
    assert "工具调用" in ask_txt and "交叉核验" in ask_txt and "133.67" in ask_txt

    monkeypatch.setattr(cli_mod, "ingest_a_share_stock", lambda symbol, days, force=False: {"status": "success", "symbol": symbol, "bars": 5})
    assert cli_mod.main(["ingest", "--symbol", "000858", "--days", "5"]) == 0
    assert "000858" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 5b. Iteration 2 Regression Tests (Concurrency, Validation, Verifier, Timeout, DNS Pinning)
# ---------------------------------------------------------------------------


def test_mcp_concurrent_gather_and_fetch_market_data_validation(tmp_path: Path) -> None:
    env = _isolated_env(tmp_path)
    env.pop("DEEPSEEK_API_KEY", None)

    async def _run() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "stock_research.mcp_server.server"],
            cwd=str(REPO_ROOT),
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Cold DB concurrent calls mixing db_session() + quant/risk + paper tools
                results = await asyncio.gather(
                    session.call_tool("query_stored_market_data", {"table": "market_bars", "symbol": "000858"}),
                    session.call_tool("jevdb_semantic_query", {"condition": "Positive growth", "backend": "mock", "filters": {"symbol": "000858"}}),
                    session.call_tool("run_backtest_tool", {"strategy_id": "cn_daily_momentum_sentiment", "symbols": ["000858", "601318"], "start_date": "2025-04-24", "end_date": "2025-05-15"}),
                    session.call_tool("compare_strategies_tool", {"strategy_ids": ["cn_daily_momentum_sentiment", "value_quality_margin_of_safety"], "symbols": ["000858", "601318"], "start_date": "2025-04-24", "end_date": "2025-05-15"}),
                    session.call_tool("analyze_portfolio_risk", {"holdings": [{"symbol": "000858", "weight": 0.5}, {"symbol": "601318", "weight": 0.5}]}),
                    session.call_tool("paper_account_status", {}),
                )
                for idx, res in enumerate(results):
                    assert res.is_error is False, f"Concurrent MCP tool idx={idx} failed: {res.content}"

                # fetch_market_data input validation errors must raise ToolError cleanly
                bad_calls = [
                    ("fetch_market_data", {"source": "akshare_sector", "sector": "电力", "days": 0}),
                    ("fetch_market_data", {"source": "akshare_stock", "symbol": "000858", "days": -3}),
                    ("fetch_market_data", {"source": "akshare_sector", "sector": "电力", "days": True}),
                    ("fetch_market_data", {"source": "finnhub_earnings", "from_date": "   ", "to_date": "2026-05-01"}),
                    ("fetch_market_data", {"source": "finnhub_earnings", "from_date": "2026-99-99", "to_date": "2026-05-01"}),
                    ("fetch_market_data", {"source": "finnhub_earnings", "from_date": "2026-05-10", "to_date": "2026-05-01"}),
                ]
                for tool_name, args in bad_calls:
                    res = await session.call_tool(tool_name, args)
                    assert res.is_error is True, f"Expected ToolError for {args}"

                # Server remains healthy after validation ToolErrors
                alive = await session.call_tool("paper_account_status", {})
                assert alive.is_error is False

    asyncio.run(_run())


def test_verifier_rejects_substring_global_unit_and_fabricated_urls_and_handles_derived_scaling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_calls = [
        {
            "name": "query_stored_market_data",
            "args": {"symbol": "000858.SZ"},
            "result_summary": (
                "date=2025-04-26T15:00:00 close=133.67, pe_ttm=15.8x, roe=0.224, "
                "revenue=123000000, q1=9710647471.03, q2=8954257202.51, q3=6474551792.18"
            ),
            "duration_ms": 10,
        }
    ]
    sources = ["static_baseline", "https://example.com/real-report"]

    # 1. Substring bypasses must be rejected
    c1 = verify_answer_grounding(
        "五粮液(000858)收盘价33.67元，PE 5.8倍，营收23000000元。来源：static_baseline",
        tool_calls,
        sources,
    )
    assert c1["grounded"] is False
    assert {"33.67", "5.8", "23000000"} <= set(c1["ungrounded_numbers"])

    c2 = verify_answer_grounding(
        "五粮液(000858)收盘价23元，目标价133.6元。来源：static_baseline",
        tool_calls,
        sources,
    )
    assert c2["grounded"] is False
    assert {"23", "133.6"} <= set(c2["ungrounded_numbers"])

    # 2. Global "万" / "亿" must NOT leak across clauses, and RMB/Market must not trigger B/M scaling
    c3 = verify_answer_grounding(
        "某指标100万元。五粮液虚构数值158500元，RMB口径1.23。来源：static_baseline",
        tool_calls,
        sources,
    )
    assert c3["grounded"] is False
    assert "158500" in c3["ungrounded_numbers"]
    assert "1.23" in c3["ungrounded_numbers"]

    # Even with "万" attached, 158500 vs 15.8*10000=158000 exceeds tight 0.1% tolerance
    c3b = verify_answer_grounding(
        "五粮液虚构数值158500万元。来源：static_baseline",
        tool_calls,
        sources,
    )
    assert c3b["grounded"] is False
    assert "158500" in c3b["ungrounded_numbers"]

    # 3. Fabricated URL & prefix-spoofed URL must fail grounded/verified/passed
    c4 = verify_answer_grounding(
        "五粮液(000858)收盘价133.67元，详情见 https://evil-fabricated.example.com/fake-report 。",
        tool_calls,
        sources,
    )
    assert c4["grounded"] is False
    assert c4["verified"] is False
    assert c4["passed"] is False
    assert "https://evil-fabricated.example.com/fake-report" in c4["unverified_urls"]

    c4_prefix = verify_answer_grounding(
        "五粮液(000858)收盘价133.67元，详情见 https://example.com/real 。",
        tool_calls,
        sources,
    )
    assert c4_prefix["grounded"] is False
    assert "https://example.com/real" in c4_prefix["unverified_urls"]

    # 4. Structured date/ticker/unit tokens + Markdown table column scaling + derived 12190352881.36 -> 121.90亿元
    good_structured = verify_answer_grounding(
        (
            "截至2025-04-26(20250426) 15:00，五粮液(000858)收盘133.67元，PE 15.8倍，ROE 22.4%。\n"
            "TTM净利润计算：9710647471.03 + 8954257202.51 - 6474551792.18 = 12190352881.36 元。\n"
            "核心要点：TTM净利润约121.90 亿元（简记121.90）。\n"
            "| 股票 | 营收(亿元) |\n"
            "| --- | --- |\n"
            "| 000858 | 1.23 |\n"
            "来源：static_baseline 与 https://example.com/real-report"
        ),
        tool_calls,
        sources,
    )
    assert good_structured["grounded"] is True, good_structured
    assert good_structured["ungrounded_numbers"] == []
    assert good_structured["unverified_urls"] == []

    # 5. pi_runner repair loop must accept a repair that fixes unverified_urls even when ungrounded_numbers == []
    _isolated_env(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-live-repair")
    jsonl_events = [
        {
            "type": "tool_execution_start",
            "toolCallId": "tc1",
            "toolName": "mcp__stock_research__query_stored_market_data",
            "args": {"symbol": "000858"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "tc1",
            "toolName": "mcp__stock_research__query_stored_market_data",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"source": "static_baseline", "close": 133.67, "url": "https://example.com/real-report"}
                        ),
                    }
                ]
            },
            "isError": False,
            "durationMs": 5,
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [
                    {
                        "type": "text",
                        "text": "五粮液收盘133.67元，来源 https://evil-fabricated.example.com/fake-report",
                    }
                ],
            },
        },
    ]
    raw_stdout = ("\n".join(json.dumps(e) for e in jsonl_events) + "\n").encode("utf-8")

    class _Pipe:
        def __init__(self, data: bytes) -> None:
            self._d = data
            self._i = 0

        def read1(self, n: int = 8192) -> bytes:
            c = self._d[self._i:self._i + n]
            self._i += len(c)
            return c

        def read(self, n: int = 4096) -> bytes:
            return self.read1(n)

    class _Popen:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            self.stdout = _Pipe(raw_stdout)
            self.stderr = _Pipe(b"")

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return

    monkeypatch.setattr("stock_research.agent.pi_runner.subprocess.Popen", _Popen)
    monkeypatch.setattr(
        "stock_research.agent.pi_runner._repair_ungrounded_answer",
        lambda **kw: "五粮液收盘133.67元，来源 https://example.com/real-report",
    )
    repaired_res = run_research_query("分析000858")
    assert repaired_res["verification"]["grounded"] is True
    assert repaired_res["verification"]["unverified_urls"] == []
    assert "evil-fabricated.example.com" not in repaired_res["answer"]


def test_pi_runner_timeout_kills_silent_hanging_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    _isolated_env(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-timeout-key")

    for bad_t in (0, -1, True, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            run_research_query("test timeout validation", timeout_seconds=bad_t)  # type: ignore[arg-type]

    real_popen = subprocess.Popen

    def _hanging_popen(cmd: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        return real_popen([sys.executable, "-c", "import time; time.sleep(5)"], **kwargs)

    monkeypatch.setattr("stock_research.agent.pi_runner.subprocess.Popen", _hanging_popen)
    t0 = _time.monotonic()
    with pytest.raises(TimeoutError, match="timed out"):
        run_research_query("test silent hang", timeout_seconds=0.3)
    elapsed = _time.monotonic() - t0
    assert elapsed < 1.2, f"Silent hang took too long to time out: {elapsed:.2f}s"


def test_web_tools_dns_rebinding_pinning_and_link_table_preservation(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket as _socket

    # 1. Verify links and tables are preserved in Markdown while <h1> headings stay intact
    html_rich = (
        "<html><body><h1>电力行业简报</h1>"
        "<p>长江电力(<a href='https://quote.example.com/600900'>600900</a>)上涨 2.18%。</p>"
        "<table><tr><th>代码</th><th>涨跌幅</th></tr><tr><td>600900</td><td>2.18%</td></tr></table>"
        "</body></html>"
    )
    md_rich = web_tools._html_to_markdown(html_rich, source_url="https://news.example.com/a", max_chars=5000)
    assert "电力行业简报" in md_rich
    assert "[600900](https://quote.example.com/600900)" in md_rich
    assert "| 代码 | 涨跌幅 |" in md_rich
    assert "| 600900 | 2.18% |" in md_rich

    # 2. Simulate DNS-rebinding TOCTOU:
    # 1st getaddrinfo (validation) returns a public IP (93.184.216.34);
    # 2nd getaddrinfo (if connect attempted a re-lookup) would return 127.0.0.1.
    # Because _PinnedNetworkBackend connects strictly to the pinned IP with zero 2nd lookup,
    # getaddrinfo is called ONLY ONCE and connect uses the pinned 93.184.216.34 sockaddr.
    lookup_calls: list[tuple[Any, ...]] = []
    connected_addrs: list[Any] = []

    def _fake_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        lookup_calls.append((host, port))
        if len(lookup_calls) == 1:
            return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", int(port)))]
        return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("127.0.0.1", int(port)))]

    class _FakeSocket:
        def __init__(self, family: int, stype: int, proto: int) -> None:
            self.family = family

        def settimeout(self, t: float | None) -> None:
            return

        def connect(self, sockaddr: Any) -> None:
            connected_addrs.append(sockaddr)
            raise OSError("sentinel-connect-stop")

        def setsockopt(self, *args: Any) -> None:
            return

        def close(self) -> None:
            return

    monkeypatch.setattr(web_tools.socket, "getaddrinfo", _fake_getaddrinfo)
    monkeypatch.setattr(web_tools.socket, "socket", _FakeSocket)
    # Disable env proxy so direct _PinnedNetworkBackend path is exercised deterministically
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)

    with pytest.raises(Exception, match="sentinel-connect-stop"):
        web_fetch_url("http://rebind-test.example.com/report.html")

    assert len(lookup_calls) == 1, f"Expected single DNS lookup, got {lookup_calls}"
    assert connected_addrs == [("93.184.216.34", 80)], f"Connect did not use pinned IP: {connected_addrs}"


# ---------------------------------------------------------------------------
# 5c. Iteration 3 Regression Tests (Equation Unit Propagation, Multi-Operand Sums,
#     Table % / Colon Formatting, Tool-Error Source Skipping, URL Spoof, Unit Suffixes)
# ---------------------------------------------------------------------------


def test_verifier_equation_unit_propagation_multi_operand_and_table_formatting() -> None:
    tool_calls = [
        {
            "name": "query_stored_market_data",
            "args": {"symbol": "000858"},
            "result_summary": (
                "close=133.67, pe_ttm=15.8x, roe=0.2248, revenue=123000000, "
                "q1=9710647471.03, q2=8954257202.51, q3=6474551792.18, "
                "t1=245.03, t2=227.20, t3=194.85, d4=10.50, d5=20.25"
            ),
            "duration_ms": 10,
        }
    ]
    sources = ["static_baseline"]

    # 1. Equation-clause 亿/万 propagation in all supported formats
    eq_variants = [
        "TTM净利润计算：97.11 + 89.54 - 64.75 = 121.90 亿元。来源：static_baseline",
        "TTM净利润计算：(97.11 + 89.54 - 64.75) = 121.90（亿元）。来源：static_baseline",
        "TTM净利润（亿元）：97.11 + 89.54 - 64.75 = 121.90。来源：static_baseline",
        "TTM净利润计算：121.90 亿元 = 97.11 + 89.54 - 64.75。来源：static_baseline",
    ]
    for ans in eq_variants:
        res = verify_answer_grounding(ans, tool_calls, sources)
        assert res["grounded"] is True, f"Expected grounded=True for {ans!r}, got {res}"
        assert res["ungrounded_numbers"] == []

    # 2. Comma/sentence/non-scaled-yuan leaks must stay strictly rejected
    leak_variants = [
        ("某指标100万元，虚构158500元。来源：static_baseline", "158500"),
        ("某指标100万元。虚构158500元。来源：static_baseline", "158500"),
        ("某指标100万元 + 虚构158500元。来源：static_baseline", "158500"),
        ("营收1.23，收盘价133.67亿元。来源：static_baseline", "1.23"),
    ]
    for ans, bad_tok in leak_variants:
        res = verify_answer_grounding(ans, tool_calls, sources)
        assert res["grounded"] is False, f"Expected grounded=False for {ans!r}"
        assert bad_tok in res["ungrounded_numbers"], f"Expected {bad_tok!r} in {res}"

    # 3. 3-day sum (a + b + c), a - b - c, and 5-operand explicit '=' sum; reject if any operand fabricated
    r_3sum = verify_answer_grounding(
        "最近3天成交额：245.03 + 227.20 + 194.85 = 667.08 亿元。来源：static_baseline",
        tool_calls,
        sources,
    )
    assert r_3sum["grounded"] is True, r_3sum

    r_3sub = verify_answer_grounding(
        "差额：245.03 - 10.50 - 20.25 = 214.28 亿元。来源：static_baseline",
        tool_calls,
        sources,
    )
    assert r_3sub["grounded"] is True, r_3sub

    r_5sum = verify_answer_grounding(
        "五项合计：245.03 + 227.20 + 194.85 + 10.50 + 20.25 = 697.83 亿元。来源：static_baseline",
        tool_calls,
        sources,
    )
    assert r_5sum["grounded"] is True, r_5sum

    r_5bad = verify_answer_grounding(
        "五项合计：245.03 + 227.20 + 194.85 + 10.50 + 999.99 = 1677.57 亿元。来源：static_baseline",
        tool_calls,
        sources,
    )
    assert r_5bad["grounded"] is False
    assert {"999.99", "1677.57"} <= set(r_5bad["ungrounded_numbers"])

    # 4. Multi-punctuation prefix '（亿元）：1.23', Markdown table '| ROE(%) |', and '22.4 %' spacing
    r_fmt = verify_answer_grounding(
        (
            "五粮液(000858)营收（亿元）：1.23，收盘价133.67元，ROE 22.5 %。\n"
            "| 股票 | ROE(%) |\n"
            "| --- | --- |\n"
            "| 000858 | 22.48 |\n"
            "来源：static_baseline"
        ),
        tool_calls,
        sources,
    )
    assert r_fmt["grounded"] is True, r_fmt
    assert r_fmt["ungrounded_numbers"] == []


def test_verifier_unit_suffixed_numbers_grounding_and_rejection() -> None:
    tool_calls = [
        {
            "name": "query_stored_market_data",
            "args": {"symbol": "000858"},
            "result_summary": "close=133.67, pe_ttm=15.8, roe=0.224, revenue=123000000, cap=1230000000, target=777000",
            "duration_ms": 10,
        }
    ]
    sources = ["static_baseline"]

    # Fabricated unit-suffixed numbers must NEVER bypass verifier
    bad_units = verify_answer_grounding(
        "PE 99.9x，95x，市值999.99B，营收888.88M，目标价777K_bad无效，1234K，规模5.55billion。来源：static_baseline",
        tool_calls,
        sources,
    )
    assert bad_units["grounded"] is False
    assert {"99.9x", "95x", "999.99B", "888.88M", "1234K", "5.55billion"} <= set(bad_units["ungrounded_numbers"])

    # 1.23e8 (123M) must NOT satisfy 1.23billion when 1.23e9 is absent
    tc_only_m = [
        {
            "name": "query_stored_market_data",
            "args": {"symbol": "000858"},
            "result_summary": "close=133.67, pe_ttm=15.8, revenue=123000000",
            "duration_ms": 5,
        }
    ]
    bad_b_vs_m = verify_answer_grounding(
        "PE 99.9x，市值999.99B，营收888.88M，目标价777K，规模1.23billion，另记1.23 billion。来源：static_baseline",
        tc_only_m,
        sources,
    )
    assert bad_b_vs_m["grounded"] is False
    assert {"99.9x", "999.99B", "888.88M", "777K", "1.23billion", "1.23"} <= set(bad_b_vs_m["ungrounded_numbers"])

    # Valid grounded unit-suffixed equivalents must pass
    good_units = verify_answer_grounding(
        "PE 15.8x，营收123M，市值1.23B（1.23billion），目标价777K。来源：static_baseline",
        tool_calls,
        sources,
    )
    assert good_units["grounded"] is True, good_units
    assert good_units["ungrounded_numbers"] == []


def test_pi_runner_skips_tool_error_sources_and_blocks_url_prefix_spoofing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_env(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-mock-key")

    def _run_mocked_events(jsonl_events: list[dict[str, Any]], prompt: str) -> dict[str, Any]:
        raw_stdout = ("\n".join(json.dumps(e, ensure_ascii=False) for e in jsonl_events) + "\n").encode("utf-8")

        class _Pipe:
            def __init__(self, data: bytes) -> None:
                self._d = data
                self._i = 0

            def read1(self, n: int = 8192) -> bytes:
                c = self._d[self._i:self._i + n]
                self._i += len(c)
                return c

            def read(self, n: int = 4096) -> bytes:
                return self.read1(n)

        class _Popen:
            def __init__(self, cmd: list[str], **kwargs: Any) -> None:
                self.stdout = _Pipe(raw_stdout)
                self.stderr = _Pipe(b"")

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def kill(self) -> None:
                return

        monkeypatch.setattr("stock_research.agent.pi_runner.subprocess.Popen", _Popen)
        return run_research_query(prompt)

    # 1. Tool error messages mentioning yfinance/sec_edgar/URLs must NOT pollute sources
    err_events = [
        {
            "type": "tool_execution_start",
            "toolCallId": "tc_err",
            "toolName": "mcp__stock_research__fetch_market_data",
            "args": {"source": "akshare", "sector": "电力"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "tc_err",
            "toolName": "mcp__stock_research__fetch_market_data",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "Error executing tool fetch_market_data: Unsupported source 'akshare'; expected auto, akshare_sector, akshare_stock, yfinance, sec_edgar, finnhub_news, or finnhub_earnings",
                    }
                ],
                "isError": True,
            },
            "isError": True,
            "durationMs": 4,
        },
        {
            "type": "tool_execution_start",
            "toolCallId": "tc_ok",
            "toolName": "mcp__stock_research__fetch_market_data",
            "args": {"source": "akshare_sector", "sector": "电力", "days": 3},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "tc_ok",
            "toolName": "mcp__stock_research__fetch_market_data",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"source": "akshare_ths", "change_pct": 1.85}, ensure_ascii=False),
                    }
                ]
            },
            "isError": False,
            "durationMs": 12,
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "电力板块上涨1.85%。来源：akshare_ths"}],
            },
        },
    ]
    res_err = _run_mocked_events(err_events, "测试错误工具来源过滤")
    assert "akshare_ths" in res_err["sources"]
    assert "yfinance" not in res_err["sources"]
    assert "sec_edgar" not in res_err["sources"]
    assert res_err["verification"]["grounded"] is True

    # 2. End-to-end URL prefix spoofing must be blocked (Challenge A)
    spoof_events = [
        {
            "type": "tool_execution_start",
            "toolCallId": "tc_ws",
            "toolName": "mcp__stock_research__web_search",
            "args": {"query": "000858"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "tc_ws",
            "toolName": "mcp__stock_research__web_search",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"results": [{"url": "https://example.com/real-other", "title": "real"}]},
                            ensure_ascii=False,
                        ),
                    }
                ]
            },
            "isError": False,
            "durationMs": 10,
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "五粮液详情见 https://example.com/real"}],
            },
        },
    ]
    res_spoof = _run_mocked_events(spoof_events, "测试URL前缀伪造")
    assert res_spoof["verification"]["grounded"] is False
    assert "https://example.com/real" in res_spoof["verification"]["unverified_urls"]
    assert "https://example.com/real" not in res_spoof["sources"]

    # 3. Exact http:// -> https:// scheme upgrade on identical full URL is accepted,
    #    while truncated homepage or prefix spoof stays rejected
    tc_http = [
        {
            "name": "query_stored_market_data",
            "args": {"symbol": "000858"},
            "result_summary": "close=71.20 url=http://finance.eastmoney.com/a/202611243938674275.html",
            "duration_ms": 5,
        }
    ]
    r_https_ok = verify_answer_grounding(
        "五粮液收盘71.20元，来源：https://finance.eastmoney.com/a/202611243938674275.html",
        tc_http,
        ["http://finance.eastmoney.com/a/202611243938674275.html"],
    )
    assert r_https_ok["grounded"] is True, r_https_ok
    assert r_https_ok["unverified_urls"] == []

    r_bare_bad = verify_answer_grounding(
        "五粮液收盘71.20元，来源：https://finance.eastmoney.com",
        tc_http,
        ["http://finance.eastmoney.com/a/202611243938674275.html"],
    )
    assert r_bare_bad["grounded"] is False
    assert "https://finance.eastmoney.com" in r_bare_bad["unverified_urls"]


# ---------------------------------------------------------------------------
# 5d. Iteration 4 Regression Tests (Error Evidence Isolation, <0.1s 200-Operand
#     Equations, Extended/Plural Unit Suffixes, Prose Ints, Comma Clauses, URL Spans)
# ---------------------------------------------------------------------------


def test_pi_runner_and_verifier_isolate_is_error_tool_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_env(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-mock-key")

    jsonl_events = [
        {
            "type": "tool_execution_start",
            "toolCallId": "tc_err",
            "toolName": "mcp__stock_research__web_fetch_url",
            "args": {"url": "https://error-leak.example.com/500"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "tc_err",
            "toolName": "mcp__stock_research__web_fetch_url",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": "Error executing tool web_fetch_url: timeout fetching https://error-leak.example.com/500 (code 599.99)",
                    }
                ],
                "isError": True,
            },
            "isError": True,
            "durationMs": 5,
        },
        {
            "type": "tool_execution_start",
            "toolCallId": "tc_ok",
            "toolName": "mcp__stock_research__query_stored_market_data",
            "args": {"symbol": "000858"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "tc_ok",
            "toolName": "mcp__stock_research__query_stored_market_data",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"source": "static_baseline", "close": 133.67}, ensure_ascii=False),
                    }
                ]
            },
            "isError": False,
            "durationMs": 8,
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [
                    {
                        "type": "text",
                        "text": "目标价599.99元，详见 https://error-leak.example.com/500 ，来源 static_baseline",
                    }
                ],
            },
        },
    ]
    raw_stdout = ("\n".join(json.dumps(e, ensure_ascii=False) for e in jsonl_events) + "\n").encode("utf-8")

    class _Pipe:
        def __init__(self, data: bytes) -> None:
            self._d = data
            self._i = 0

        def read1(self, n: int = 8192) -> bytes:
            c = self._d[self._i:self._i + n]
            self._i += len(c)
            return c

        def read(self, n: int = 4096) -> bytes:
            return self.read1(n)

    class _Popen:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            self.stdout = _Pipe(raw_stdout)
            self.stderr = _Pipe(b"")

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return

    monkeypatch.setattr("stock_research.agent.pi_runner.subprocess.Popen", _Popen)
    res = run_research_query("测试错误工具输出隔离")

    # Failed tool stays recorded in trace with is_error=True, but cannot ground numbers/URLs
    assert len(res["tool_calls"]) == 2
    assert res["tool_calls"][0]["is_error"] is True
    assert res["sources"] == ["static_baseline"]
    assert res["verification"]["grounded"] is False
    assert "599.99" in res["verification"]["ungrounded_numbers"]
    assert "https://error-leak.example.com/500" in res["verification"]["unverified_urls"]

    # Direct callers passing res["tool_calls"] to verify_answer_grounding must also skip error entries
    direct_ver = verify_answer_grounding(
        "目标价599.99元，详见 https://error-leak.example.com/500",
        res["tool_calls"],
        res["sources"],
    )
    assert direct_ver["grounded"] is False
    assert "599.99" in direct_ver["ungrounded_numbers"]
    assert "https://error-leak.example.com/500" in direct_ver["unverified_urls"]

    only_err = verify_answer_grounding(
        "目标价599.99元",
        [res["tool_calls"][0]],
        ["static_baseline"],
    )
    assert only_err["grounded"] is False
    assert only_err["checks"][1]["passed"] is False


def test_verifier_200_operand_equations_linear_time_under_100ms() -> None:
    import time as _time

    tc_grounded = [{"name": "query_stored_market_data", "args": {}, "result_summary": "a=100000000", "duration_ms": 5}]
    tc_ungrounded = [{"name": "query_stored_market_data", "args": {}, "result_summary": "close=133.67", "duration_ms": 5}]
    tc_100_yi = [
        {
            "name": "query_stored_market_data",
            "args": {},
            "result_summary": ", ".join(f"v{i}={100 + i}.{i % 10}1e8" for i in range(0, 200, 2)),
            "duration_ms": 5,
        }
    ]

    cases = [
        ("grounded_200", "长等式：" + " + ".join(["1.00"] * 200) + " = 200.00 亿元。来源：static_baseline", tc_grounded),
        ("ungrounded_200", "长等式：" + " + ".join(["1.00"] * 200) + " = 200.00 亿元。来源：static_baseline", tc_ungrounded),
        ("200_ints", "长等式：" + " + ".join([str(10 + (i % 90)) for i in range(200)]) + " = 9999.99 亿元。来源：static_baseline", tc_ungrounded),
        ("200_parens", "计算：" + "(".join(["1.00 + "] * 200) + "abc\n" + "1.00 + " * 200 + " = 999.99 亿元。来源：static_baseline", tc_ungrounded),
        (
            "200_distinct_mixed_yi",
            "长等式："
            + " + ".join(
                [f"{100 + i}.{i % 10}1" for i in range(0, 200, 2)]
                + [f"{500 + i}.{i % 10}7" for i in range(1, 200, 2)]
            )
            + " = 99999.99 亿元。来源：static_baseline",
            tc_100_yi,
        ),
        (
            "200_alternating_yi_and_ungrounded",
            "长等式："
            + " + ".join(
                [f"{100 + i}.{i % 10}1" if i % 2 == 0 else f"{500 + i}.{i % 10}7" for i in range(200)]
            )
            + " = 99999.99 亿元。来源：static_baseline",
            tc_100_yi,
        ),
    ]
    for label, ans, tc in cases:
        t0 = _time.perf_counter()
        res = verify_answer_grounding(ans, tc, ["static_baseline"])
        elapsed = _time.perf_counter() - t0
        assert elapsed < 0.10, f"{label} took {elapsed:.4f}s (must be < 0.10s)"
        assert res["grounded"] is False


def test_verifier_extended_unit_suffixes_prose_ints_comma_clauses_and_url_spans() -> None:
    tc_base = [
        {
            "name": "query_stored_market_data",
            "args": {"symbol": "000858"},
            "result_summary": "close=133.67, pb=1.23, pe_ttm=15.8, roe=0.224, rev=9710647471.03, cap=3500000000000, bps_val=150bps, note='size 1.23 billions'",
            "duration_ms": 5,
        }
    ]
    sources = ["static_baseline", "https://finance.eastmoney.com/a/202611243938674275.html"]

    # 1. Extended non-BMK & plural suffixes must be extracted & rejected if ungrounded
    for bad_tok in (
        "999.99T",
        "999.99trillion",
        "999.99trillions",
        "999.99billions",
        "888.88millions",
        "777thousands",
        "999.99USD",
        "999.99CNY",
        "999.99RMB",
        "99.9pct",
        "250bps",
        "25.5pp",
    ):
        r = verify_answer_grounding(f"指标 {bad_tok}。来源：static_baseline", tc_base, sources)
        assert r["grounded"] is False, f"Expected {bad_tok} ungrounded, got {r}"
        assert bad_tok in r["ungrounded_numbers"]

    # "市值 1.23 billion" must NOT short-circuit on pb=1.23 when 1.23e9 is absent
    tc_pb_only = [{"name": "query_stored_market_data", "args": {}, "result_summary": "pb=1.23, close=133.67", "duration_ms": 5}]
    r_pb = verify_answer_grounding("AAPL PB为1.23，市值 1.23 billion。来源：static_baseline", tc_pb_only, sources)
    assert r_pb["grounded"] is False
    assert "1.23" in r_pb["ungrounded_numbers"]

    # Valid grounded extended units (including spaced "billions" in tool output, 3.5T, 22.4pct, 150bps, 133.67USD)
    r_good_ext = verify_answer_grounding(
        "AAPL总市值3.5T（3.5 trillions），规模1.23 billion，股价133.67USD，ROE 22.4pct，利差150bps。来源：static_baseline",
        tc_base,
        sources,
    )
    assert r_good_ext["grounded"] is True, r_good_ext
    assert r_good_ext["ungrounded_numbers"] == []

    # 2. Harmless prose integers must NOT synthesize fabricated line arithmetic targets
    r_prose = verify_answer_grounding("第10名与第20名对比，目标价200元。来源：static_baseline", tc_base, sources)
    assert r_prose["grounded"] is False
    assert "200" in r_prose["ungrounded_numbers"]

    # 3. Comma delimiters & has_explicit_yuan guard prevent comma-crossed 亿/万 leakage
    r_comma = verify_answer_grounding("股价97.11元，同比增十亿元。来源：static_baseline", tc_base, sources)
    assert r_comma["grounded"] is False
    assert "97.11" in r_comma["ungrounded_numbers"]

    r_comma_ascii = verify_answer_grounding("股价97.11元,同比增十亿元。来源：static_baseline", tc_base, sources)
    assert r_comma_ascii["grounded"] is False
    assert "97.11" in r_comma_ascii["ungrounded_numbers"]

    # 4. Exact URL span containment catches numbers immediately following closing ')' or ']'
    r_url_md = verify_answer_grounding(
        "[公告](https://finance.eastmoney.com/a/202611243938674275.html)虚构99999元。来源：static_baseline",
        tc_base,
        sources,
    )
    assert r_url_md["grounded"] is False
    assert "99999" in r_url_md["ungrounded_numbers"]

    r_url_paren = verify_answer_grounding(
        "来源(https://finance.eastmoney.com/a/202611243938674275.html)虚构99999元。来源：static_baseline",
        tc_base,
        sources,
    )
    assert r_url_paren["grounded"] is False
    assert "99999" in r_url_paren["ungrounded_numbers"]


# ---------------------------------------------------------------------------
# 5e. Iteration 5 Regression Tests (Monetary/Equation Integer Hardening &
#     CJK-Adjacent Spaced Scale + Extended Minor Suffixes)
# ---------------------------------------------------------------------------


def test_verifier_rejects_monetary_equation_harmless_ints_and_cjk_spaced_scales() -> None:
    tc_close = [
        {
            "name": "query_stored_market_data",
            "args": {"symbol": "000858"},
            "result_summary": "close=133.67, pb=1.23, roe=0.224",
            "duration_ms": 5,
        }
    ]
    sources = ["static_baseline"]

    # 1. Monetary, scale, equation, and table-header contexts must NOT bypass via _HARMLESS_INTEGERS / 1990..2035
    bad_monetary_int_cases = [
        ("10 + 20 = 30 亿元。来源：static_baseline", {"10", "20", "30"}),
        ("目标价30元。来源：static_baseline", {"30"}),
        ("市值100亿元。来源：static_baseline", {"100"}),
        ("营收250万元。来源：static_baseline", {"250"}),
        ("目标价2000元。来源：static_baseline", {"2000"}),
        ("目标价 2025 亿元。来源：static_baseline", {"2025"}),
        ("| 股票 | PE |\n| --- | --- |\n| 000858 | 30 |\n来源：static_baseline", {"30"}),
    ]
    for ans, expected_bad in bad_monetary_int_cases:
        res = verify_answer_grounding(ans, tc_close, sources)
        assert res["grounded"] is False, f"Expected grounded=False for {ans!r}, got {res}"
        assert expected_bad <= set(res["ungrounded_numbers"]), f"Expected {expected_bad} in {res}"

    # Genuine prose markers (近3天, 第10名, 100股, 2025年, 2025-11-26, T+1) remain valid
    tc_with_date = [
        {
            "name": "query_stored_market_data",
            "args": {"symbol": "000858"},
            "result_summary": "date=2025-11-26, close=133.67, pb=1.23, roe=0.224",
            "duration_ms": 5,
        }
    ]
    good_prose = verify_answer_grounding(
        "近3天（2025-11-26）第10名五粮液收盘133.67元，2025年按T+1规则买100股，PB=1.23，ROE 22.4%，95% VaR。来源：static_baseline",
        tc_with_date,
        sources,
    )
    assert good_prose["grounded"] is True, good_prose
    assert good_prose["ungrounded_numbers"] == []

    # 2. CJK-adjacent spaced scales must fail against pb=1.23
    for cjk_scale_ans in (
        "市值 1.23 billion美元。来源：static_baseline",
        "市值 1.23 billion元。来源：static_baseline",
    ):
        r_cjk = verify_answer_grounding(cjk_scale_ans, tc_close, sources)
        assert r_cjk["grounded"] is False, f"Expected grounded=False for {cjk_scale_ans!r}"
        assert "1.23" in r_cjk["ungrounded_numbers"]

    # 3. Extended minor unit suffixes must be caught when ungrounded, while 22.4percent grounds via roe=0.224
    for bad_suffix in ("999.99percent", "999.99yuan", "999.99CAD", "999.99bp", "999.99pts"):
        r_suf = verify_answer_grounding(f"指标 {bad_suffix}。来源：static_baseline", tc_close, sources)
        assert r_suf["grounded"] is False, f"Expected {bad_suffix} ungrounded, got {r_suf}"
        assert bad_suffix in r_suf["ungrounded_numbers"]

    r_good_pct = verify_answer_grounding("ROE 22.4percent。来源：static_baseline", tc_close, sources)
    assert r_good_pct["grounded"] is True, r_good_pct
    assert r_good_pct["ungrounded_numbers"] == []


# ---------------------------------------------------------------------------
# 5f. Iteration 6 Regression Tests (Scaled Direct Non-Pollution, Spaced ASCII &
#     Chinese Table Headers, Calendar Period Shorthand, 3-Attempt Repair Loop)
# ---------------------------------------------------------------------------


def test_verifier_blocks_scaled_direct_pollution_spaced_ascii_and_chinese_headers() -> None:
    tc = [
        {
            "name": "query_stored_market_data",
            "args": {"symbol": "000858"},
            "result_summary": (
                "close=133.67, revenue=123000000, roe=0.224, "
                "q1=9710647471.03, q2=8954257202.51, q3=6474551792.18"
            ),
            "duration_ms": 5,
        }
    ]
    sources = ["static_baseline"]

    # Challenge A: scaled "direct" values must NOT pollute global working_floats / float_index
    pollution_cases = [
        ("| 股票 | 营收(亿元) |\n| --- | --- |\n| 000858 | 1.23 |\n五粮液目标价 1.23 元。来源：static_baseline", "1.23"),
        ("| 股票 | 营收(亿元) |\n| --- | --- |\n| 000858 | 1.23 |\n五粮液目标价 1.23。来源：static_baseline", "1.23"),
        ("| 股票 | ROE(%) |\n| --- | --- |\n| 000858 | 22.4 |\n五粮液目标价 22.4 元。来源：static_baseline", "22.4"),
        ("ROE为22.4%，五粮液目标价 22.4。来源：static_baseline", "22.4"),
    ]
    for ans, leaked in pollution_cases:
        res = verify_answer_grounding(ans, tc, sources)
        assert res["grounded"] is False, f"Expected grounded=False for {ans!r}, got {res}"
        assert leaked in res["ungrounded_numbers"], f"Expected {leaked!r} in {res}"

    # Derived 12190352881.36 -> 121.90 亿元 and Markdown-bolded equations must still pass
    for derived_ans in (
        "净利润合计：9710647471.03 + 8954257202.51 - 6474551792.18 = 12190352881.36，折合 121.90 亿元。来源：static_baseline",
        "- TTM 归母净利 = 97.11 + 89.54 - 64.75 = **121.90 亿元**\n来源：static_baseline",
        "- TTM 归母净利 = **97.11** + 89.54 - 64.75 = **121.90** 亿元\n来源：static_baseline",
    ):
        derived_ok = verify_answer_grounding(derived_ans, tc, sources)
        assert derived_ok["grounded"] is True, derived_ok
        assert derived_ok["ungrounded_numbers"] == []

    # Challenge B: spaced ASCII currencies/units must NOT bypass _HARMLESS_INTEGERS / 1990..2035
    spaced_ascii_cases = [
        ("目标价 30 USD。来源：static_baseline", "30"),
        ("目标价 30 yuan。来源：static_baseline", "30"),
        ("市值 100 RMB。来源：static_baseline", "100"),
        ("营收 250 CNY。来源：static_baseline", "250"),
        ("目标价 2000 HKD。来源：static_baseline", "2000"),
        ("ROE 30 percent。来源：static_baseline", "30"),
        ("涨幅 30 pct。来源：static_baseline", "30"),
        ("利差 30 bps。来源：static_baseline", "30"),
    ]
    for ans, bad in spaced_ascii_cases:
        res = verify_answer_grounding(ans, tc, sources)
        assert res["grounded"] is False, f"Expected grounded=False for {ans!r}, got {res}"
        assert bad in res["ungrounded_numbers"], f"Expected {bad!r} in {res}"

    # Challenge B: unitless Chinese financial table headers must NOT bypass _HARMLESS_INTEGERS
    chinese_header_cases = [
        ("| 股票 | 目标价 |\n| --- | --- |\n| 000858 | 100 |\n来源：static_baseline", "100"),
        ("| 股票 | 市盈率 |\n| --- | --- |\n| 000858 | 30 |\n来源：static_baseline", "30"),
        ("| 股票 | 市值 |\n| --- | --- |\n| 000858 | 100 |\n来源：static_baseline", "100"),
    ]
    for ans, bad in chinese_header_cases:
        res = verify_answer_grounding(ans, tc, sources)
        assert res["grounded"] is False, f"Expected grounded=False for {ans!r}, got {res}"
        assert bad in res["ungrounded_numbers"], f"Expected {bad!r} in {res}"


def test_verifier_calendar_period_shorthand_and_3attempt_repair_convergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc = [
        {
            "name": "query_stored_market_data",
            "args": {"symbol": "000858"},
            "result_summary": "close=133.67, q3=6474551792.18",
            "duration_ms": 5,
        }
    ]
    sources = ["static_baseline"]

    # Calendar period shorthand allowed ONLY in explicit period contexts
    good_periods = [
        "五粮液 2025 9M 净利润 64.75亿元，收盘133.67元。来源：static_baseline",
        "五粮液 2026年9M 净利润 64.75亿元，收盘133.67元。来源：static_baseline",
        "五粮液 9M25 净利润 64.75亿元，收盘133.67元。来源：static_baseline",
        "五粮液 前9M 净利润 64.75亿元，收盘133.67元。来源：static_baseline",
        "五粮液 9M同比 净利润 64.75亿元，收盘133.67元。来源：static_baseline",
        "五粮液 2026年3M、2026年6M、2026年12M 收盘133.67元。来源：static_baseline",
    ]
    for ans in good_periods:
        res = verify_answer_grounding(ans, tc, sources)
        assert res["grounded"] is True, f"Expected grounded=True for {ans!r}, got {res}"
        assert res["ungrounded_numbers"] == []

    # Financial/decimal/currency contexts must remain strictly rejected
    bad_periods = [
        ("五粮液营收9M，收盘133.67元。来源：static_baseline", "9M"),
        ("五粮液市值 9M，收盘133.67元。来源：static_baseline", "9M"),
        ("五粮液2025年营收9M，收盘133.67元。来源：static_baseline", "9M"),
        ("五粮液规模$9M，收盘133.67元。来源：static_baseline", "9M"),
        ("五粮液营收999.99M，收盘133.67元。来源：static_baseline", "999.99M"),
        ("五粮液营收888.88M，收盘133.67元。来源：static_baseline", "888.88M"),
        ("五粮液 2026年9.0M 收盘133.67元。来源：static_baseline", "9.0M"),
        ("| 股票 | 市值 |\n| --- | --- |\n| 000858 | 9M |\n来源：static_baseline", "9M"),
    ]
    for ans, bad in bad_periods:
        res = verify_answer_grounding(ans, tc, sources)
        assert res["grounded"] is False, f"Expected grounded=False for {ans!r}, got {res}"
        assert bad in res["ungrounded_numbers"], f"Expected {bad!r} in {res}"

    # Verify pi_runner runs up to 3 repair attempts and rejects only when new_nums + new_urls >= old_nums + old_urls
    _isolated_env(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-mock-repair-loop")
    jsonl_events = [
        {
            "type": "tool_execution_start",
            "toolCallId": "tc1",
            "toolName": "mcp__stock_research__query_stored_market_data",
            "args": {"symbol": "000858"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "tc1",
            "toolName": "mcp__stock_research__query_stored_market_data",
            "result": {"content": [{"type": "text", "text": json.dumps({"source": "static_baseline", "close": 133.67})}]},
            "isError": False,
            "durationMs": 5,
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "content": [{"type": "text", "text": "五粮液目标价 555.55、666.66、777.77，收盘133.67元。来源：static_baseline"}],
            },
        },
    ]
    raw_stdout = ("\n".join(json.dumps(e, ensure_ascii=False) for e in jsonl_events) + "\n").encode("utf-8")

    class _Pipe:
        def __init__(self, data: bytes) -> None:
            self._d = data
            self._i = 0

        def read1(self, n: int = 8192) -> bytes:
            c = self._d[self._i:self._i + n]
            self._i += len(c)
            return c

        def read(self, n: int = 4096) -> bytes:
            return self.read1(n)

    class _Popen:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            self.stdout = _Pipe(raw_stdout)
            self.stderr = _Pipe(b"")

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return

    repair_outputs = iter([
        "五粮液目标价 555.55、666.66、777.77，收盘133.67元。来源：static_baseline",
        "五粮液目标价 777.77，收盘133.67元。来源：static_baseline",
        "五粮液收盘133.67元。来源：static_baseline",
    ])
    repair_flags: list[bool] = []

    def _fake_repair(**kwargs: Any) -> str | None:
        repair_flags.append(bool(kwargs.get("strict_delete")))
        return next(repair_outputs, None)

    monkeypatch.setattr("stock_research.agent.pi_runner.subprocess.Popen", _Popen)
    monkeypatch.setattr("stock_research.agent.pi_runner._repair_ungrounded_answer", _fake_repair)
    res_rep = run_research_query("测试3轮修复收敛")
    assert repair_flags == [False, True, True]
    assert res_rep["verification"]["grounded"] is True
    assert res_rep["answer"] == "五粮液收盘133.67元。来源：static_baseline"


# ---------------------------------------------------------------------------
# 6. Live Integration & End-to-End Pi Agent Queries (RUN_LIVE_TESTS=1)
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason="Set RUN_LIVE_TESTS=1 to run live network & DeepSeek Pi tests")
def test_live_web_search_and_fetch() -> None:
    hits = web_search("JEVDB DuckDB", max_results=3)
    assert len(hits) >= 1
    assert hits[0]["url"].startswith(("http://", "https://"))
    md = web_fetch_url("https://jevdb.org/")
    assert len(md) > 40


@pytest.mark.live
@pytest.mark.skipif(not LIVE_ENABLED, reason="Set RUN_LIVE_TESTS=1 to run live DeepSeek Pi CLI tests")
@pytest.mark.parametrize(
    "prompt,expected_keywords",
    [
        ("我想知道最近3天a股电力行业的情况", ("电力",)),
        ("请从长期价值投资角度分析 000858（五粮液）的估值、ROE与安全边际", ("000858", "五粮液")),
        ("请提出或查看一个A股日频量化交易策略，运行回测并检查当前模拟交易账户状态", ("策略", "回测")),
    ],
)
def test_live_cli_ask_end_to_end_scenarios(
    prompt: str,
    expected_keywords: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.fail("DEEPSEEK_API_KEY must be set when RUN_LIVE_TESTS=1")
    env = _isolated_env(tmp_path, monkeypatch)

    # Seed baseline bars/news so quant backtests & static+dynamic JEVDB queries have data immediately
    with db_session(Path(env["STOCK_RESEARCH_DB_PATH"]), init=True) as con:
        load_static_baseline(con)

    proc = subprocess.run(
        [sys.executable, "-m", "stock_research.cli", "ask", prompt, "--json"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, f"CLI failed: {proc.stderr}\nSTDOUT: {proc.stdout}"
    data = json.loads(proc.stdout)
    assert data["prompt"] == prompt
    assert len(data["tool_calls"]) >= 2, f"Expected multi-round tool calls, got {data['tool_calls']}"
    assert data["duration_ms"] > 0
    assert len(data["sources"]) >= 1
    assert re.search(r"\d", data["answer"]) is not None
    for kw in expected_keywords:
        assert kw in data["answer"] or kw in json.dumps(data["tool_calls"], ensure_ascii=False)
    assert data["verification"]["sources"], "Expected verified cited sources"
    assert data["verification"]["grounded"] is True, f"Ungrounded claims: {data['verification']}"
