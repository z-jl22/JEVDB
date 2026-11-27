"""Pytest configuration, isolation fixtures, dual-mode resolution, and Playwright bridge."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SCRATCH_BASE = Path("/home/tao/.gemini/antigravity/scratch/teamwork_tmp/e2e")
CHROMIUM_PATH = "/usr/bin/chromium"
CHROMIUM_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
ARPO_PYTHON = "/home/tao/miniconda3/envs/arpo/bin/python"

CORE_TRACKED_PATHS = [
    "src",
    "python",
    "stock_research/data/baseline",
    "tools",
    "docs",
    "scripts",
    ".github",
    "CMakeLists.txt",
    "extension_config.cmake",
    "pyproject.toml",
    "README.md",
    "LICENSE",
    ".gitignore",
    ".clang-format",
]

LIVE_SENTINEL_FILES = [
    REPO_ROOT / "stock_research" / "api" / "app.py",
    REPO_ROOT / "stock_research" / "cli.py",
    REPO_ROOT / "stock_research" / "mcp_server" / "server.py",
    REPO_ROOT / "docs_project" / "RUNBOOK.md",
]

MARKERS = [
    "tier1: Core feature end-to-end tests",
    "tier2: Negative, boundary, and error-handling E2E tests",
    "tier3: Cross-feature integration E2E tests",
    "tier4: Full user workflow and UI E2E scenarios",
    "e2e_self: E2E harness self-validation and contract mock tests",
    "live_network: Tests that call external financial/LLM APIs",
    "playwright: Headless Chromium browser UI tests",
]


def resolve_e2e_mode(requested_mode: str = "auto") -> str:
    mode = (requested_mode or os.environ.get("JEVDB_E2E_MODE") or "auto").strip().lower()
    if mode not in ("auto", "live", "contract"):
        raise ValueError(f"Unsupported E2E mode: {mode!r}; expected auto|live|contract")
    if mode == "contract":
        return "contract"
    if mode == "live":
        return "live"
    return "live" if all(p.exists() for p in LIVE_SENTINEL_FILES) else "contract"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e-mode",
        action="store",
        default=os.environ.get("JEVDB_E2E_MODE", "auto"),
        choices=["auto", "live", "contract"],
        help="E2E execution mode: auto (default), live (strict real targets), or contract (pre-impl self-test)",
    )
    parser.addoption(
        "--live-network",
        action="store_true",
        default=os.environ.get("JEVDB_E2E_LIVE_NETWORK", "0") == "1",
        help="Allow live internet API calls for @pytest.mark.live_network tests",
    )


def pytest_configure(config: pytest.Config) -> None:
    for m in MARKERS:
        config.addinivalue_line("markers", m)
    requested = config.getoption("--e2e-mode", default="auto")
    resolved = resolve_e2e_mode(requested)
    config._jevdb_resolved_mode = resolved  # type: ignore[attr-defined]
    if resolved == "contract":
        print(
            "\n[JEVDB E2E] MODE=contract (implementation incomplete or contract requested — NOT a live pass)",
            file=sys.stderr,
        )
    else:
        print("\n[JEVDB E2E] MODE=live (strict real targets, zero contract fallbacks)", file=sys.stderr)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _snapshot_dir(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    snap: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(root))
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            snap[rel] = h
    return snap


def _git_core_diff() -> str:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--name-only", "HEAD", "--", *CORE_TRACKED_PATHS],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


@pytest.fixture(scope="session")
def e2e_mode(request: pytest.FixtureRequest) -> str:
    requested = request.config.getoption("--e2e-mode")
    return resolve_e2e_mode(requested)


@pytest.fixture(scope="session")
def live_network(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--live-network"))


@pytest.fixture(scope="session")
def e2e_scratch_root() -> Any:
    base = Path(os.environ.get("JEVDB_E2E_SCRATCH_ROOT", str(DEFAULT_SCRATCH_BASE)))
    base.mkdir(parents=True, exist_ok=True)
    run_dir = base / f"run_{os.getpid()}_{uuid4().hex[:8]}"
    tmp_dir = run_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    prev_env = {
        k: os.environ.get(k)
        for k in (
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
        )
    }
    session_rt = run_dir / "session_runtime"
    session_rt.mkdir(parents=True, exist_ok=True)
    session_db = session_rt / "market_research.duckdb"
    session_ledger = session_rt / "paper_ledger.duckdb"
    session_strat = session_rt / "strategies"
    os.environ["TMPDIR"] = str(tmp_dir)
    os.environ["TEMP"] = str(tmp_dir)
    os.environ["TMP"] = str(tmp_dir)
    os.environ["STOCK_RESEARCH_RUNTIME_DIR"] = str(session_rt)
    os.environ["STOCK_RESEARCH_CACHE_DIR"] = str(session_rt / "cache")
    os.environ["STOCK_RESEARCH_DB_PATH"] = str(session_db)
    os.environ["STOCK_RESEARCH_LEDGER_DB_PATH"] = str(session_ledger)
    os.environ["STOCK_RESEARCH_STRATEGY_DIR"] = str(session_strat)
    os.environ["JEVDB_RESEARCH_DB"] = str(session_db)
    os.environ["JEVDB_MARKET_DB"] = str(session_db)
    os.environ["JEVDB_LEDGER_DB"] = str(session_ledger)
    os.environ["JEVDB_PAPER_LEDGER_DB"] = str(session_ledger)
    os.environ["JEVDB_STRATEGY_DIR"] = str(session_strat)
    tempfile.tempdir = str(tmp_dir)

    repo_tmp_before = _snapshot_dir(REPO_ROOT / ".tmp")
    core_diff_before = _git_core_diff()

    try:
        yield run_dir
    finally:
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        tempfile.tempdir = None

        repo_tmp_after = _snapshot_dir(REPO_ROOT / ".tmp")
        core_diff_after = _git_core_diff()
        shutil.rmtree(run_dir, ignore_errors=True)

        if repo_tmp_before != repo_tmp_after:
            raise AssertionError(
                f"E2E isolation violation: /home/tao/JEVDB/.tmp changed during run! "
                f"before={repo_tmp_before} after={repo_tmp_after}"
            )
        if core_diff_before != core_diff_after:
            raise AssertionError(
                f"Core repo immutability violation: git diff changed during run: {core_diff_after}"
            )


@pytest.fixture
def mark_backing(request: pytest.FixtureRequest):
    def _mark(backing: str) -> None:
        request.node._jevdb_backing = backing  # type: ignore[attr-defined]
    return _mark


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" or (rep.when == "setup" and rep.failed):
        jsonl_path = os.environ.get("JEVDB_E2E_CASES_JSONL")
        if jsonl_path:
            mode = getattr(item.config, "_jevdb_resolved_mode", "contract")
            default_backing = "real" if mode == "live" else "contract_mock"
            backing = getattr(item, "_jevdb_backing", default_backing)
            rec = {
                "nodeid": item.nodeid,
                "outcome": rep.outcome,
                "duration_s": round(float(rep.duration), 4),
                "backing": backing,
                "mode": mode,
            }
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


@pytest.fixture
def isolated_env(e2e_scratch_root: Path, monkeypatch: pytest.MonkeyPatch, e2e_mode: str) -> dict[str, Any]:
    case_dir = e2e_scratch_root / f"case_{uuid4().hex[:8]}"
    tmp_dir = case_dir / "tmp"
    strategy_dir = case_dir / "strategies"
    output_dir = case_dir / "output"
    for d in (tmp_dir, strategy_dir, output_dir):
        d.mkdir(parents=True, exist_ok=True)

    db_path = case_dir / "market_research.duckdb"
    ledger_db_path = case_dir / "paper_ledger.duckdb"

    shim_port = str(find_free_port())
    env_overrides = {
        "TMPDIR": str(tmp_dir),
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "STOCK_RESEARCH_RUNTIME_DIR": str(case_dir / "runtime"),
        "STOCK_RESEARCH_CACHE_DIR": str(case_dir / "cache"),
        "JEVDB_RESEARCH_DB": str(db_path),
        "JEVDB_MARKET_DB": str(db_path),
        "STOCK_RESEARCH_DB_PATH": str(db_path),
        "JEVDB_LEDGER_DB": str(ledger_db_path),
        "JEVDB_PAPER_LEDGER_DB": str(ledger_db_path),
        "STOCK_RESEARCH_LEDGER_DB": str(ledger_db_path),
        "STOCK_RESEARCH_LEDGER_DB_PATH": str(ledger_db_path),
        "JEVDB_STRATEGY_DIR": str(strategy_dir),
        "STOCK_RESEARCH_STRATEGY_DIR": str(strategy_dir),
        "JEVDB_SHIM_PORT": shim_port,
        "JEVDB_LLM_SHIM_PORT": shim_port,
        "SEC_USER_AGENT": "JEVDBResearchTest/0.1 (e2e-test@example.com)",
    }
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_dir))

    if e2e_mode == "live":
        try:
            cfg = importlib.import_module("stock_research.config")
            get_settings = getattr(cfg, "get_settings", None)
            if get_settings is not None and hasattr(get_settings, "cache_clear"):
                get_settings.cache_clear()
        except ImportError:
            pass

    yield {
        "case_dir": case_dir,
        "tmp_dir": tmp_dir,
        "db_path": db_path,
        "ledger_db_path": ledger_db_path,
        "strategy_dir": strategy_dir,
        "output_dir": output_dir,
    }

    if e2e_mode == "live":
        try:
            cfg = importlib.import_module("stock_research.config")
            get_settings = getattr(cfg, "get_settings", None)
            if get_settings is not None and hasattr(get_settings, "cache_clear"):
                get_settings.cache_clear()
        except ImportError:
            pass


# ---------------------------------------------------------------------------
# Playwright Headless Chromium Runner (In-process + `arpo` Subprocess Bridge)
# ---------------------------------------------------------------------------

_PLAYWRIGHT_DRIVER_SCRIPT = r"""
import json, sys
from playwright.sync_api import sync_playwright

payload = json.loads(sys.stdin.read())
chromium_path = payload["chromium_path"]
chromium_args = payload["chromium_args"]
viewport = payload.get("viewport", {"width": 1280, "height": 800})
steps = payload["steps"]

console_errors = []
page_errors = []
server_errors = []
api_requests = []
results = {}

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=chromium_path, headless=True, args=chromium_args)
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda err: page_errors.append(str(err)))
    page.on("response", lambda r: server_errors.append({"status": r.status, "url": r.url}) if r.status >= 500 else None)
    page.on("request", lambda req: api_requests.append({"url": req.url, "method": req.method}) if "/api/" in req.url else None)

    for idx, step in enumerate(steps):
        act = step["action"]
        if act == "goto":
            page.goto(step["url"], wait_until=step.get("wait_until", "networkidle"), timeout=step.get("timeout") or 20000)
        elif act == "fill":
            page.locator(step["selector"]).first.fill(step["value"])
        elif act == "click":
            page.locator(step["selector"]).first.click()
        elif act == "click_if_present":
            loc = page.locator(step["selector"])
            if loc.count() > 0:
                loc.first.click()
        elif act == "wait_for_selector":
            page.locator(step["selector"]).first.wait_for(state=step.get("state", "visible"), timeout=step.get("timeout") or 15000)
        elif act == "wait_for_nonempty_text":
            sel = step["selector"]
            excludes = step.get("exclude", [])
            page.wait_for_function(
                (
                    "([s, ex]) => {"
                    " const el = document.querySelector(s);"
                    " if (!el) return false;"
                    " const txt = (el.innerText || el.textContent || '').trim();"
                    " if (!txt) return false;"
                    " return !ex.includes(txt);"
                    "}"
                ),
                arg=[sel, excludes],
                timeout=step.get("timeout") or 20000,
            )
        elif act == "reload":
            page.reload(wait_until="networkidle", timeout=20000)
        elif act == "inspect_selectors":
            inspected = {}
            for sel in step["selectors"]:
                loc = page.locator(sel)
                exists = loc.count() > 0
                visible = loc.first.is_visible() if exists else False
                text = loc.first.inner_text().strip() if exists else ""
                box = loc.first.bounding_box() if exists else None
                chart_info = page.evaluate(
                    (
                        "(s) => {"
                        " const root = document.querySelector(s);"
                        " if (!root) return null;"
                        " const target = root.querySelector('canvas, svg') || root;"
                        " const rect = target.getBoundingClientRect();"
                        " return {tag: target.tagName.toLowerCase(), width: rect.width, height: rect.height};"
                        "}"
                    ),
                    sel,
                )
                inspected[sel] = {
                    "exists": exists,
                    "visible": visible,
                    "text": text,
                    "box": box,
                    "chart": chart_info,
                }
            results[step.get("key", f"inspect_{idx}")] = inspected
        elif act == "inspect_validation":
            info = page.evaluate(
                (
                    "() => {"
                    " const inp = document.querySelector(\"[data-testid='agent-input']\");"
                    " const nativeMsg = inp && inp.validationMessage ? inp.validationMessage : '';"
                    " const nodes = Array.from(document.querySelectorAll('[role=alert], .err, .error, #agent-validation, [data-testid=\"agent-validation\"]'));"
                    " const visibleMsgs = nodes.filter(n => n.offsetParent !== null || n.getBoundingClientRect().height > 0 || (n.innerText || '').trim().length > 0).map(n => (n.innerText || n.textContent || '').trim()).filter(Boolean);"
                    " const bodyText = (document.body && document.body.innerText || '').trim();"
                    " return {native_validation: nativeMsg, visible_msgs: visibleMsgs, body_text: bodyText};"
                    "}"
                )
            )
            results[step.get("key", f"validation_{idx}")] = info
        elif act == "title":
            results[step.get("key", "title")] = page.title()

    browser.close()

print(json.dumps({
    "results": results,
    "console_errors": console_errors,
    "page_errors": page_errors,
    "server_errors": server_errors,
    "api_requests": api_requests,
}, ensure_ascii=False))
"""


class PlaywrightRunner:
    def __init__(self, scratch_dir: Path):
        self.scratch_dir = scratch_dir
        self._has_inprocess = False
        try:
            import playwright.sync_api  # noqa: F401
            self._has_inprocess = True
        except ImportError:
            self._has_inprocess = False

        if self._has_inprocess:
            self.backend = "inprocess_playwright"
        elif Path(ARPO_PYTHON).exists():
            self.backend = "arpo_subprocess_bridge"
        else:
            self.backend = "unavailable"

    def run_steps(
        self,
        steps: list[dict[str, Any]],
        viewport: dict[str, int] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        if self.backend == "unavailable":
            pytest.fail(
                "Neither in-process playwright nor arpo fallback Python is available for headless Chromium tests."
            )
        if not Path(CHROMIUM_PATH).exists():
            pytest.fail(f"Chromium binary missing at {CHROMIUM_PATH}")

        payload = {
            "chromium_path": CHROMIUM_PATH,
            "chromium_args": CHROMIUM_ARGS,
            "viewport": viewport or {"width": 1280, "height": 800},
            "steps": steps,
        }

        if self._has_inprocess:
            py_exe = sys.executable
        else:
            py_exe = ARPO_PYTHON

        # Keep Playwright's TMPDIR short (<64 chars) so Chromium's
        # playwright_chromiumdev_profile-XXXXXX/SingletonSocket stays within the
        # 108-byte AF_UNIX sun_path limit, while never touching /tmp.
        pw_short_tmp = Path("/home/tao/.gemini/antigravity/scratch/teamwork_tmp") / f"p{uuid4().hex[:4]}"
        pw_short_tmp.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["TMPDIR"] = str(pw_short_tmp)
        env["TEMP"] = str(pw_short_tmp)
        env["TMP"] = str(pw_short_tmp)

        proc_timeout = (
            timeout
            if timeout is not None
            else max(90, int(sum(int(s.get("timeout") or 20000) for s in steps) / 1000) + 30)
        )
        try:
            proc = subprocess.run(
                [py_exe, "-c", _PLAYWRIGHT_DRIVER_SCRIPT],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                env=env,
                timeout=proc_timeout,
                check=False,
            )
        finally:
            shutil.rmtree(pw_short_tmp, ignore_errors=True)
        if proc.returncode != 0:
            raise AssertionError(
                f"Playwright execution failed (backend={self.backend}, rc={proc.returncode}):\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        return json.loads(proc.stdout)


@pytest.fixture(scope="session")
def playwright_runner(e2e_scratch_root: Path) -> PlaywrightRunner:
    pw_tmp = e2e_scratch_root / "pw_tmp"
    pw_tmp.mkdir(parents=True, exist_ok=True)
    return PlaywrightRunner(pw_tmp)
