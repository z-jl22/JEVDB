"""Self-validation tests for the E2E harness, markers, isolation, runner CLI, and Playwright bridge."""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from stock_research.tests.e2e.conftest import (
    CORE_TRACKED_PATHS,
    DEFAULT_SCRATCH_BASE,
    MARKERS,
    REPO_ROOT,
    resolve_e2e_mode,
)
from stock_research.tests.e2e.contract_harness import ContractLLMShimServer, init_db, load_static_baseline

pytestmark = pytest.mark.e2e_self

E2E_DIR = Path(__file__).resolve().parent


def test_markers_registered_strictly():
    ini_path = E2E_DIR / "pytest.ini"
    assert ini_path.exists()
    cp = configparser.ConfigParser()
    cp.read(ini_path, encoding="utf-8")
    raw_markers = cp["pytest"]["markers"]
    expected = {"tier1", "tier2", "tier3", "tier4", "e2e_self", "live_network", "playwright"}
    parsed = {line.split(":")[0].strip() for line in raw_markers.strip().splitlines() if line.strip()}
    assert expected <= parsed
    conftest_markers = {m.split(":")[0].strip() for m in MARKERS}
    assert expected <= conftest_markers


def test_scratch_isolation_and_tmp_env(isolated_env: dict[str, Path]):
    scratch_prefix = os.environ.get("JEVDB_E2E_SCRATCH_ROOT", str(DEFAULT_SCRATCH_BASE))
    for var in (
        "TMPDIR",
        "TEMP",
        "TMP",
        "STOCK_RESEARCH_RUNTIME_DIR",
        "STOCK_RESEARCH_CACHE_DIR",
        "STOCK_RESEARCH_DB_PATH",
        "STOCK_RESEARCH_LEDGER_DB_PATH",
        "STOCK_RESEARCH_STRATEGY_DIR",
        "JEVDB_RESEARCH_DB",
        "JEVDB_MARKET_DB",
        "JEVDB_LEDGER_DB",
        "JEVDB_PAPER_LEDGER_DB",
        "JEVDB_STRATEGY_DIR",
    ):
        val = os.environ.get(var, "")
        assert val.startswith(scratch_prefix), f"{var}={val!r} escaped scratch base {scratch_prefix}"
        assert not val.startswith("/tmp/"), f"{var} must never point to /tmp"
        assert ".tmp/stock_research" not in val, f"{var} must never touch repo .tmp/stock_research"
    assert isolated_env["db_path"].parent == isolated_env["case_dir"]
    assert len(CORE_TRACKED_PATHS) >= 17


def test_mode_resolution_contract_vs_live(monkeypatch: pytest.MonkeyPatch):
    assert resolve_e2e_mode("contract") == "contract"
    assert resolve_e2e_mode("live") == "live"
    with pytest.raises(ValueError):
        resolve_e2e_mode("invalid_mode")
    # Currently implementation sentinels do not all exist, so auto -> contract
    all_present = all(
        (REPO_ROOT / p).exists()
        for p in (
            "stock_research/api/app.py",
            "stock_research/cli.py",
            "stock_research/mcp_server/server.py",
            "docs_project/RUNBOOK.md",
        )
    )
    expected_auto = "live" if all_present else "contract"
    assert resolve_e2e_mode("auto") == expected_auto


def test_contract_harness_llm_shim_and_jevdb(isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch):
    shim = ContractLLMShimServer()
    port = shim.start()
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "test-contract-key")
        con = init_db(isolated_env["db_path"])
        loaded = load_static_baseline(con)
        assert loaded["bars"] == 26
        assert loaded["news"] == 2
        con.execute("CREATE SECRET llm (TYPE openai, PROVIDER env)")
        con.execute("SET jevdb_backend='llm'")
        con.execute("SET jevdb_endpoint=?", [f"http://127.0.0.1:{port}/v1"])
        con.execute("SET jevdb_model='deepseek-flash'")
        rows = con.execute(
            "SELECT event_id, jev_choose('Classify impact', {'title': title, 'body': summary}, ['positive', 'negative', 'neutral']) FROM news_events ORDER BY event_id"
        ).fetchall()
        con.close()
        assert len(rows) == 2
        assert all(r[1] in ("positive", "negative", "neutral") for r in rows)
    finally:
        shim.stop()


def test_playwright_bridge_smoke(playwright_runner):
    html = (
        "<!doctype html><html><head><title>Playwright Smoke</title></head>"
        "<body><h1 data-testid='smoke-title'>JEVDB Playwright OK</h1>"
        "<div data-testid='smoke-chart'><svg width='200' height='80'><rect width='200' height='80'/></svg></div>"
        "</body></html>"
    ).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/"
        res = playwright_runner.run_steps([
            {"action": "goto", "url": url},
            {"action": "wait_for_selector", "selector": "[data-testid='smoke-title']"},
            {"action": "inspect_selectors", "selectors": ["[data-testid='smoke-title']", "[data-testid='smoke-chart']"], "key": "dom"},
            {"action": "title", "key": "title"},
        ])
        assert res["console_errors"] == []
        assert res["page_errors"] == []
        assert res["server_errors"] == []
        assert res["results"]["title"] == "Playwright Smoke"
        assert res["results"]["dom"]["[data-testid='smoke-title']"]["text"] == "JEVDB Playwright OK"
        assert res["results"]["dom"]["[data-testid='smoke-chart']"]["chart"]["width"] > 0
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def test_runner_cli_help_and_collection(isolated_env: dict[str, Path]):
    proc = subprocess.run(
        [sys.executable, "-m", "stock_research.tests.e2e.run_e2e", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "--tier" in proc.stdout
    assert "--mode" in proc.stdout
    assert "--json-report" in proc.stdout

    col = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(E2E_DIR),
            "-c",
            str(E2E_DIR / "pytest.ini"),
            f"--basetemp={isolated_env['tmp_dir'] / 'col_basetemp'}",
            "--collect-only",
            "-q",
        ],
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert col.returncode == 0
    assert "test_tier1_features.py" in col.stdout
    assert "test_tier4_scenarios.py" in col.stdout
