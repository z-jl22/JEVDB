"""Unified CLI runner for the JEVDB Stock Research & Paper-Trading E2E Test Suite."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

from stock_research.tests.e2e.conftest import DEFAULT_SCRATCH_BASE, resolve_e2e_mode

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path("/home/tao/JEVDB")


def build_marker_expr(tier: str, include_self: bool, no_live_network: bool) -> str:
    if tier == "all":
        base = "tier1 or tier2 or tier3 or tier4"
    elif tier in ("1", "2", "3", "4"):
        base = f"tier{tier}"
    else:
        raise ValueError(f"Unsupported tier: {tier!r}")

    if include_self:
        base = f"({base}) or e2e_self"
    if no_live_network:
        base = f"({base}) and not live_network"
    return base


def _select_python_with_pytest() -> str:
    if importlib.util.find_spec("pytest") is not None:
        return sys.executable
    scratch_py = DEFAULT_SCRATCH_BASE / "pytest_venv" / "bin" / "python"
    if scratch_py.exists():
        return str(scratch_py)
    return sys.executable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JEVDB Stock Research E2E Test Runner")
    parser.add_argument("--tier", choices=["1", "2", "3", "4", "all"], default="all")
    parser.add_argument("--mode", choices=["auto", "live", "contract"], default="auto")
    parser.add_argument("--include-self", action="store_true", help="Include e2e_self harness self-tests")
    parser.add_argument("--no-live-network", action="store_true", help="Exclude @pytest.mark.live_network tests")
    parser.add_argument("--live-network", action="store_true", help="Enable live internet calls for live_network tests")
    parser.add_argument("--json-report", type=Path, help="Write structured JSON E2E report to path")
    parser.add_argument("--verbose", action="store_true", help="Verbose pytest output")
    args = parser.parse_args(argv)

    resolved_mode = resolve_e2e_mode(args.mode)
    live_net = bool(args.live_network and not args.no_live_network)
    if resolved_mode == "contract":
        print(
            "[run_e2e] WARNING: MODE=contract (implementation incomplete or contract mode selected — NOT a live pass)",
            file=sys.stderr,
        )
    else:
        print("[run_e2e] MODE=live (strict real targets)", file=sys.stderr)

    scratch_base = Path(os.environ.get("JEVDB_E2E_SCRATCH_ROOT", str(DEFAULT_SCRATCH_BASE)))
    scratch_base.mkdir(parents=True, exist_ok=True)
    runner_dir = scratch_base / f"runner_{os.getpid()}_{uuid4().hex[:8]}"
    tmp_dir = runner_dir / "tmp"
    basetemp_dir = runner_dir / "basetemp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    basetemp_dir.mkdir(parents=True, exist_ok=True)
    cases_jsonl = runner_dir / "cases.jsonl"

    marker_expr = build_marker_expr(args.tier, args.include_self, args.no_live_network)
    py_exe = _select_python_with_pytest()

    cmd = [
        py_exe,
        "-m",
        "pytest",
        str(E2E_DIR),
        "-c",
        str(E2E_DIR / "pytest.ini"),
        "-m",
        marker_expr,
        f"--e2e-mode={args.mode}",
        f"--basetemp={basetemp_dir}",
    ]
    if live_net:
        cmd.append("--live-network")
    if args.verbose:
        cmd.append("-vv")

    env = os.environ.copy()
    env["TMPDIR"] = str(tmp_dir)
    env["TEMP"] = str(tmp_dir)
    env["TMP"] = str(tmp_dir)
    env["JEVDB_E2E_MODE"] = args.mode
    env["JEVDB_E2E_LIVE_NETWORK"] = "1" if live_net else "0"
    env["JEVDB_E2E_CASES_JSONL"] = str(cases_jsonl)
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, check=False)
        duration_s = round(time.perf_counter() - t0, 4)

        cases: list[dict] = []
        if cases_jsonl.exists():
            for line in cases_jsonl.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    cases.append(json.loads(line))

        passed = sum(1 for c in cases if c.get("outcome") == "passed")
        failed = sum(1 for c in cases if c.get("outcome") == "failed")
        skipped = sum(1 for c in cases if c.get("outcome") == "skipped")
        errors = sum(1 for c in cases if c.get("outcome") == "error")

        if args.json_report:
            args.json_report.parent.mkdir(parents=True, exist_ok=True)
            report = {
                "tier": args.tier,
                "requested_mode": args.mode,
                "mode": resolved_mode,
                "live_network": live_net,
                "core_baseline_real": True,
                "marker_expr": marker_expr,
                "exit_code": int(proc.returncode),
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "errors": errors,
                "total_recorded": len(cases),
                "duration_s": duration_s,
                "cases": cases,
            }
            args.json_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        return int(proc.returncode)
    finally:
        shutil.rmtree(runner_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
