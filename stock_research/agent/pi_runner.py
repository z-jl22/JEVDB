"""Runner executing @earendil-works/pi-coding-agent with DeepSeek + JEVDB MCP tools."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from stock_research.agent.verifier import verify_answer_grounding
from stock_research.config import Settings, get_settings

_URL_RE = re.compile(r"https?://[^\s\)\]>\"'`*\\（），。；、]+")
_SOURCE_KEY_RE = re.compile(
    r"\b(?:static_baseline|akshare_ths|akshare_sina|akshare_tx|akshare_cls|akshare_em|yfinance|sec_edgar|finnhub|jevdb_hybrid|ddgs)\b"
)

_RESEARCH_SYSTEM_PROMPT = (
    "You are the JEVDB Stock Research & Quantitative Investment Agent. "
    "Use the available mcp__stock_research__* tools (and read .pi/skills/*/SKILL.md only if needed) "
    "to execute a concise multi-turn research and cross-verification workflow before answering.\n"
    "Mandatory rules:\n"
    "1. Always fetch or query real data via MCP tools (do NOT answer from memory alone).\n"
    "2. Stay focused (aim for 3-6 targeted MCP tool calls total; on recent news or industry research queries, ALWAYS include 1 web_search call to cross-check external sources, and call web_fetch_url at most 1 time only if search snippets lack key figures — do NOT make redundant repeated web_search or web_fetch_url calls).\n"
    "3. Quote exact numeric values, dates, and units verbatim from tool outputs. "
    "Pay strict attention to unit conversion: 1亿元 = 100,000,000元 (divide raw yuan by 1e8, e.g., 9710647471.03 = 97.11亿元, NOT 9.71亿元), and 1万元 = 10,000元 (divide by 1e4). "
    "ALWAYS attach `亿元` or `万元` directly to EVERY converted AkShare raw yuan figure (or put `(亿元)` / `(万元)` in the Markdown table column header), e.g., write `89.54亿元`, `64.75亿元`, `97.11亿元` — NEVER leave a converted number unitless. "
    "Write Chinese period labels such as `前三季度` or `前9个月` (and `上半年` / `第一季度`) rather than shorthand like `9M` / `6M` / `3M`. "
    "Whenever you state ANY derived/calculated number (such as TTM net profit, 3-day sum, implied market cap, target price, or margin-of-safety price), "
    "ALWAYS print the exact raw tool operands on the SAME line at its FIRST appearance in `operand1 op operand2 = result` format with the unit at the end or on the numbers "
    "(e.g., `97.11 + 89.54 - 64.75 = 121.90 亿元`, `245.03 + 227.20 + 194.85 = 667.08 亿元`, `3.46 * 15 = 51.9`). "
    "Never invent hypothetical scenario forecasts, assumed target price ranges, or assumed valuation multiples (e.g., `60–70元`, `130–140亿元`, `1.75倍PB`) unless those exact numbers appear verbatim in tool outputs or are computed via an explicit same-line equation from tool numbers. "
    "Never emit an unshown calculated number, unshown ratio, or unverified target price from memory, and NEVER cite unverified snippet/headline figures from web_search unless those exact figures appear verbatim in tool outputs or you fetch the URL via web_fetch_url first.\n"
    "4. Explicitly list data sources and URLs (e.g., akshare_ths, yfinance, sec_edgar, static_baseline, https://...) in your final answer. Copy URLs character-for-character from tool outputs (never fabricate or truncate a URL to a bare homepage like https://finance.sina.com.cn).\n"
    "5. Respond clearly in the user's language with structured sections."
)


def _resolve_pi_binary(settings: Settings) -> str:
    which_pi = shutil.which("pi")
    if which_pi:
        return which_pi
    local_pi = settings.repo_root / "stock_research" / "node_modules" / ".bin" / "pi"
    if local_pi.exists():
        return str(local_pi)
    raise FileNotFoundError(
        "pi binary not found on PATH or at stock_research/node_modules/.bin/pi. "
        "Install @earendil-works/pi-coding-agent@1.6.1 first."
    )


def _normalize_tool_name(raw_name: str) -> str:
    prefix = "mcp__stock_research__"
    if raw_name.startswith(prefix):
        return raw_name[len(prefix):]
    return raw_name


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _is_internal_url(url: str) -> bool:
    low = url.lower()
    return low.startswith(("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost"))


def _extract_sources_from_text(text: str, tool_name: str) -> list[str]:
    if tool_name == "read":
        return []
    found: list[str] = []
    for url in _URL_RE.findall(text):
        clean_u = url.rstrip(".,;:!?`*")
        if not _is_internal_url(clean_u) and clean_u not in found:
            found.append(clean_u)
    for src_m in _SOURCE_KEY_RE.findall(text):
        if src_m not in found:
            found.append(src_m)
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None

    if isinstance(parsed, dict):
        for k in ("source", "bar_source", "backend"):
            val = parsed.get(k)
            if isinstance(val, str) and val.strip() and val not in found and val != "llm":
                found.append(val.strip())
        srcs = parsed.get("sources")
        if isinstance(srcs, list):
            for s in srcs:
                if isinstance(s, str) and s.strip() and not _is_internal_url(s.strip()) and s.strip() not in found:
                    found.append(s.strip())
        results = parsed.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    u = str(item.get("url") or "").strip()
                    if u.startswith(("http://", "https://")) and not _is_internal_url(u) and u not in found:
                        found.append(u)

    if not found:
        fallback_map = {
            "jevdb_semantic_query": "jevdb_hybrid",
            "web_search": "ddgs",
            "web_fetch_url": "web_fetch",
            "run_backtest_tool": "quant_backtest",
            "compare_strategies_tool": "quant_compare",
            "analyze_portfolio_risk": "portfolio_risk",
            "paper_account_status": "paper_ledger",
            "submit_paper_order": "paper_ledger",
            "list_quant_strategies": "strategy_registry",
            "save_quant_strategy": "strategy_registry",
        }
        if tool_name in fallback_map:
            found.append(fallback_map[tool_name])
    return found


def _summarize_tool_output(raw_text: str, max_len: int = 1200) -> str:
    compact = " ".join(raw_text.split())
    if len(compact) <= max_len:
        return compact
    return compact[:max_len].rstrip() + "..."


def _emit(cb: Callable[[dict[str, Any]], None] | None, event: dict[str, Any]) -> None:
    if cb is not None:
        cb(event)


def _repair_ungrounded_answer(
    *,
    prompt: str,
    draft_answer: str,
    verification: dict[str, Any],
    raw_tool_outputs: list[str],
    settings: Settings,
    sources: list[str] | None = None,
    strict_delete: bool = False,
) -> str | None:
    """Ask DeepSeek to revise only ungrounded numbers/URLs against actual tool outputs."""
    ungrounded = verification.get("ungrounded_numbers") or []
    warnings = verification.get("warnings") or []
    unverified_urls = verification.get("unverified_urls") or []
    if not ungrounded and not warnings and not unverified_urls:
        return None
    if not settings.deepseek_api_key or settings.deepseek_api_key.startswith("test-"):
        return None

    evidence_snippets: list[str] = []
    budget = 120000
    for idx, txt in enumerate(raw_tool_outputs, 1):
        piece = txt[:15000]
        if budget - len(piece) < 0:
            break
        evidence_snippets.append(f"[Tool Output {idx}]\n{piece}")
        budget -= len(piece)

    allowed_http_urls = [s for s in (sources or []) if isinstance(s, str) and s.startswith(("http://", "https://"))]
    strict_banner = (
        f"\nCRITICAL ESCALATION: Previous revision STILL retained ungrounded numbers {ungrounded}. "
        "Do NOT attempt to substitute, round, or recalculate alternative target prices/multiples/ranges for those numbers. "
        "Simply DELETE every clause, scenario range, parenthetical, or table cell containing ANY of those flagged numbers, or rewrite qualitatively WITHOUT digits!\n"
        if strict_delete
        else ""
    )
    repair_prompt = (
        "Your previous draft answer failed automated numeric/source grounding verification.\n"
        f"User Question: {prompt}\n"
        f"Flagged ungrounded numbers (not found verbatim in tool outputs): {ungrounded}\n"
        f"Flagged unverified URLs (MUST be removed or replaced with an exact URL from Allowed URLs): {unverified_urls}\n"
        f"Allowed URLs from tool outputs: {allowed_http_urls}\n"
        f"Flagged URL warnings: {warnings}\n"
        f"{strict_banner}\n"
        "Strict revision instructions:\n"
        "1. Preserve the helpful analysis, structure, and all verified facts/sources.\n"
        "2. Inspect EVERY occurrence of EVERY flagged number in the draft (tables, body paragraphs, AND final summary/conclusions):\n"
        "   - Replace period shorthands like `9M` / `6M` / `3M` (which look like millions) with `前三季度` / `前9个月` / `上半年`.\n"
        "   - ALWAYS attach `亿元` or `万元` directly to EVERY converted AkShare raw yuan figure (or put `(亿元)` / `(万元)` in the Markdown table column header), e.g. `89.54亿元`, `64.75亿元`, `97.11亿元` (double-check ÷ 1e8 for 亿元 vs ÷ 1e4 for 万元: e.g., 9710647471.03 = 97.11亿元, NOT 9.71亿元) — NEVER leave a converted number unitless,\n"
        "   - OR write its exact raw arithmetic formula using verbatim Tool Output numbers on the SAME line with the unit at the end or on the numbers (e.g. `97.11 + 89.54 - 64.75 = 121.90 亿元`, `245.03 + 227.20 + 194.85 = 667.08 亿元`),\n"
        "   - OR DELETE/rephrase without that number (and any dependent target price / derived figure / unverified web_search headline snippet number) if it does not appear verbatim in Tool Outputs.\n"
        "3. Do NOT introduce any new unverified numbers (NEVER extrapolate or cite unverified snippet figures from web_search headlines).\n"
        "4. DELETE every flagged unverified URL (including bare homepage URLs like https://finance.sina.com.cn) or replace it ONLY with an exact character-for-character URL from Allowed URLs.\n"
        "5. Return ONLY the revised final Markdown answer.\n\n"
        f"--- Tool Outputs ---\n{chr(10).join(evidence_snippets)}\n\n"
        f"--- Draft Answer ---\n{draft_answer}"
    )

    try:
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(
                f"{settings.deepseek_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.deepseek_model or "deepseek-flash",
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a strict financial fact-checking editor. Fix ungrounded numbers using ONLY the provided Tool Outputs.",
                        },
                        {"role": "user", "content": repair_prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 16384,
                },
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return None
            content = str((choices[0].get("message") or {}).get("content") or "").strip()
            return content or None
    except Exception:
        return None


def _synthesize_answer_from_tools(
    *,
    prompt: str,
    raw_tool_outputs: list[str],
    sources: list[str],
    settings: Settings,
) -> str | None:
    """Recover cleanly if Pi executed tools (rc=0) but emitted no final assistant text."""
    if not raw_tool_outputs or not settings.deepseek_api_key or settings.deepseek_api_key.startswith("test-"):
        return None
    evidence_snippets: list[str] = []
    budget = 120000
    for idx, txt in enumerate(raw_tool_outputs, 1):
        piece = txt[:15000]
        if budget - len(piece) < 0:
            break
        evidence_snippets.append(f"[Tool Output {idx}]\n{piece}")
        budget -= len(piece)
    synth_prompt = (
        f"User Question: {prompt}\n"
        f"Verified Sources: {sources}\n\n"
        "Write a clear, structured Markdown research answer strictly grounded in the Tool Outputs below.\n"
        "Follow all formatting rules: quote exact tool numbers/units (1亿元=1e8元), show `operand1 op operand2 = result` on the same line for any calculation, and cite exact sources/URLs verbatim.\n\n"
        f"--- Tool Outputs ---\n{chr(10).join(evidence_snippets)}"
    )
    try:
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(
                f"{settings.deepseek_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.deepseek_model or "deepseek-flash",
                    "messages": [
                        {"role": "system", "content": _RESEARCH_SYSTEM_PROMPT},
                        {"role": "user", "content": synth_prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 8192,
                },
            )
            if resp.status_code != 200:
                return None
            choices = resp.json().get("choices") or []
            if not choices:
                return None
            return str((choices[0].get("message") or {}).get("content") or "").strip() or None
    except Exception:
        return None


def _terminate_proc_tree(proc: Any) -> None:
    if os.name == "posix":
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and pid > 0:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


def run_research_query(
    prompt: str,
    mode: str = "auto",
    session_id: str | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    timeout_seconds: float = 300.0,
) -> dict[str, Any]:
    """Execute a multi-turn research query through pi-coding-agent + DeepSeek + JEVDB MCP."""
    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        raise ValueError("Research prompt must be non-empty")

    if isinstance(timeout_seconds, bool):
        raise ValueError("timeout_seconds must be a positive finite number, not a boolean")
    try:
        timeout_val = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid timeout_seconds: {timeout_seconds!r}") from exc
    if not math.isfinite(timeout_val) or timeout_val <= 0:
        raise ValueError(f"timeout_seconds must be finite and > 0, got {timeout_seconds!r}")

    settings = get_settings(ensure_dirs=False)
    if not settings.deepseek_api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is missing; set DEEPSEEK_API_KEY in the environment before running Pi agent."
        )

    pi_bin = _resolve_pi_binary(settings)
    model_id = settings.deepseek_model or "deepseek-flash"

    cmd = [
        pi_bin,
        "--mode",
        "json",
        "--no-session",
        "--approve",
        "--provider",
        "deepseek",
        "--model",
        model_id,
        "--thinking",
        "low",
        "--tools",
        "read,mcp__*",
        "--no-context-files",
        "--append-system-prompt",
        _RESEARCH_SYSTEM_PROMPT,
        "-p",
        clean_prompt,
    ]

    env = os.environ.copy()
    env["DEEPSEEK_API_KEY"] = settings.deepseek_api_key
    if settings.finnhub_api_key:
        env["FINNHUB_API_KEY"] = settings.finnhub_api_key

    t_start = time.perf_counter()
    _emit(on_event, {"type": "status", "stage": "started", "prompt": clean_prompt, "mode": mode, "session_id": session_id})

    popen_kwargs: dict[str, Any] = {
        "cwd": str(settings.repo_root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    assert proc.stdout is not None and proc.stderr is not None

    stderr_chunks: list[bytes] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            try:
                chunk = proc.stderr.read(4096)
            except (ValueError, OSError):
                break
            if not chunk:
                break
            stderr_chunks.append(chunk)

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    tool_calls: list[dict[str, Any]] = []
    non_error_tool_calls: list[dict[str, Any]] = []
    non_error_tool_outputs: list[str] = []
    sources: list[str] = []
    pending_tools: dict[str, tuple[str, dict[str, Any], float]] = {}
    latest_assistant_text = ""
    assistant_error: str | None = None

    def _add_source(src: str) -> None:
        if src and src not in sources:
            sources.append(src)

    def _handle_lines(lines_to_process: list[bytes]) -> None:
        nonlocal latest_assistant_text, assistant_error
        for raw_line in lines_to_process:
            line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue

            ev_type = ev.get("type")
            if ev_type == "tool_execution_start":
                tc_id = str(ev.get("toolCallId") or "")
                raw_name = str(ev.get("toolName") or "")
                short_name = _normalize_tool_name(raw_name)
                args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
                pending_tools[tc_id] = (short_name, args, time.perf_counter())
                _emit(
                    on_event,
                    {
                        "type": "tool_start",
                        "tool_call_id": tc_id,
                        "name": short_name,
                        "args": args,
                    },
                )
            elif ev_type == "tool_execution_end":
                tc_id = str(ev.get("toolCallId") or "")
                raw_name = str(ev.get("toolName") or "")
                short_name = _normalize_tool_name(raw_name)
                started = pending_tools.pop(tc_id, (short_name, {}, time.perf_counter()))
                args = started[1]
                elapsed_ms = max(1, int(ev.get("durationMs") or ((time.perf_counter() - started[2]) * 1000)))
                result_obj = ev.get("result") if isinstance(ev.get("result"), dict) else {}
                raw_text = _extract_text_from_content(result_obj.get("content"))
                if not raw_text and result_obj:
                    raw_text = json.dumps(result_obj, ensure_ascii=False, default=str)
                is_tool_error = (
                    bool(ev.get("isError"))
                    or bool(result_obj.get("isError"))
                    or raw_text.lstrip().startswith("Error executing tool")
                )
                summary = _summarize_tool_output(raw_text)
                entry: dict[str, Any] = {
                    "name": short_name,
                    "args": args,
                    "result_summary": summary,
                    "duration_ms": elapsed_ms,
                }
                if is_tool_error:
                    entry["is_error"] = True
                tool_calls.append(entry)
                if not is_tool_error:
                    non_error_tool_outputs.append(raw_text)
                    non_error_tool_calls.append(entry)
                    for src in _extract_sources_from_text(raw_text, short_name):
                        _add_source(src)
                _emit(
                    on_event,
                    {
                        "type": "tool_end",
                        "tool_call_id": tc_id,
                        **entry,
                        "is_error": is_tool_error,
                    },
                )
            elif ev_type == "message_end":
                msg = ev.get("message")
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    txt = _extract_text_from_content(msg.get("content"))
                    if txt.strip():
                        latest_assistant_text = txt.strip()
                    if msg.get("stopReason") == "error" and msg.get("errorMessage"):
                        assistant_error = str(msg["errorMessage"])
            elif ev_type == "agent_end":
                msgs = ev.get("messages")
                if isinstance(msgs, list) and not latest_assistant_text:
                    for m in reversed(msgs):
                        if isinstance(m, dict) and m.get("role") == "assistant":
                            txt = _extract_text_from_content(m.get("content"))
                            if txt.strip():
                                latest_assistant_text = txt.strip()
                                break
                            if m.get("stopReason") == "error" and m.get("errorMessage"):
                                assistant_error = str(m["errorMessage"])
                                break

    # Read stdout strictly delimited on LF (\n), never str.splitlines() (which splits on U+2028/U+2029).
    buf = b""
    deadline = time.monotonic() + timeout_val
    sel: selectors.BaseSelector | None = None
    timer: threading.Timer | None = None
    timed_out = threading.Event()
    completed_cleanly = False

    fd: int | None = None
    if os.name == "posix" and hasattr(proc.stdout, "fileno"):
        try:
            cand_fd = proc.stdout.fileno()
            if isinstance(cand_fd, int) and cand_fd >= 0:
                fd = cand_fd
        except (OSError, ValueError, AttributeError):
            fd = None

    try:
        if fd is not None:
            os.set_blocking(fd, False)
            sel = selectors.DefaultSelector()
            sel.register(fd, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Pi agent timed out after {timeout_seconds}s")
                events = sel.select(timeout=remaining)
                if not events:
                    raise TimeoutError(f"Pi agent timed out after {timeout_seconds}s")
                try:
                    chunk = os.read(fd, 8192)
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    if buf.strip():
                        _handle_lines([buf])
                        buf = b""
                    break
                buf += chunk
                if b"\n" in buf:
                    lines = buf.split(b"\n")
                    buf = lines.pop()
                    _handle_lines(lines)
        else:
            def _on_timer() -> None:
                timed_out.set()
                _terminate_proc_tree(proc)

            timer = threading.Timer(max(0.001, timeout_val), _on_timer)
            timer.daemon = True
            timer.start()
            while True:
                if timed_out.is_set() or time.monotonic() > deadline:
                    raise TimeoutError(f"Pi agent timed out after {timeout_seconds}s")
                chunk = proc.stdout.read1(8192) if hasattr(proc.stdout, "read1") else proc.stdout.readline()
                if timed_out.is_set() or time.monotonic() > deadline:
                    raise TimeoutError(f"Pi agent timed out after {timeout_seconds}s")
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                if not chunk:
                    if buf.strip():
                        _handle_lines([buf])
                        buf = b""
                    break
                buf += chunk
                if b"\n" in buf:
                    lines = buf.split(b"\n")
                    buf = lines.pop()
                    _handle_lines(lines)

        rem_wait = deadline - time.monotonic()
        if rem_wait <= 0:
            raise TimeoutError(f"Pi agent timed out after {timeout_seconds}s")
        try:
            returncode = proc.wait(timeout=min(15.0, max(0.05, rem_wait)))
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Pi agent timed out after {timeout_seconds}s") from exc

        stderr_thread.join(timeout=3.0)
        completed_cleanly = True
    finally:
        if timer is not None:
            timer.cancel()
        if sel is not None:
            try:
                sel.close()
            except Exception:
                pass
        if not completed_cleanly:
            _terminate_proc_tree(proc)
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None and hasattr(pipe, "close"):
                    try:
                        pipe.close()
                    except OSError:
                        pass
            try:
                proc.wait(timeout=5.0)
            except (subprocess.TimeoutExpired, OSError):
                pass
            stderr_thread.join(timeout=1.0)

    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()

    if assistant_error and not latest_assistant_text:
        raise RuntimeError(f"Pi agent error: {assistant_error}")
    if returncode != 0 and not latest_assistant_text:
        raise RuntimeError(f"Pi agent exited with code {returncode}: {stderr_text}")

    answer = latest_assistant_text.strip()
    if not answer and tool_calls and non_error_tool_outputs:
        synth = _synthesize_answer_from_tools(
            prompt=clean_prompt,
            raw_tool_outputs=non_error_tool_outputs,
            sources=sources,
            settings=settings,
        )
        if synth:
            answer = synth
    if not answer:
        raise RuntimeError(f"Pi agent produced an empty answer (rc={returncode}): {stderr_text}")

    # Capture any URLs in answer that are backed by non-error tool outputs (exact token match, no prefix spoofing)
    combined_tool_text = "\n".join(non_error_tool_outputs)
    tool_urls = {u.rstrip(".,;:!?`*/") for u in _URL_RE.findall(combined_tool_text)}
    for url in _URL_RE.findall(answer):
        clean_u = url.rstrip(".,;:!?`*")
        norm_u = clean_u.rstrip("/")
        if not _is_internal_url(clean_u) and (
            norm_u in tool_urls
            or (norm_u.startswith("https://") and ("http://" + norm_u[8:]) in tool_urls)
        ):
            _add_source(clean_u)

    verification = verify_answer_grounding(
        answer=answer,
        tool_calls=non_error_tool_calls,
        sources=sources,
        raw_tool_outputs=non_error_tool_outputs,
    )
    for _attempt in range(3):
        if verification.get("grounded") or not non_error_tool_calls:
            break
        repaired = _repair_ungrounded_answer(
            prompt=clean_prompt,
            draft_answer=answer,
            verification=verification,
            raw_tool_outputs=non_error_tool_outputs,
            settings=settings,
            sources=sources,
            strict_delete=(_attempt >= 1),
        )
        if not repaired:
            if _attempt == 0:
                continue
            break
        repaired_ver = verify_answer_grounding(
            answer=repaired,
            tool_calls=non_error_tool_calls,
            sources=sources,
            raw_tool_outputs=non_error_tool_outputs,
        )
        old_nums = len(verification.get("ungrounded_numbers", []))
        new_nums = len(repaired_ver.get("ungrounded_numbers", []))
        old_urls = len(verification.get("unverified_urls", []))
        new_urls = len(repaired_ver.get("unverified_urls", []))
        if repaired_ver.get("grounded") or (new_nums + new_urls) < (old_nums + old_urls):
            answer = repaired
            verification = repaired_ver
        elif _attempt == 0:
            continue
        else:
            break

    duration_ms = max(1, int((time.perf_counter() - t_start) * 1000))

    _emit(on_event, {"type": "verification", **verification})
    _emit(
        on_event,
        {
            "type": "final_answer",
            "answer": answer,
            "sources": sources,
            "duration_ms": duration_ms,
        },
    )

    return {
        "prompt": clean_prompt,
        "tool_calls": tool_calls,
        "verification": verification,
        "answer": answer,
        "sources": sources,
        "duration_ms": duration_ms,
    }
