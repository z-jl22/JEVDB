"""Post-response numeric and source grounding verifier for research answers."""

from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
import json
import math
import re
from typing import Any

# ASCII numeric tokens including optional thousands separators and decimals,
# matching cleanly even when adjacent to CJK characters (where Python's \b fails).
_NUM_PAT = r"(?:(?:\d{1,3}(?:,\d{3})+)|\d+)(?:\.\d+)?"
_NUM_RE = re.compile(rf"(?<![A-Za-z0-9_.]){_NUM_PAT}(?![A-Za-z0-9_])(?!\.\d)")
_EVIDENCE_SCI_RE = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?[eE][+-]?\d+(?![A-Za-z0-9_.])")
_TICKER_CODE_RE = re.compile(r"(?<![A-Za-z0-9])(?:sh|sz|bj)?(\d{6})(?:\.(?:SH|SZ|SS|BJ))?(?![A-Za-z0-9])", re.IGNORECASE)
_DATE_SEP_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})(?:日|T|\b|(?![0-9]))")
_DATE_COMPACT_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
_TIME_ISO_RE = re.compile(r"T(\d{2}):(\d{2})(?::(\d{2}))?(?!\d)")
_UNIT_NUM_RE = re.compile(
    rf"(?<![A-Za-z0-9_.])({_NUM_PAT})(trillions?|billions?|millions?|thousands?|percent|yuan|tn|bn|mn|USD|CNY|RMB|HKD|EUR|GBP|JPY|CAD|AUD|CHF|SGD|KRW|TWD|PCT|BPS|pts|bp|PP|[TBMKtbmXx])(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_EVIDENCE_SPACED_UNIT_RE = re.compile(
    rf"(?<![A-Za-z0-9_.])({_NUM_PAT})\s+(trillions?|tn|billions?|bn|millions?|mn|thousands?)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_UNIT_MULTIPLIERS: dict[str, float] = {
    "T": 1e12,
    "TN": 1e12,
    "TRILLION": 1e12,
    "TRILLIONS": 1e12,
    "B": 1e9,
    "BN": 1e9,
    "BILLION": 1e9,
    "BILLIONS": 1e9,
    "M": 1e6,
    "MN": 1e6,
    "MILLION": 1e6,
    "MILLIONS": 1e6,
    "K": 1e3,
    "THOUSAND": 1e3,
    "THOUSANDS": 1e3,
    "X": 1.0,
    "PERCENT": 1.0,
    "YUAN": 1.0,
    "USD": 1.0,
    "CNY": 1.0,
    "RMB": 1.0,
    "HKD": 1.0,
    "EUR": 1.0,
    "GBP": 1.0,
    "JPY": 1.0,
    "CAD": 1.0,
    "AUD": 1.0,
    "CHF": 1.0,
    "SGD": 1.0,
    "KRW": 1.0,
    "TWD": 1.0,
    "PCT": 1.0,
    "BPS": 1.0,
    "BP": 1.0,
    "PTS": 1.0,
    "PP": 1.0,
}
_URL_RE = re.compile(r"https?://[^\s\)\]>\"'`*\\（），。；、]+")
_ARITH_RIGHT_STEP_RE = re.compile(
    rf"[)）\s]*(?:(?:\*\*|__|`)[\s]*)?[+\-−*×/÷=＝≈][(（\s]*(?:(?:\*\*|__|`)[\s]*)?约?{_NUM_PAT}"
)
_ARITH_RIGHT_END_RE = re.compile(
    r"[)）\s]*(?:(?:\*\*|__|`)[\s]*)?(?:[+\-−*×/÷=＝≈][(（\s]*(?:(?:\*\*|__|`)[\s]*)?)?[（(]?(亿|万)元?"
)
_EQ_CHAIN_TAIL_RE = re.compile(
    rf"(?:^|[：:\s(（*_`])({_NUM_PAT}(?:\s*[+\-−]\s*{_NUM_PAT}){{1,7}})\s*[)）*_`]{{0,2}}\s*(?:[亿万]?元?|[%％])?\s*[*_`]*$"
)
_EQ_CHAIN_LEAD_RE = re.compile(
    rf"^\s*[*_`]*\s*[（(]?\s*({_NUM_PAT}(?:\s*[+\-−]\s*{_NUM_PAT}){{1,7}})(?=\s*[)）*_`]{{0,2}}\s*(?:[亿万]?元?|[%％])?(?:$|[，,。；;!?！？|\s（(*_`]))"
)
_SIGNED_NUM_RE = re.compile(rf"([+\-−]?)\s*({_NUM_PAT})")
_SPACED_SCALE_T_RE = re.compile(r"^\s*(?:trillions?|tn|T)(?![A-Za-z0-9_+])", re.IGNORECASE)
_SPACED_SCALE_B_RE = re.compile(r"^\s*(?:billions?|bn|B)(?![A-Za-z0-9_+])", re.IGNORECASE)
_SPACED_SCALE_M_RE = re.compile(r"^\s*(?:millions?|mn|M)(?![A-Za-z0-9_+])", re.IGNORECASE)
_SPACED_SCALE_K_RE = re.compile(r"^\s*(?:thousands?|K)(?![A-Za-z0-9_+])", re.IGNORECASE)
_MONETARY_AFTER_RE = re.compile(
    r"^\s*(?:[亿万]?(?:元|美元|港[币元]|日元|欧元|英镑)|人民币|倍|个百分点|(?:USD|CNY|RMB|HKD|EUR|GBP|JPY|CAD|AUD|CHF|SGD|KRW|TWD|yuan|pct|percent|bps?|pts|pp)(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)
_MONETARY_COL_HEADER_RE = re.compile(
    r"(?:[亿万元]|美元|港[币元]|人民币|目标价|收盘价|开盘价|最高价|最低价|股价|现价|市盈率|市净率|市值|营收|营业收入|净利润|成交额|涨跌幅|收益率|trillion|billion|million|thousand|(?<![A-Za-z])(?:PE|PB|ROE|EPS|BPS|USD|CNY|RMB|HKD|EUR|GBP|JPY|CAD|AUD|CHF|SGD|KRW|TWD)(?![A-Za-z]))",
    re.IGNORECASE,
)
_PERIOD_NUMS = frozenset({"3", "6", "9", "12"})
_PERIOD_PREFIX_RE = re.compile(
    r"(?:(?:19|20)\d{2}\s*(?:年|[-/])?\s*|FY\s*(?:19|20)?\d{2}\s*(?:[-/])?\s*|前\s*)$",
    re.IGNORECASE,
)
_PERIOD_SUFFIX_RE = re.compile(
    r"^\s*(?:(?:(?:19|20)\d{2}|(?:['’\-/]\s*)?[12]\d|FY\s*(?:19|20)?\d{2})(?!\d|\s*(?:\$|¥|￥|元|美元|港[币元]|日元|欧元|英镑|人民币|USD|CNY|RMB|HKD|EUR|GBP|JPY))|同比|环比|财报|业绩|数据|累计)",
    re.IGNORECASE,
)
_PERIOD_BAD_PREFIX_RE = re.compile(
    r"(?:市值|总市值|营收|营业收入|总营收|收入|净利润|利润|目标价|股价|现价|收盘价|规模|融资|成交额?|资产|负债|价格?|成本|\$|¥|￥|(?:USD|CNY|RMB|HKD|EUR|GBP|JPY|CAD|AUD|CHF|SGD|KRW|TWD))\s*[为达约：:=\s(（$¥￥]*$",
    re.IGNORECASE,
)
_PERIOD_BAD_SUFFIX_RE = re.compile(
    r"^\s*(?:\$|¥|￥|元|美元|港[币元]|日元|欧元|英镑|人民币|(?:USD|CNY|RMB|HKD|EUR|GBP|JPY|CAD|AUD|CHF|SGD|KRW|TWD|yuan)(?![A-Za-z0-9_]))",
    re.IGNORECASE,
)
_EQ_LEFT_BOUND_RE = re.compile(r"[=＝≈]\s*$")
_EQ_RIGHT_BOUND_RE = re.compile(r"^\s*[=＝≈]")
_EQ_RIGHT_BOUND_AT_RE = re.compile(r"\s*[=＝≈]")
_EQ_CLAUSE_SEP_RE = re.compile(r"[。；;!?！？|]")
_EQ_OP_RE = re.compile(r"[=＝≈]")
_EQ_SEG_FORBIDDEN_RE = re.compile(r"[\d+\-−*×/÷]")
_EQ_SEG_FORBIDDEN_LAST_RE = re.compile(r".*([\d+\-−*×/÷])")
_T_PLUS_LEFT_RE = re.compile(r"(?<![A-Za-z0-9_])T\s*\+\s*$", re.IGNORECASE)
_T_PLUS_INLINE_RE = re.compile(r"(?<![A-Za-z0-9_])T\s*\+\s*[01](?![0-9])", re.IGNORECASE)
_CLAUSE_ARITH_OP_RE = re.compile(r"[+−*×÷]|(?:\d|[)）])\s*/\s*(?:\d|[(（])|(?:\d|[)）])\s+-\s+(?:\d|[(（])")
_EXPLICIT_YUAN_RE = re.compile(r"^\s*元")
_AFTER_STOP_SPLIT_RE = re.compile(r"[，,。；;!?！？|\d]")
_AFTER_TOK_SPLIT_RE = re.compile(r"[，,。；;!?！？|\d\n]")
_HAS_T_HDR_RE = re.compile(r"\b(?:T|tn|USD\s*T|trillions?)\b", re.IGNORECASE)
_HAS_B_HDR_RE = re.compile(r"\b(?:B|bn|USD\s*B|billions?)\b", re.IGNORECASE)
_HAS_M_HDR_RE = re.compile(r"\b(?:M|mn|USD\s*M|millions?)\b", re.IGNORECASE)
_HAS_K_HDR_RE = re.compile(r"\b(?:K|thousands?)\b", re.IGNORECASE)
_LEFT_YI_TAIL_RE = re.compile(r"亿元?\s*[:：)）=＝≈\s]*$")
_LEFT_WAN_TAIL_RE = re.compile(r"万元?\s*[:：)）=＝≈\s]*$")
_LIST_ITEM_NUM_RE = re.compile(r"(?m)^\s*(?:#{1,6}\s*)?(\d+)\s*(?:\.(?!\d)(?=\s+\S)|、(?!\d)|[\)）])")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:\-\|]+\|\s*$")

# Common non-financial marker numbers allowed in prose (years, list bullets, T+1, 100-share lot, 95% VaR, 3-day window).
_HARMLESS_INTEGERS = frozenset({0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 20, 30, 50, 60, 90, 95, 100, 120, 250, 252})


class _SortedFloatIndex:
    """Sorted index over non-zero working floats for O(log M) tolerance lookups."""

    def __init__(self, floats: list[float]) -> None:
        self.has_zero: bool = False
        nz: list[float] = []
        abs_nz: list[float] = []
        cap_cands: list[float] = []
        ret_cands: list[float] = []
        pct_cands: list[float] = []
        base_cands: list[float] = []
        small_pcts: list[float] = []
        for ev in floats:
            if ev == 0.0:
                self.has_zero = True
            elif math.isfinite(ev):
                nz.append(ev)
                aev = abs(ev)
                abs_nz.append(aev)
                if len(cap_cands) < 5 and ev >= 10000.0 and ev % 10000.0 == 0.0:
                    cap_cands.append(ev)
                if len(ret_cands) < 30 and -0.99 <= ev <= 5.0:
                    ret_cands.append(ev)
                if len(pct_cands) < 40 and 0.0 < aev < 30.0:
                    pct_cands.append(ev)
                if len(base_cands) < 60 and aev >= 10.0:
                    base_cands.append(ev)
                if len(small_pcts) < 30 and 0.0 < aev < 35.0:
                    small_pcts.append(aev)
        nz.sort()
        abs_nz.sort()
        self.sorted_nonzero: list[float] = nz
        self.sorted_abs_nonzero: list[float] = abs_nz
        self.cap_cands: list[float] = cap_cands
        self.ret_cands: list[float] = ret_cands
        self.pct_cands: list[float] = pct_cands
        self.base_cands: list[float] = base_cands
        self.small_pcts: list[float] = small_pcts

    def add(self, ev: float) -> None:
        if ev == 0.0:
            self.has_zero = True
        elif math.isfinite(ev):
            insort(self.sorted_nonzero, ev)
            aev = abs(ev)
            insort(self.sorted_abs_nonzero, aev)
            if len(self.cap_cands) < 5 and ev >= 10000.0 and ev % 10000.0 == 0.0:
                self.cap_cands.append(ev)
            if len(self.ret_cands) < 30 and -0.99 <= ev <= 5.0:
                self.ret_cands.append(ev)
            if len(self.pct_cands) < 40 and 0.0 < aev < 30.0:
                self.pct_cands.append(ev)
            if len(self.base_cands) < 60 and aev >= 10.0:
                self.base_cands.append(ev)
            if len(self.small_pcts) < 30 and 0.0 < aev < 35.0:
                self.small_pcts.append(aev)

    def has_direct_match(self, val: float, *, is_decimal: bool, dec_places: int) -> bool:
        bound = max(0.06, abs(val) * 2e-4 + 0.06)
        nz = self.sorted_nonzero
        lo = bisect_left(nz, val - bound)
        upper = val + bound
        n = len(nz)
        while lo < n:
            ev = nz[lo]
            if ev > upper:
                break
            tol = max(0.015, abs(ev) * 1e-4)
            if is_decimal and dec_places == 1 and abs(ev) >= 10.0:
                tol = max(tol, 0.0501)
            if abs(val - ev) <= tol:
                return True
            lo += 1
        return False

    def has_line_direct_match(self, val: float) -> bool:
        bound = max(0.06, abs(val) * 2e-3 + 0.06)
        nz = self.sorted_nonzero
        lo = bisect_left(nz, val - bound)
        upper = val + bound
        n = len(nz)
        while lo < n:
            ev = nz[lo]
            if ev > upper:
                break
            if abs(val - ev) <= max(0.0501, abs(ev) * 1e-3):
                return True
            lo += 1
        return False

    def has_scaled_unit_match(self, scaled_val: float, tol_scaled: float) -> bool:
        nz = self.sorted_nonzero
        lo = bisect_left(nz, scaled_val - tol_scaled - 1e-9)
        upper = scaled_val + tol_scaled + 1e-9
        n = len(nz)
        while lo < n:
            ev = nz[lo]
            if ev > upper:
                break
            if abs(scaled_val - ev) <= tol_scaled:
                return True
            lo += 1
        return False

    def has_abs_div_match(self, val: float, div_scale: float, min_abs_ev: float, tol_val: float) -> bool:
        anz = self.sorted_abs_nonzero
        lo_target = max(min_abs_ev, (val - tol_val) * div_scale - 1.0)
        hi_target = (val + tol_val) * div_scale + 1.0
        lo = bisect_left(anz, lo_target)
        n = len(anz)
        while lo < n:
            aev = anz[lo]
            if aev > hi_target:
                break
            if abs(val - (aev / div_scale)) <= tol_val:
                return True
            lo += 1
        return False

    def has_abs_mul_match(self, val: float, mul_scale: float, min_abs_ev: float, max_abs_ev: float, tol_val: float) -> bool:
        anz = self.sorted_abs_nonzero
        lo_target = max(min_abs_ev, (val - tol_val) / mul_scale - 1e-9)
        hi_target = min(max_abs_ev, (val + tol_val) / mul_scale + 1e-9)
        lo = bisect_left(anz, lo_target)
        n = len(anz)
        while lo < n:
            aev = anz[lo]
            if aev >= max_abs_ev or aev > hi_target:
                break
            if aev > min_abs_ev and abs(val - aev * mul_scale) <= tol_val:
                return True
            lo += 1
        return False

    def has_abs_mul_inclusive_match(
        self, val: float, mul_scale: float, min_abs_ev: float, max_abs_ev: float, tol_val: float
    ) -> bool:
        anz = self.sorted_abs_nonzero
        lo_target = max(min_abs_ev, (val - tol_val) / mul_scale - 1e-9)
        hi_target = min(max_abs_ev, (val + tol_val) / mul_scale + 1e-9)
        lo = bisect_left(anz, lo_target)
        n = len(anz)
        while lo < n:
            aev = anz[lo]
            if aev > max_abs_ev or aev > hi_target:
                break
            if aev >= min_abs_ev and abs(val - aev * mul_scale) <= tol_val:
                return True
            lo += 1
        return False

    def has_cap_diff_match(self, val: float, cap: float, tol: float = 0.15) -> bool:
        nz = self.sorted_nonzero
        n = len(nz)
        lo1 = bisect_left(nz, max(1000.0, cap + val - tol - 1e-9))
        hi1 = cap + val + tol + 1e-9
        while lo1 < n:
            ev = nz[lo1]
            if ev > hi1:
                break
            if ev >= 1000.0 and ev != cap and abs(val - abs(ev - cap)) <= tol:
                return True
            lo1 += 1
        lo2 = bisect_left(nz, max(1000.0, cap - val - tol - 1e-9))
        hi2 = cap - val + tol + 1e-9
        if hi2 >= 1000.0:
            while lo2 < n:
                ev = nz[lo2]
                if ev > hi2:
                    break
                if ev >= 1000.0 and ev != cap and abs(val - abs(ev - cap)) <= tol:
                    return True
                lo2 += 1
        return False


def _extract_numeric_strings(text: str) -> list[str]:
    if not text:
        return []
    return [m.group(0) for m in _NUM_RE.finditer(text)]


def _build_evidence_numbers(corpus: str) -> tuple[set[str], list[float]]:
    text = corpus or ""
    raw_tokens = _extract_numeric_strings(text)
    sci_tokens = [m.group(0) for m in _EVIDENCE_SCI_RE.finditer(text)]
    norm_strings: set[str] = set()
    floats: list[float] = []

    def _add_numeric_token(clean: str, multiplier: float = 1.0) -> None:
        if multiplier == 1.0:
            norm_strings.add(clean)
        try:
            val = float(clean) * multiplier
            if math.isfinite(val):
                floats.append(val)
                if multiplier == 1.0 and "." in clean and "e" not in clean.lower():
                    norm_strings.add(f"{val:.1f}")
                    norm_strings.add(f"{val:.2f}")
        except ValueError:
            pass

    for tok in raw_tokens + sci_tokens:
        _add_numeric_token(tok.replace(",", ""))

    # Unit/multiplier-suffixed evidence tokens (e.g. 15.8x, 1.23B, 456.7M, 12.3K, 1.5bn, 2.3million, 3.5T)
    for m in _UNIT_NUM_RE.finditer(text):
        clean = m.group(1).replace(",", "")
        mult = _UNIT_MULTIPLIERS.get(m.group(2).upper(), 1.0)
        _add_numeric_token(clean)
        if mult != 1.0:
            _add_numeric_token(clean, mult)

    # Space-separated English scale tokens (e.g. "1.23 billion", "3.5 trillion")
    for m in _EVIDENCE_SPACED_UNIT_RE.finditer(text):
        clean = m.group(1).replace(",", "")
        mult = _UNIT_MULTIPLIERS.get(m.group(2).upper(), 1.0)
        _add_numeric_token(clean)
        if mult != 1.0:
            _add_numeric_token(clean, mult)

    # 6-digit ticker codes with market prefix/suffix (e.g. 000858.SZ, sh600900) -> strings only
    for m in _TICKER_CODE_RE.finditer(text):
        norm_strings.add(m.group(1))

    # ISO / slash / CJK / compact dates -> string components only
    for m in _DATE_SEP_RE.finditer(text):
        yyyy, mm_s, dd_s = m.group(1), m.group(2), m.group(3)
        mm_i, dd_i = int(mm_s), int(dd_s)
        if 1 <= mm_i <= 12 and 1 <= dd_i <= 31:
            norm_strings.update(
                {
                    yyyy,
                    mm_s,
                    dd_s,
                    f"{mm_i:02d}",
                    f"{dd_i:02d}",
                    str(mm_i),
                    str(dd_i),
                    f"{yyyy}{mm_i:02d}{dd_i:02d}",
                }
            )
    for m in _DATE_COMPACT_RE.finditer(text):
        yyyy, mm_s, dd_s = m.group(1), m.group(2), m.group(3)
        norm_strings.update(
            {
                yyyy,
                mm_s,
                dd_s,
                str(int(mm_s)),
                str(int(dd_s)),
                f"{yyyy}{mm_s}{dd_s}",
            }
        )
    for m in _TIME_ISO_RE.finditer(text):
        hh, mm_s, ss_s = m.group(1), m.group(2), m.group(3)
        norm_strings.update({hh, str(int(hh)), mm_s, str(int(mm_s))})
        if ss_s:
            norm_strings.update({ss_s, str(int(ss_s))})

    return norm_strings, floats


def _get_table_header_cells(
    answer: str,
    line_start: int,
    line_text: str,
    table_header_cache: dict[int, tuple[list[str], list[str]]] | None = None,
) -> list[str]:
    if not line_text.lstrip().startswith("|"):
        return []
    if table_header_cache is not None and line_start in table_header_cache:
        return table_header_cache[line_start][0]

    visited: list[tuple[int, str]] = []
    cur_start = line_start
    header_below: list[str] = []

    while cur_start > 0:
        prev_end = cur_start - 1
        prev_nl = answer.rfind("\n", 0, prev_end)
        prev_start = prev_nl + 1
        prev_line = answer[prev_start:prev_end].rstrip("\r")
        if not prev_line.lstrip().startswith("|"):
            break
        if table_header_cache is not None and prev_start in table_header_cache:
            header_below = table_header_cache[prev_start][1]
            break
        visited.append((prev_start, prev_line))
        cur_start = prev_start

    for ls, raw_l in reversed(visited):
        row_header = header_below
        if not header_below and not _TABLE_SEP_RE.match(raw_l):
            header_below = raw_l.split("|")
        if table_header_cache is not None:
            table_header_cache[ls] = (row_header, header_below)

    cur_header = header_below
    if table_header_cache is not None:
        next_below = header_below
        if not next_below and not _TABLE_SEP_RE.match(line_text):
            next_below = line_text.split("|")
        table_header_cache[line_start] = (cur_header, next_below)
    return cur_header


def _get_line_pipe_positions(
    line_text: str,
    line_start: int,
    pipe_pos_cache: dict[int, list[int]] | None = None,
) -> list[int]:
    if pipe_pos_cache is not None:
        cached = pipe_pos_cache.get(line_start)
        if cached is not None:
            return cached
    positions: list[int] = []
    pos = line_text.find("|")
    while pos != -1:
        positions.append(pos)
        pos = line_text.find("|", pos + 1)
    if pipe_pos_cache is not None:
        pipe_pos_cache[line_start] = positions
    return positions


def _extract_table_col_header(
    answer: str,
    line_start: int,
    line_text: str,
    rel_start: int,
    *,
    table_header_cache: dict[int, tuple[list[str], list[str]]] | None = None,
    pipe_pos_cache: dict[int, list[int]] | None = None,
) -> str:
    cells = _get_table_header_cells(
        answer,
        line_start,
        line_text,
        table_header_cache=table_header_cache,
    )
    if not cells:
        return ""
    if pipe_pos_cache is not None:
        pipe_positions = _get_line_pipe_positions(line_text, line_start, pipe_pos_cache)
        col_idx = bisect_left(pipe_positions, rel_start)
    else:
        col_idx = line_text[:rel_start].count("|")
    if 0 <= col_idx < len(cells):
        return cells[col_idx]
    return ""


def _is_inside_url(
    answer: str,
    match_start: int,
    url_spans: list[tuple[int, int]] | None = None,
) -> bool:
    spans = url_spans if url_spans is not None else [(m.start(), m.end()) for m in _URL_RE.finditer(answer)]
    return any(s <= match_start < e for s, e in spans)


def _match_spaced_scale_mult(after_text: str) -> float:
    if _SPACED_SCALE_T_RE.match(after_text):
        return 1e12
    if _SPACED_SCALE_B_RE.match(after_text):
        return 1e9
    if _SPACED_SCALE_M_RE.match(after_text):
        return 1e6
    if _SPACED_SCALE_K_RE.match(after_text):
        return 1e3
    return 1.0


def _is_calendar_period_label(
    answer: str,
    match_start: int,
    match_end: int,
    num_tok: str,
    suffix: str,
    *,
    table_header_cache: dict[int, tuple[list[str], list[str]]] | None = None,
    pipe_pos_cache: dict[int, list[int]] | None = None,
) -> bool:
    """Return True only when an integer '3M'/'6M'/'9M'/'12M' token is unambiguously a calendar period label."""
    if suffix != "M" or num_tok not in _PERIOD_NUMS:
        return False
    line_start = answer.rfind("\n", 0, match_start) + 1
    line_end = answer.find("\n", match_end)
    if line_end == -1:
        line_end = len(answer)
    line_text = answer[line_start:line_end]
    rel_start = match_start - line_start
    rel_end = match_end - line_start
    before_line = line_text[:rel_start]
    after_line = line_text[rel_end:]

    if _PERIOD_BAD_PREFIX_RE.search(before_line):
        return False
    if _PERIOD_BAD_SUFFIX_RE.match(after_line):
        return False

    col_header = _extract_table_col_header(
        answer,
        line_start,
        line_text,
        rel_start,
        table_header_cache=table_header_cache,
        pipe_pos_cache=pipe_pos_cache,
    )
    if col_header and _MONETARY_COL_HEADER_RE.search(col_header):
        return False

    return bool(_PERIOD_PREFIX_RE.search(before_line) or _PERIOD_SUFFIX_RE.match(after_line))


def _find_before_clause_start(line_text: str, pos: int) -> int:
    limit = max(0, pos - 256)
    best = limit
    for sep in "，。；;!?！？|":
        idx = line_text.rfind(sep, limit, pos)
        if idx != -1 and idx + 1 > best:
            best = idx + 1
    search_end = pos
    n = len(line_text)
    while search_end > best:
        c_idx = line_text.rfind(",", best, search_end)
        if c_idx == -1:
            break
        prev_digit = c_idx > 0 and line_text[c_idx - 1].isdigit()
        next_3digits = (
            c_idx + 3 < pos
            and line_text[c_idx + 1:c_idx + 4].isdigit()
            and (c_idx + 4 >= n or not line_text[c_idx + 4].isdigit())
        )
        if not (prev_digit and next_3digits):
            return c_idx + 1
        search_end = c_idx
    return best


def _find_right_arith_unit(line_text: str, r_pos: int, right_memo: dict[int, str]) -> str:
    if r_pos in right_memo:
        return right_memo[r_pos]
    visited = [r_pos]
    cur = r_pos
    while True:
        m_step = _ARITH_RIGHT_STEP_RE.match(line_text, cur)
        if not m_step:
            break
        cur = m_step.end()
        if cur in right_memo:
            unit = right_memo[cur]
            for p in visited:
                right_memo[p] = unit
            return unit
        visited.append(cur)
    m_right_unit = _ARITH_RIGHT_END_RE.match(line_text, cur)
    unit = m_right_unit.group(1) if m_right_unit else ""
    for p in visited:
        right_memo[p] = unit
    return unit


def _has_left_arith_units(
    line_text: str,
    rel_start: int,
    left_memo: dict[int, tuple[bool, bool]],
) -> tuple[bool, bool]:
    if rel_start in left_memo:
        return left_memo[rel_start]
    visited: list[int] = [rel_start]
    idx = rel_start
    while idx > 0:
        saved = idx
        while idx > 0 and line_text[idx - 1] in "()（） \t\r\n":
            idx -= 1
        if idx > 0 and line_text[idx - 1] in "+-−*×/÷=＝≈":
            idx -= 1
        else:
            idx = saved
            break
        while idx > 0 and line_text[idx - 1] in ")） \t\r\n":
            idx -= 1
        if idx <= 0 or not line_text[idx - 1].isdigit():
            idx = saved
            break
        frac_start = idx
        while frac_start > 0 and line_text[frac_start - 1].isdigit():
            frac_start -= 1
        if frac_start > 1 and line_text[frac_start - 1] == "." and line_text[frac_start - 2].isdigit():
            int_end = frac_start - 1
        else:
            int_end = idx
        p = int_end
        while p > 0 and line_text[p - 1].isdigit():
            p -= 1
        cur_p = p
        while cur_p > 1 and line_text[cur_p - 1] == "," and line_text[cur_p - 2].isdigit():
            next_sep = line_text.find(",", cur_p, int_end)
            grp_len = (next_sep - cur_p) if next_sep != -1 else (int_end - cur_p)
            if grp_len != 3:
                break
            q = cur_p - 1
            while q > 0 and line_text[q - 1].isdigit() and (cur_p - 1 - q) < 3:
                q -= 1
            if q == cur_p - 1 or (q > 0 and line_text[q - 1].isdigit()):
                break
            cur_p = q
        idx = cur_p
        while idx > 0 and line_text[idx - 1] in "(（ \t\r\n":
            idx -= 1
        if idx in left_memo:
            res = left_memo[idx]
            for v in visited:
                left_memo[v] = res
            return res
        visited.append(idx)

    c_start = _find_before_clause_start(line_text, idx)
    while idx > c_start and line_text[idx - 1] in "+-−*×/÷=＝≈()（） \t\r\n":
        idx -= 1
    tail_slice = line_text[max(c_start, idx - 16):idx]
    res = (
        bool(tail_slice and _LEFT_YI_TAIL_RE.search(tail_slice)),
        bool(tail_slice and _LEFT_WAN_TAIL_RE.search(tail_slice)),
    )
    for v in visited:
        left_memo[v] = res
    return res


def _collect_line_operands_index(
    line_text: str,
    *,
    evidence_strs: set[str],
    working_floats: list[float],
    derived_scaled_floats: list[float],
    float_index: _SortedFloatIndex | None = None,
) -> tuple[list[tuple[int, int, float]], dict[tuple[int, int], float]]:
    items: list[tuple[int, int, float]] = []
    spans: dict[tuple[int, int], float] = {}
    f_idx = float_index if float_index is not None else _SortedFloatIndex(working_floats)

    for lm in _NUM_RE.finditer(line_text):
        l_clean = lm.group(0).replace(",", "")
        try:
            lv = float(l_clean)
        except ValueError:
            continue
        if not math.isfinite(lv):
            continue
        after_raw = line_text[lm.end():lm.end() + 16]
        after_stop = _AFTER_STOP_SPLIT_RE.split(after_raw, maxsplit=1)[0]
        spaced_mult = _match_spaced_scale_mult(after_stop)
        if spaced_mult > 1.0:
            scaled_lv = lv * spaced_mult
            dec_p = len(l_clean.split(".")[1]) if "." in l_clean else 0
            u_step = 0.501 if dec_p == 0 else (0.0501 if dec_p == 1 else 0.0051)
            u_tol = max(u_step * spaced_mult, abs(scaled_lv) * 1e-3)
            is_grounded = (lv == 0.0 and (f_idx.has_zero or "0" in evidence_strs)) or f_idx.has_scaled_unit_match(
                scaled_lv, u_tol
            )
            if is_grounded:
                items.append((lm.start(), lm.end(), scaled_lv))
                spans[(lm.start(), lm.end())] = scaled_lv
        else:
            tol_scaled_l = max(0.0501, lv * 1e-3)
            is_grounded = (
                (l_clean in evidence_strs)
                or f_idx.has_line_direct_match(lv)
                or (
                    bool(derived_scaled_floats)
                    and any(abs(lv - ds) <= max(0.0501, abs(ds) * 1e-3) for ds in derived_scaled_floats)
                )
                or f_idx.has_abs_div_match(lv, 1e8, 1e6, tol_scaled_l)
                or f_idx.has_abs_div_match(lv, 1e4, 1e3, tol_scaled_l)
            )
            if is_grounded:
                items.append((lm.start(), lm.end(), lv))
                spans[(lm.start(), lm.end())] = lv

    for um in _UNIT_NUM_RE.finditer(line_text):
        l_clean = um.group(1).replace(",", "")
        u_suffix = um.group(2)
        if _is_calendar_period_label(line_text, um.start(), um.end(), um.group(1), u_suffix):
            continue
        u_mult = _UNIT_MULTIPLIERS.get(u_suffix.upper(), 1.0)
        try:
            lv = float(l_clean)
        except ValueError:
            continue
        if not math.isfinite(lv):
            continue
        if u_mult > 1.0:
            scaled_lv = lv * u_mult
            dec_p = len(l_clean.split(".")[1]) if "." in l_clean else 0
            u_step = 0.501 if dec_p == 0 else (0.0501 if dec_p == 1 else 0.0051)
            u_tol = max(u_step * u_mult, abs(scaled_lv) * 1e-3)
            is_grounded = (lv == 0.0 and (f_idx.has_zero or "0" in evidence_strs)) or f_idx.has_scaled_unit_match(
                scaled_lv, u_tol
            )
            if is_grounded:
                items.append((um.start(1), um.end(1), scaled_lv))
                spans[(um.start(1), um.end(1))] = scaled_lv
        else:
            is_grounded = (
                (l_clean in evidence_strs)
                or f_idx.has_line_direct_match(lv)
                or (
                    bool(derived_scaled_floats)
                    and any(abs(lv - ds) <= max(0.0501, abs(ds) * 1e-3) for ds in derived_scaled_floats)
                )
            )
            if is_grounded:
                items.append((um.start(1), um.end(1), lv))
                spans[(um.start(1), um.end(1))] = lv

    return items, spans


def _collect_line_grounded_operands(
    line_text: str,
    rel_start: int,
    rel_end: int,
    *,
    evidence_strs: set[str],
    working_floats: list[float],
    derived_scaled_floats: list[float],
) -> tuple[list[float], dict[tuple[int, int], float]]:
    items, _ = _collect_line_operands_index(
        line_text,
        evidence_strs=evidence_strs,
        working_floats=working_floats,
        derived_scaled_floats=derived_scaled_floats,
    )
    line_grounded_vals: list[float] = []
    grounded_spans: dict[tuple[int, int], float] = {}
    for s, e, v in items:
        if s < rel_end and e > rel_start:
            continue
        line_grounded_vals.append(v)
        grounded_spans[(s, e)] = v
    return line_grounded_vals, grounded_spans


def _extract_relevant_line_vals(
    items: list[tuple[int, int, float]],
    rel_start: int,
    rel_end: int,
    limit: int = 16,
) -> list[float]:
    vals: list[float] = []
    for s, e, v in items:
        if not (s < rel_end and e > rel_start):
            vals.append(v)
            if len(vals) >= limit:
                break
    return vals


def _build_line_eq_index(
    line_text: str,
) -> tuple[
    list[int],
    list[int],
    list[tuple[list[int], list[tuple[int, int]], list[tuple[int | None, int | None]]] | None],
]:
    clause_starts: list[int] = [0]
    clause_ends: list[int] = []
    for sm in _EQ_CLAUSE_SEP_RE.finditer(line_text):
        clause_ends.append(sm.start())
        clause_starts.append(sm.end())
    clause_ends.append(len(line_text))

    clause_metas: list[
        tuple[list[int], list[tuple[int, int]], list[tuple[int | None, int | None]]] | None
    ] = []
    for c_start, c_end in zip(clause_starts, clause_ends):
        clause = line_text[c_start:c_end]
        if "=" not in clause and "＝" not in clause and "≈" not in clause:
            clause_metas.append(None)
            continue
        eq_matches = list(_EQ_OP_RE.finditer(line_text, c_start, c_end))
        if not eq_matches:
            clause_metas.append(None)
            continue
        segments: list[tuple[int, int]] = []
        seg_starts: list[int] = []
        seg_bounds: list[tuple[int | None, int | None]] = []
        pos = c_start
        for em in eq_matches:
            segments.append((pos, em.start()))
            seg_starts.append(pos)
            pos = em.end()
        segments.append((pos, c_end))
        seg_starts.append(pos)
        for s_start, s_end in segments:
            seg_cleaned = line_text[s_start:s_end].replace("**", "  ")
            m_first = _EQ_SEG_FORBIDDEN_RE.search(seg_cleaned)
            if m_first is None:
                seg_bounds.append((None, None))
            else:
                first_op = s_start + m_first.start()
                m_last = _EQ_SEG_FORBIDDEN_LAST_RE.search(seg_cleaned, m_first.start())
                last_op = (s_start + m_last.start(1)) if m_last else first_op
                seg_bounds.append((first_op, last_op))
        clause_metas.append((seg_starts, segments, seg_bounds))
    return clause_starts, clause_ends, clause_metas


def _check_explicit_equation_sum(
    val: float,
    line_text: str,
    rel_start: int,
    rel_end: int,
    tol_calc: float,
    grounded_spans: dict[tuple[int, int], float],
    exclude_span: tuple[int, int] | None = None,
    eq_index: tuple[
        list[int],
        list[int],
        list[tuple[list[int], list[tuple[int, int]], list[tuple[int | None, int | None]]] | None],
    ]
    | None = None,
) -> bool:
    if "=" not in line_text and "＝" not in line_text and "≈" not in line_text:
        return False

    idx_data = eq_index if eq_index is not None else _build_line_eq_index(line_text)
    clause_starts, clause_ends, clause_metas = idx_data
    c_idx = bisect_right(clause_starts, rel_start) - 1
    if c_idx < 0 or rel_end > clause_ends[c_idx]:
        return False
    c_meta = clause_metas[c_idx]
    if c_meta is None:
        return False

    seg_starts, segments, seg_bounds = c_meta
    seg_idx = bisect_right(seg_starts, rel_start) - 1
    if seg_idx < 0:
        return False
    s_start, s_end = segments[seg_idx]
    if rel_start < s_start or rel_end > s_end:
        return False

    first_op, last_op = seg_bounds[seg_idx]
    if (first_op is not None and first_op < rel_start) or (last_op is not None and last_op >= rel_end):
        return False

    def _eval_chain(chain_str: str, chain_offset_in_line: int) -> bool:
        total = 0.0
        count = 0
        for sm in _SIGNED_NUM_RE.finditer(chain_str):
            sign = -1.0 if sm.group(1) in ("-", "−") else 1.0
            span = (chain_offset_in_line + sm.start(2), chain_offset_in_line + sm.end(2))
            if exclude_span is not None and span[0] < exclude_span[1] and span[1] > exclude_span[0]:
                return False
            if span not in grounded_spans:
                return False
            total += sign * grounded_spans[span]
            count += 1
        return count >= 2 and abs(val - total) <= tol_calc

    if seg_idx > 0:
        lhs_s, lhs_e = segments[seg_idx - 1]
        tail_start = max(lhs_s, lhs_e - 300)
        lhs_tail = line_text[tail_start:lhs_e]
        m_tail = _EQ_CHAIN_TAIL_RE.search(lhs_tail)
        if m_tail and _eval_chain(m_tail.group(1), tail_start + m_tail.start(1)):
            return True

    if seg_idx + 1 < len(segments):
        rhs_s, rhs_e = segments[seg_idx + 1]
        rhs_head = line_text[rhs_s:min(rhs_e, rhs_s + 300)]
        m_lead = _EQ_CHAIN_LEAD_RE.match(rhs_head)
        if m_lead and _eval_chain(m_lead.group(1), rhs_s + m_lead.start(1)):
            return True

    return False


def _extract_immediate_clause(line_text: str, rel_start: int, rel_end: int) -> str:
    cl_start = 0
    i = rel_start - 1
    n = len(line_text)
    while i >= 0:
        ch = line_text[i]
        if ch in "，。；;!?！？|：":
            cl_start = i + 1
            break
        if ch == ":" and not (i > 0 and i + 1 < n and line_text[i - 1].isdigit() and line_text[i + 1].isdigit()):
            cl_start = i + 1
            break
        if ch == ",":
            prev_digit = i > 0 and line_text[i - 1].isdigit()
            next_3digits = (
                i + 3 < rel_start
                and line_text[i + 1:i + 4].isdigit()
                and (i + 4 >= n or not line_text[i + 4].isdigit())
            )
            if not (prev_digit and next_3digits):
                cl_start = i + 1
                break
        i -= 1

    cl_end = n
    j = rel_end
    while j < n:
        ch = line_text[j]
        if ch in "，。；;!?！？|：":
            cl_end = j
            break
        if ch == ":" and not (j > 0 and j + 1 < n and line_text[j - 1].isdigit() and line_text[j + 1].isdigit()):
            cl_end = j
            break
        if ch == ",":
            prev_digit = j > 0 and line_text[j - 1].isdigit()
            next_3digits = (
                j + 3 < n
                and line_text[j + 1:j + 4].isdigit()
                and (j + 4 >= n or not line_text[j + 4].isdigit())
            )
            if not (prev_digit and next_3digits):
                cl_end = j
                break
        j += 1

    return line_text[cl_start:cl_end]


def _build_immediate_clause_index(line_text: str) -> tuple[list[int], list[int], dict[int, bool]]:
    starts: list[int] = [0]
    ends: list[int] = []
    n = len(line_text)
    for k, ch in enumerate(line_text):
        if ch in "，。；;!?！？|：":
            ends.append(k)
            starts.append(k + 1)
        elif ch == ":":
            if not (k > 0 and k + 1 < n and line_text[k - 1].isdigit() and line_text[k + 1].isdigit()):
                ends.append(k)
                starts.append(k + 1)
        elif ch == ",":
            prev_digit = k > 0 and line_text[k - 1].isdigit()
            next_3digits = (
                k + 3 < n
                and line_text[k + 1:k + 4].isdigit()
                and (k + 4 >= n or not line_text[k + 4].isdigit())
            )
            if not (prev_digit and next_3digits):
                ends.append(k)
                starts.append(k + 1)
    ends.append(n)
    return starts, ends, {}


def _has_clause_arith_eq(
    line_text: str,
    rel_start: int,
    rel_end: int,
    im_clause_index: tuple[list[int], list[int], dict[int, bool]] | None = None,
) -> bool:
    if "=" not in line_text and "＝" not in line_text and "≈" not in line_text:
        return False
    if im_clause_index is not None:
        starts, ends, memo = im_clause_index
        idx = bisect_right(starts, rel_start) - 1
        if idx < 0 or rel_end > ends[idx]:
            return False
        if idx in memo:
            return memo[idx]
        clause = line_text[starts[idx]:ends[idx]]
        if "=" not in clause and "＝" not in clause and "≈" not in clause:
            memo[idx] = False
            return False
        sanitized = _URL_RE.sub(" ", clause)
        sanitized = _T_PLUS_INLINE_RE.sub("T_RULE", sanitized)
        sanitized = _DATE_SEP_RE.sub(" DATE ", sanitized)
        sanitized = sanitized.replace("**", " ").replace("__", " ").replace("`", " ")
        res = bool(_CLAUSE_ARITH_OP_RE.search(sanitized))
        memo[idx] = res
        return res
    clause = _extract_immediate_clause(line_text, rel_start, rel_end)
    if "=" not in clause and "＝" not in clause and "≈" not in clause:
        return False
    sanitized = _URL_RE.sub(" ", clause)
    sanitized = _T_PLUS_INLINE_RE.sub("T_RULE", sanitized)
    sanitized = _DATE_SEP_RE.sub(" DATE ", sanitized)
    sanitized = sanitized.replace("**", " ").replace("__", " ").replace("`", " ")
    return bool(_CLAUSE_ARITH_OP_RE.search(sanitized))


def _build_line_arith_cands(vals_2op: tuple[float, ...], small_line: tuple[float, ...]) -> list[float]:
    cands: list[float] = []
    for i, a in enumerate(vals_2op):
        for b in vals_2op[i + 1:]:
            raw = [a + b, abs(a - b), a * b]
            if b != 0:
                raw.extend([a / b, (a / b) * 100.0, (abs(a - b) / abs(b)) * 100.0])
            if a != 0:
                raw.extend([b / a, (b / a) * 100.0, (abs(a - b) / abs(a)) * 100.0])
            if a > 0 and b > 0:
                raw.append(math.sqrt(a * b))
            for c in raw:
                if math.isfinite(c):
                    cands.append(c)
    if len(small_line) >= 3:
        n_sl = len(small_line)
        for i in range(n_sl):
            a = small_line[i]
            for j in range(i + 1, n_sl):
                b = small_line[j]
                for k in range(j + 1, n_sl):
                    c = a + b + small_line[k]
                    if math.isfinite(c):
                        cands.append(c)
                for k in range(n_sl):
                    if k == i or k == j:
                        continue
                    c = a + b - small_line[k]
                    if math.isfinite(c):
                        cands.append(c)
            for j in range(n_sl):
                if j == i:
                    continue
                b = small_line[j]
                for k in range(j + 1, n_sl):
                    if k == i:
                        continue
                    c = a - b - small_line[k]
                    if math.isfinite(c):
                        cands.append(c)
    cands.sort()
    return cands


def _matches_line_arithmetic(
    val: float,
    line_text: str,
    rel_start: int,
    rel_end: int,
    line_grounded_vals: list[float],
    grounded_spans: dict[tuple[int, int], float],
    exclude_span: tuple[int, int] | None = None,
    arith_cands_cache: dict[tuple[tuple[float, ...], tuple[float, ...]], list[float]] | None = None,
    eq_index: tuple[
        list[int],
        list[int],
        list[tuple[list[int], list[tuple[int, int]], list[tuple[int | None, int | None]]] | None],
    ]
    | None = None,
) -> bool:
    if len(line_grounded_vals) < 2:
        return False
    tol_calc = max(0.15, abs(val) * 0.005)
    vals_2op = tuple(line_grounded_vals[:16])
    small_line = tuple(line_grounded_vals[:10]) if len(line_grounded_vals) >= 3 else ()
    if arith_cands_cache is not None:
        ck = (vals_2op, small_line)
        sorted_cands = arith_cands_cache.get(ck)
        if sorted_cands is None:
            sorted_cands = _build_line_arith_cands(vals_2op, small_line)
            arith_cands_cache[ck] = sorted_cands
    else:
        sorted_cands = _build_line_arith_cands(vals_2op, small_line)

    lo = bisect_left(sorted_cands, val - tol_calc - 1e-12)
    n_cands = len(sorted_cands)
    upper = val + tol_calc + 1e-12
    while lo < n_cands:
        c = sorted_cands[lo]
        if c > upper:
            break
        if abs(val - c) <= tol_calc:
            return True
        lo += 1

    return _check_explicit_equation_sum(
        val,
        line_text,
        rel_start,
        rel_end,
        tol_calc,
        grounded_spans,
        exclude_span=exclude_span,
        eq_index=eq_index,
    )


def _has_eq_left(line_text: str, rel_start: int) -> bool:
    i = rel_start - 1
    while i >= 0 and line_text[i].isspace():
        i -= 1
    return i >= 0 and line_text[i] in "=＝≈"


def _has_t_plus_left(line_text: str, rel_start: int) -> bool:
    i = rel_start - 1
    while i >= 0 and line_text[i].isspace():
        i -= 1
    if i < 0 or line_text[i] != "+":
        return False
    i -= 1
    while i >= 0 and line_text[i].isspace():
        i -= 1
    if i < 0 or line_text[i] not in ("T", "t"):
        return False
    if i == 0:
        return True
    p = line_text[i - 1]
    return not ("A" <= p <= "Z" or "a" <= p <= "z" or "0" <= p <= "9" or p == "_")


def _classify_number_grounding(
    raw_num: str,
    *,
    answer: str,
    match_start: int,
    match_end: int,
    evidence_strs: set[str],
    working_floats: list[float],
    derived_scaled_floats: list[float],
    allow_harmless_int: bool = True,
    unit_suffix: str = "",
    url_spans: list[tuple[int, int]] | None = None,
    line_operands_cache: dict[tuple[int, int], tuple[list[tuple[int, int, float]], dict[tuple[int, int], float]]] | None = None,
    line_grounded_updates: dict[tuple[int, int], list[tuple[int, int, float]]] | None = None,
    right_arith_memo: dict[tuple[int, int], dict[int, str]] | None = None,
    left_arith_memo: dict[tuple[int, int], dict[int, tuple[bool, bool]]] | None = None,
    arith_cands_cache: dict[tuple[tuple[float, ...], tuple[float, ...]], list[float]] | None = None,
    float_index: _SortedFloatIndex | None = None,
    eq_line_cache: dict[tuple[int, int], Any] | None = None,
    im_clause_cache: dict[tuple[int, int], tuple[list[int], list[int], dict[int, bool]]] | None = None,
    table_header_cache: dict[int, tuple[list[str], list[str]]] | None = None,
    pipe_pos_cache: dict[int, list[int]] | None = None,
    line_info: tuple[int, int, str, int, bool] | None = None,
) -> tuple[str, float | None]:
    """Return (status, parsed_float) where status is 'trivial', 'direct', 'derived', or 'ungrounded'."""
    clean = raw_num.replace(",", "")

    if line_info is not None:
        line_start, line_end, line_text, list_item_end, is_table_row = line_info
        rel_start = match_start - line_start
        rel_end = match_end - line_start
        if list_item_end != -1 and rel_end == list_item_end:
            return "trivial", None
    else:
        # Ignore markdown numbered list items / headings like "1. " or "2、" at line start
        line_start = answer.rfind("\n", 0, match_start) + 1
        line_end = answer.find("\n", match_end)
        if line_end == -1:
            line_end = len(answer)
        line_text = answer[line_start:line_end]
        rel_start = match_start - line_start
        rel_end = match_end - line_start
        m_li = _LIST_ITEM_NUM_RE.match(line_text)
        if m_li is not None and rel_start == m_li.start(1) and rel_end == m_li.end(1):
            return "trivial", None
        is_table_row = bool(line_text and line_text[0] in "| \t\r" and line_text.lstrip().startswith("|"))

    # Ignore numbers embedded inside URLs
    if _is_inside_url(answer, match_start, url_spans=url_spans):
        return "trivial", None

    try:
        val = float(clean)
    except ValueError:
        return "trivial", None
    if not math.isfinite(val):
        return "trivial", None

    # Check if an explicit English scale suffix (e.g. "billion", "bn", "million", "trillion", "thousand")
    # is attached immediately after this number; if so, require the scaled value rather than short-circuiting
    # on an unscaled coincidence like pb=1.23 vs "1.23 billion".
    after_raw = answer[match_end:min(line_end, match_end + 16)]
    after_stop = _AFTER_STOP_SPLIT_RE.split(after_raw, maxsplit=1)[0]
    attached_mult = _match_spaced_scale_mult(after_stop) if not unit_suffix else 1.0

    is_decimal = "." in clean
    dec_places = len(clean.split(".")[1]) if is_decimal else 0
    f_idx = float_index if float_index is not None else _SortedFloatIndex(working_floats)

    def _get_line_operands() -> tuple[list[float], dict[tuple[int, int], float]]:
        if line_operands_cache is not None:
            ck = (line_start, line_end)
            cached = line_operands_cache.get(ck)
            if cached is None:
                items, spans = _collect_line_operands_index(
                    line_text,
                    evidence_strs=evidence_strs,
                    working_floats=working_floats,
                    derived_scaled_floats=derived_scaled_floats,
                    float_index=f_idx,
                )
                if line_grounded_updates:
                    for rs, re_pos, pv in line_grounded_updates.get(ck, ()):
                        if (rs, re_pos) not in spans:
                            spans[(rs, re_pos)] = pv
                            items.append((rs, re_pos, pv))
                cached = (items, spans)
                line_operands_cache[ck] = cached
            items, spans = cached
            vals = _extract_relevant_line_vals(items, rel_start, rel_end)
            return vals, spans
        return _collect_line_grounded_operands(
            line_text,
            rel_start,
            rel_end,
            evidence_strs=evidence_strs,
            working_floats=working_floats,
            derived_scaled_floats=derived_scaled_floats,
        )

    def _get_eq_index() -> Any:
        if eq_line_cache is None:
            return None
        ck = (line_start, line_end)
        cached_eq = eq_line_cache.get(ck)
        if cached_eq is None and ("=" in line_text or "＝" in line_text or "≈" in line_text):
            cached_eq = _build_line_eq_index(line_text)
            eq_line_cache[ck] = cached_eq
        return cached_eq

    if attached_mult > 1.0:
        scaled_val = val * attached_mult
        if val == 0.0:
            return ("direct", 0.0) if (f_idx.has_zero or "0" in evidence_strs) else ("ungrounded", 0.0)
        unit_step = 0.501 if dec_places == 0 else (0.0501 if dec_places == 1 else 0.0051)
        tol_scaled = max(unit_step * attached_mult, abs(scaled_val) * 1e-3)
        if f_idx.has_scaled_unit_match(scaled_val, tol_scaled):
            return "direct", scaled_val
        line_grounded_vals, grounded_spans = _get_line_operands()
        if _matches_line_arithmetic(
            scaled_val,
            line_text,
            rel_start,
            rel_end,
            line_grounded_vals,
            grounded_spans,
            exclude_span=(rel_start, rel_end),
            arith_cands_cache=arith_cands_cache,
            eq_index=_get_eq_index(),
        ):
            return "derived", scaled_val
        return "ungrounded", scaled_val

    if clean in evidence_strs:
        return "direct", val

    # Markdown table column header & local unit/equation context
    col_header = (
        _extract_table_col_header(
            answer,
            line_start,
            line_text,
            rel_start,
            table_header_cache=table_header_cache,
            pipe_pos_cache=pipe_pos_cache,
        )
        if is_table_row
        else ""
    )
    has_explicit_yuan = bool(_EXPLICIT_YUAN_RE.match(after_stop))

    line_key = (line_start, line_end)
    if not has_explicit_yuan:
        r_memo = right_arith_memo.setdefault(line_key, {}) if right_arith_memo is not None else {}
        right_unit = _find_right_arith_unit(line_text, rel_end, r_memo)
        l_memo = left_arith_memo.setdefault(line_key, {}) if left_arith_memo is not None else {}
        has_left_yi, has_left_wan = _has_left_arith_units(line_text, rel_start, l_memo)
    else:
        right_unit = ""
        has_left_yi, has_left_wan = False, False

    has_yi = (
        (not has_explicit_yuan and "亿" in after_stop)
        or (right_unit == "亿")
        or has_left_yi
        or ("亿" in col_header)
    )
    has_wan = (
        (not has_explicit_yuan and "万" in after_stop)
        or (right_unit == "万")
        or has_left_wan
        or ("万" in col_header)
    )
    if col_header:
        has_t = bool(_HAS_T_HDR_RE.search(col_header))
        has_b = bool(_HAS_B_HDR_RE.search(col_header))
        has_m = bool(_HAS_M_HDR_RE.search(col_header))
        has_k = bool(_HAS_K_HDR_RE.search(col_header))
    else:
        has_t = has_b = has_m = has_k = False

    if not is_decimal:
        iv = int(val)
        if allow_harmless_int and (iv in _HARMLESS_INTEGERS or 1990 <= iv <= 2035):
            is_t_plus_marker = (
                iv in (0, 1)
                and _has_t_plus_left(line_text, rel_start)
                and not _MONETARY_AFTER_RE.match(after_stop)
                and not after_stop.lstrip().startswith(("%", "％"))
            )
            if not is_t_plus_marker:
                has_clause_eq = False
                if "=" in line_text or "＝" in line_text or "≈" in line_text:
                    if im_clause_cache is not None:
                        im_idx = im_clause_cache.get(line_key)
                        if im_idx is None:
                            im_idx = _build_immediate_clause_index(line_text)
                            im_clause_cache[line_key] = im_idx
                        has_clause_eq = _has_clause_arith_eq(line_text, rel_start, rel_end, im_clause_index=im_idx)
                    else:
                        has_clause_eq = _has_clause_arith_eq(line_text, rel_start, rel_end)
                has_monetary_or_eq_context = (
                    bool(_MONETARY_AFTER_RE.match(after_stop))
                    or attached_mult > 1.0
                    or has_yi
                    or has_wan
                    or has_b
                    or has_m
                    or has_k
                    or has_t
                    or _has_eq_left(line_text, rel_start)
                    or bool(_EQ_RIGHT_BOUND_AT_RE.match(line_text, rel_end))
                    or has_clause_eq
                    or bool(col_header and _MONETARY_COL_HEADER_RE.search(col_header))
                )
            else:
                has_monetary_or_eq_context = False
            if not has_monetary_or_eq_context:
                return "trivial", val

    # Direct float match within rounding tolerance
    # (e.g., 194.85 rounded to 1 decimal place -> 194.9 has max rounding delta 0.05)
    if f_idx.has_direct_match(val, is_decimal=is_decimal, dec_places=dec_places):
        return "direct", val

    # Scaled forms of previously verified derived calculations (e.g. 12190352881.36 -> 121.90)
    if derived_scaled_floats:
        for ds in derived_scaled_floats:
            if abs(val - ds) <= max(0.0501, abs(ds) * 1e-3):
                return "derived", val

    # Percentage scaling & derived multi-day % change match:
    # e.g. evidence has 0.224 and answer states "22.4%", "22.4 %", "22.4pct", "22.4percent", or table cell under "| ROE(%) |"
    after_slice = answer[match_end:match_end + 12]
    if (
        unit_suffix.lower() in ("pct", "percent")
        or after_slice.lstrip().lower().startswith(("%", "％", "个百分点", "pct", "percent"))
        or "%" in col_header
        or "％" in col_header
    ):
        if (f_idx.has_zero and abs(val) <= 0.15) or f_idx.has_abs_mul_match(
            val, 100.0, 0.0, float("inf"), 0.15
        ):
            return "direct", val
        if 0.0 < val < 100.0:
            small_pcts = f_idx.small_pcts
            for i, p1 in enumerate(small_pcts):
                for p2 in small_pcts[i + 1:]:
                    cands = (
                        p1 + p2,
                        abs(p1 - p2),
                        abs(((1.0 + p1 / 100.0) * (1.0 + p2 / 100.0) - 1.0) * 100.0),
                        abs(((1.0 - p1 / 100.0) * (1.0 - p2 / 100.0) - 1.0) * 100.0),
                    )
                    if any(abs(val - c) <= 0.06 for c in cands):
                        return "derived", val

    if has_yi:
        tol_yi = max(0.0501, val * 1e-3)
        if f_idx.has_abs_div_match(val, 1e8, 1e6, tol_yi):
            return "direct", val
        if val < 10.0 and f_idx.has_abs_div_match(val, 1e4, 10.0, 0.015):
            return "direct", val
    if has_t:
        if f_idx.has_abs_div_match(val, 1e12, 1e9, max(0.0501, val * 1e-3)):
            return "direct", val
    if has_b:
        if f_idx.has_abs_div_match(val, 1e9, 1e6, max(0.0501, val * 1e-3)):
            return "direct", val
    if has_m:
        if f_idx.has_abs_div_match(val, 1e6, 1e4, max(0.0501, val * 1e-3)):
            return "direct", val
    if has_k:
        if f_idx.has_abs_div_match(val, 1e3, 1e2, max(0.0501, val * 1e-3)):
            return "direct", val
    if has_wan:
        tol_wan_div = max(0.0501, val * 1e-3)
        if f_idx.has_abs_div_match(val, 1e4, 1e3, tol_wan_div):
            return "direct", val
        if f_idx.has_abs_mul_match(val, 10000.0, 0.001, 10000.0, max(1.0, val * 1e-3)):
            return "direct", val

    # Approximate derived calculation marked with ≈ or 约 (e.g., prev_close * (1 + pct/100) ≈ 1632.4)
    approx_left = answer[max(0, match_start - 4):match_start]
    if "≈" in approx_left or "约" in approx_left:
        pct_cands = f_idx.pct_cands
        base_cands = f_idx.base_cands
        for b in base_cands:
            for p in pct_cands:
                if abs(val - b * (1.0 + p / 100.0)) <= 0.15 or abs(val - b * (1.0 - p / 100.0)) <= 0.15:
                    return "derived", val

    # Line-local arithmetic CALCULATION check
    line_grounded_vals, grounded_spans = _get_line_operands()
    if _matches_line_arithmetic(
        val,
        line_text,
        rel_start,
        rel_end,
        line_grounded_vals,
        grounded_spans,
        exclude_span=(rel_start, rel_end),
        arith_cands_cache=arith_cands_cache,
        eq_index=_get_eq_index(),
    ):
        return "derived", val

    # Reverse unit scaling: e.g. 194.85 (亿元) -> 19,485,000,000
    if val >= 1e7 and f_idx.has_abs_mul_inclusive_match(val, 1e8, 0.01, 100000.0, val * 1e-3):
        return "derived", val

    # Net PnL difference or capital*(1+return) check:
    cap_cands = f_idx.cap_cands
    if cap_cands:
        ret_cands = f_idx.ret_cands
        for cap in cap_cands:
            if f_idx.has_cap_diff_match(val, cap, 0.15):
                return "derived", val
            for r in ret_cands:
                if abs(val - cap * (1.0 + r)) <= 1.0:
                    return "derived", val

    return "ungrounded", val


def _classify_unit_number_grounding(
    raw_num: str,
    mult: float,
    *,
    answer: str,
    match_start: int,
    match_end: int,
    evidence_strs: set[str],
    working_floats: list[float],
    derived_scaled_floats: list[float],
    unit_suffix: str = "",
    url_spans: list[tuple[int, int]] | None = None,
    line_operands_cache: dict[tuple[int, int], tuple[list[tuple[int, int, float]], dict[tuple[int, int], float]]] | None = None,
    line_grounded_updates: dict[tuple[int, int], list[tuple[int, int, float]]] | None = None,
    arith_cands_cache: dict[tuple[tuple[float, ...], tuple[float, ...]], list[float]] | None = None,
    float_index: _SortedFloatIndex | None = None,
    eq_line_cache: dict[tuple[int, int], Any] | None = None,
    table_header_cache: dict[int, tuple[list[str], list[str]]] | None = None,
    pipe_pos_cache: dict[int, list[int]] | None = None,
    line_info: tuple[int, int, str, int, bool] | None = None,
) -> tuple[str, float | None]:
    """Classify ASCII unit-suffixed numbers with multiplier > 1.0 (T/B/M/K/tn/bn/mn/trillion/billion/million/thousand)."""
    if _is_inside_url(answer, match_start, url_spans=url_spans):
        return "trivial", None
    if _is_calendar_period_label(
        answer,
        match_start,
        match_end,
        raw_num,
        unit_suffix,
        table_header_cache=table_header_cache,
        pipe_pos_cache=pipe_pos_cache,
    ):
        return "trivial", None

    clean = raw_num.replace(",", "")
    try:
        val = float(clean)
    except ValueError:
        return "trivial", None
    if not math.isfinite(val):
        return "trivial", None

    f_idx = float_index if float_index is not None else _SortedFloatIndex(working_floats)
    scaled_val = val * mult
    if val == 0.0:
        return ("direct", 0.0) if (f_idx.has_zero or "0" in evidence_strs) else ("ungrounded", 0.0)

    dec_places = len(clean.split(".")[1]) if "." in clean else 0
    unit_step = 0.501 if dec_places == 0 else (0.0501 if dec_places == 1 else 0.0051)
    tol = max(unit_step * mult, abs(scaled_val) * 1e-3)
    if f_idx.has_scaled_unit_match(scaled_val, tol):
        return "direct", scaled_val

    if line_info is not None:
        line_start, line_end, line_text, _, _ = line_info
    else:
        line_start = answer.rfind("\n", 0, match_start) + 1
        line_end = answer.find("\n", match_end)
        if line_end == -1:
            line_end = len(answer)
        line_text = answer[line_start:line_end]
    rel_start = match_start - line_start
    rel_end = match_end - line_start
    if line_operands_cache is not None:
        ck = (line_start, line_end)
        cached = line_operands_cache.get(ck)
        if cached is None:
            items, spans = _collect_line_operands_index(
                line_text,
                evidence_strs=evidence_strs,
                working_floats=working_floats,
                derived_scaled_floats=derived_scaled_floats,
                float_index=f_idx,
            )
            if line_grounded_updates:
                for rs, re_pos, pv in line_grounded_updates.get(ck, ()):
                    if (rs, re_pos) not in spans:
                        spans[(rs, re_pos)] = pv
                        items.append((rs, re_pos, pv))
            cached = (items, spans)
            line_operands_cache[ck] = cached
        items, grounded_spans = cached
        line_grounded_vals = _extract_relevant_line_vals(items, rel_start, rel_end)
    else:
        line_grounded_vals, grounded_spans = _collect_line_grounded_operands(
            line_text,
            rel_start,
            rel_end,
            evidence_strs=evidence_strs,
            working_floats=working_floats,
            derived_scaled_floats=derived_scaled_floats,
        )
    cached_eq = None
    if eq_line_cache is not None and ("=" in line_text or "＝" in line_text or "≈" in line_text):
        ck = (line_start, line_end)
        cached_eq = eq_line_cache.get(ck)
        if cached_eq is None:
            cached_eq = _build_line_eq_index(line_text)
            eq_line_cache[ck] = cached_eq
    if _matches_line_arithmetic(
        scaled_val,
        line_text,
        rel_start,
        rel_end,
        line_grounded_vals,
        grounded_spans,
        exclude_span=(rel_start, rel_end),
        arith_cands_cache=arith_cands_cache,
        eq_index=cached_eq,
    ):
        return "derived", scaled_val

    return "ungrounded", scaled_val


def verify_answer_grounding(
    answer: str,
    tool_calls: list[dict[str, Any]],
    sources: list[str] | None = None,
    *,
    raw_tool_outputs: list[str] | None = None,
) -> dict[str, Any]:
    """Verify that answer figures and cited sources are grounded in tool call outputs."""
    ans = str(answer or "").strip()
    calls = list(tool_calls or [])
    src_list = [str(s).strip() for s in (sources or []) if str(s).strip()]

    non_error_calls: list[dict[str, Any]] = []
    corpus_parts: list[str] = []
    for tc in calls:
        if not isinstance(tc, dict):
            continue
        summary_str = str(tc.get("result_summary") or "")
        raw_res_str = str(tc.get("raw_result") or "")
        if (
            tc.get("is_error") is True
            or summary_str.lstrip().startswith("Error executing tool")
            or raw_res_str.lstrip().startswith("Error executing tool")
        ):
            continue
        non_error_calls.append(tc)
        corpus_parts.append(str(tc.get("name") or ""))
        corpus_parts.append(json.dumps(tc.get("args") or {}, ensure_ascii=False, default=str))
        corpus_parts.append(summary_str)
        if raw_res_str:
            corpus_parts.append(raw_res_str)
    for raw in raw_tool_outputs or []:
        raw_str = str(raw)
        if raw_str.lstrip().startswith("Error executing tool"):
            continue
        corpus_parts.append(raw_str)
    for s in src_list:
        corpus_parts.append(s)

    corpus = "\n".join(corpus_parts)
    evidence_strs, evidence_floats = _build_evidence_numbers(corpus)
    working_floats: list[float] = list(evidence_floats)
    working_floats_set: set[float] = set(working_floats)
    float_index = _SortedFloatIndex(working_floats)
    derived_scaled_floats: list[float] = []
    url_spans = [(m.start(), m.end()) for m in _URL_RE.finditer(ans)]
    line_operands_cache: dict[
        tuple[int, int],
        tuple[list[tuple[int, int, float]], dict[tuple[int, int], float]],
    ] = {}
    line_grounded_updates: dict[tuple[int, int], list[tuple[int, int, float]]] = {}
    right_arith_memo: dict[tuple[int, int], dict[int, str]] = {}
    left_arith_memo: dict[tuple[int, int], dict[int, tuple[bool, bool]]] = {}
    arith_cands_cache: dict[tuple[tuple[float, ...], tuple[float, ...]], list[float]] = {}
    eq_line_cache: dict[tuple[int, int], Any] = {}
    im_clause_cache: dict[tuple[int, int], tuple[list[int], list[int], dict[int, bool]]] = {}
    table_header_cache: dict[int, tuple[list[str], list[str]]] = {}
    pipe_pos_cache: dict[int, list[int]] = {}

    tokens: list[tuple[int, int, bool, float, str, str, str]] = []
    for match in _NUM_RE.finditer(ans):
        tokens.append((match.start(), match.end(), False, 1.0, match.group(0), match.group(0), ""))
    for match in _UNIT_NUM_RE.finditer(ans):
        suffix = match.group(2)
        mult = _UNIT_MULTIPLIERS.get(suffix.upper(), 1.0)
        tokens.append((match.start(), match.end(), True, mult, match.group(1), match.group(0), suffix))
    tokens.sort(key=lambda item: (item[0], item[1]))

    ungrounded_numbers: list[str] = []
    seen_ungrounded: set[str] = set()
    derived_grounded: dict[tuple[str, float, str], float | None] = {}

    cur_line_start = -1
    cur_line_end = -1
    cur_line_info: tuple[int, int, str, int, bool] | None = None

    def _record_line_grounded(
        m_start: int, line_start: int, line_end: int, num_len: int, parsed_val: float | None
    ) -> None:
        if parsed_val is None or not math.isfinite(parsed_val):
            return
        ck = (line_start, line_end)
        rel_start = m_start - line_start
        rel_num_end = rel_start + num_len
        cached = line_operands_cache.get(ck)
        if cached is not None:
            items, spans = cached
            if (rel_start, rel_num_end) not in spans:
                spans[(rel_start, rel_num_end)] = parsed_val
                items.append((rel_start, rel_num_end, parsed_val))
        else:
            line_grounded_updates.setdefault(ck, []).append((rel_start, rel_num_end, parsed_val))

    for m_start, m_end, is_unit, mult, num_tok, display_tok, unit_suffix in tokens:
        if m_start > cur_line_end:
            cur_line_start = ans.rfind("\n", 0, m_start) + 1
            cur_line_end = ans.find("\n", m_end)
            if cur_line_end == -1:
                cur_line_end = len(ans)
            cur_line_text = ans[cur_line_start:cur_line_end]
            m_li = _LIST_ITEM_NUM_RE.match(cur_line_text)
            cur_list_end = m_li.end(1) if m_li is not None else -1
            cur_is_table_row = bool(
                cur_line_text and cur_line_text[0] in "| \t\r" and cur_line_text.lstrip().startswith("|")
            )
            cur_line_info = (cur_line_start, cur_line_end, cur_line_text, cur_list_end, cur_is_table_row)

        clean_tok = num_tok.replace(",", "")
        if is_unit:
            unit_tag = unit_suffix.upper()
        else:
            after_tok = ans[m_end:min(len(ans), m_end + 16)]
            after_tok_stop = _AFTER_TOK_SPLIT_RE.split(after_tok, maxsplit=1)[0]
            spaced_m = _match_spaced_scale_mult(after_tok_stop)
            if spaced_m > 1.0:
                unit_tag = f"SP:{spaced_m}"
            elif _EXPLICIT_YUAN_RE.match(after_tok_stop):
                unit_tag = "元"
            elif "亿" in after_tok_stop:
                unit_tag = "亿"
            elif "万" in after_tok_stop:
                unit_tag = "万"
            elif after_tok_stop.lstrip().lower().startswith(("%", "％", "个百分点", "pct", "percent")):
                unit_tag = "%"
            else:
                unit_tag = ""
        key = (clean_tok, mult, unit_tag)
        if key in derived_grounded:
            _record_line_grounded(m_start, cur_line_start, cur_line_end, len(num_tok), derived_grounded[key])
            continue
        if is_unit and mult > 1.0:
            status, parsed_val = _classify_unit_number_grounding(
                num_tok,
                mult,
                answer=ans,
                match_start=m_start,
                match_end=m_end,
                evidence_strs=evidence_strs,
                working_floats=working_floats,
                derived_scaled_floats=derived_scaled_floats,
                unit_suffix=unit_suffix,
                url_spans=url_spans,
                line_operands_cache=line_operands_cache,
                line_grounded_updates=line_grounded_updates,
                arith_cands_cache=arith_cands_cache,
                float_index=float_index,
                eq_line_cache=eq_line_cache,
                table_header_cache=table_header_cache,
                pipe_pos_cache=pipe_pos_cache,
                line_info=cur_line_info,
            )
        else:
            status, parsed_val = _classify_number_grounding(
                num_tok,
                answer=ans,
                match_start=m_start,
                match_end=m_end,
                evidence_strs=evidence_strs,
                working_floats=working_floats,
                derived_scaled_floats=derived_scaled_floats,
                allow_harmless_int=not is_unit,
                unit_suffix=unit_suffix,
                url_spans=url_spans,
                line_operands_cache=line_operands_cache,
                line_grounded_updates=line_grounded_updates,
                right_arith_memo=right_arith_memo,
                left_arith_memo=left_arith_memo,
                arith_cands_cache=arith_cands_cache,
                float_index=float_index,
                eq_line_cache=eq_line_cache,
                im_clause_cache=im_clause_cache,
                table_header_cache=table_header_cache,
                pipe_pos_cache=pipe_pos_cache,
                line_info=cur_line_info,
            )
        if status != "ungrounded":
            if status in ("direct", "derived"):
                _record_line_grounded(m_start, cur_line_start, cur_line_end, len(num_tok), parsed_val)
                if status == "derived":
                    derived_grounded[key] = parsed_val
                    if parsed_val is not None and math.isfinite(parsed_val):
                        if parsed_val not in working_floats_set:
                            working_floats_set.add(parsed_val)
                            working_floats.append(parsed_val)
                            float_index.add(parsed_val)
                        if abs(parsed_val) >= 1e6:
                            derived_scaled_floats.append(parsed_val / 1e8)
                        if abs(parsed_val) >= 1e3:
                            derived_scaled_floats.append(parsed_val / 1e4)
        else:
            if display_tok not in seen_ungrounded:
                seen_ungrounded.add(display_tok)
                ungrounded_numbers.append(display_tok)

    # Exact URL set membership against corpus URLs + cited sources (no substring/prefix spoofing)
    corpus_urls = {u.rstrip(".,;:!?`*/") for u in _URL_RE.findall(corpus)}
    norm_sources = {s.rstrip(".,;:!?`*/") for s in src_list if s.startswith(("http://", "https://"))}
    allowed_urls = corpus_urls | norm_sources

    cited_urls = _URL_RE.findall(ans)
    unverified_urls: list[str] = []
    seen_unverified_urls: set[str] = set()
    for url in cited_urls:
        clean_u = url.rstrip(".,;:!?`*")
        norm_u = clean_u.rstrip("/")
        is_allowed = (
            norm_u in allowed_urls
            or clean_u in allowed_urls
            or (norm_u.startswith("https://") and ("http://" + norm_u[8:]) in allowed_urls)
        )
        if not is_allowed:
            if clean_u not in seen_unverified_urls:
                seen_unverified_urls.add(clean_u)
                unverified_urls.append(clean_u)

    has_answer = bool(ans)
    has_tools = len(non_error_calls) > 0
    has_sources = len(src_list) > 0
    numeric_ok = len(ungrounded_numbers) == 0
    urls_ok = len(unverified_urls) == 0

    warnings: list[str] = []
    unverified_claims: list[str] = []

    if not has_answer:
        unverified_claims.append("Empty answer produced.")
    if not has_tools:
        unverified_claims.append("No MCP tool calls were recorded to ground the response.")
    if not has_sources:
        unverified_claims.append("No data sources or reference URLs were cited.")
    if ungrounded_numbers:
        unverified_claims.append(
            f"Ungrounded numeric figures not found in tool outputs: {', '.join(ungrounded_numbers[:10])}"
        )
    if unverified_urls:
        msg = f"Unverified cited URLs not found in tool outputs: {', '.join(unverified_urls[:10])}"
        unverified_claims.append(msg)
        warnings.append(f"URLs cited in answer not directly seen in tool outputs: {', '.join(unverified_urls)}")

    grounded = has_answer and has_tools and has_sources and numeric_ok and urls_ok

    checks = [
        {"name": "non_empty_answer", "passed": has_answer},
        {"name": "tool_calls_present", "passed": has_tools, "count": len(non_error_calls)},
        {"name": "sources_cited", "passed": has_sources, "count": len(src_list)},
        {
            "name": "numeric_grounding",
            "passed": numeric_ok,
            "ungrounded_count": len(ungrounded_numbers),
        },
        {
            "name": "url_grounding",
            "passed": urls_ok,
            "unverified_count": len(unverified_urls),
        },
    ]

    return {
        "verified": grounded,
        "grounded": grounded,
        "passed": grounded,
        "checks": checks,
        "verified_sources": list(src_list),
        "sources": list(src_list),
        "ungrounded_numbers": ungrounded_numbers,
        "unverified_urls": unverified_urls,
        "unverified_claims": unverified_claims,
        "warnings": warnings,
    }


verify_answer = verify_answer_grounding
