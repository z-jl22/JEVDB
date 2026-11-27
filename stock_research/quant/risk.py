"""Strategy and portfolio risk analytics using Python 3.12 stdlib only.

All numeric outputs are guaranteed finite and JSON-safe (``allow_nan=False``).
"""

from __future__ import annotations

from datetime import date, datetime
import math
import statistics
from typing import Any, Sequence

_EPS = 1e-12


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _parse_finite_float(value: Any) -> tuple[bool, float]:
    if value is None or isinstance(value, bool):
        return False, 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        return False, 0.0
    if not math.isfinite(out):
        return False, 0.0
    return True, out


def _parse_date(value: Any) -> date | None:
    if value is None or isinstance(value, bool):
        return None
    if type(value).__name__ in {"NaTType", "NAType"}:
        return None
    if isinstance(value, datetime):
        try:
            if value != value:
                return None
            return value.date()
        except Exception:
            return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none", "null"}:
        return None
    if len(text) <= 10:
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    if text[4] != "-" or text[7] != "-" or text[10] not in ("T", " "):
        return None
    iso_text = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        return datetime.fromisoformat(iso_text).date()
    except ValueError:
        return None


def _historical_var_cvar(returns: Sequence[float], confidence: float = 0.95) -> tuple[float, float]:
    """Compute historical VaR and CVaR (positive loss fractions)."""
    if len(returns) < 2:
        return 0.0, 0.0
    if not (0.0 < confidence < 1.0):
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    alpha = 1.0 - confidence
    sorted_r = sorted(float(r) for r in returns)
    n = len(sorted_r)
    pos = alpha * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    frac = pos - lo
    q = sorted_r[lo] + frac * (sorted_r[hi] - sorted_r[lo])
    var_val = max(0.0, -q)
    tail = [r for r in sorted_r if r <= q] or [sorted_r[0]]
    cvar_val = max(0.0, -statistics.fmean(tail))
    if cvar_val < var_val:
        cvar_val = var_val
    return var_val, cvar_val


def _extract_closed_pnls(trades: Sequence[dict[str, Any]]) -> list[float]:
    """Extract realized PnL list from trade records, reconstructing FIFO if absent."""
    executed: list[dict[str, Any]] = []
    for trade in trades:
        status = str(trade.get("status", "FILLED")).upper()
        if status in {"SKIPPED", "REJECTED", "CANCELLED"}:
            continue
        side = str(trade.get("side") or trade.get("action") or "").upper()
        if side == "HOLD":
            continue
        executed.append(trade)

    if not executed:
        return []

    explicit_pnls: list[float] = []
    has_explicit_sell_pnl = False
    for trade in executed:
        side = str(trade.get("side") or trade.get("action") or "").upper()
        pnl_raw = trade.get("realized_pnl")
        if pnl_raw is None:
            pnl_raw = trade.get("pnl")
        if pnl_raw is not None:
            if side in {"SELL", ""}:
                has_explicit_sell_pnl = True
                explicit_pnls.append(_safe_float(pnl_raw))

    if has_explicit_sell_pnl:
        return explicit_pnls

    # Reconstruct FIFO lot PnL from BUY/SELL fills
    lots: dict[str, list[list[float]]] = {}
    pnls: list[float] = []
    for trade in executed:
        side = str(trade.get("side") or trade.get("action") or "").upper()
        symbol = str(trade.get("symbol") or "").strip().upper()
        qty = _safe_float(trade.get("quantity") or trade.get("filled_qty"))
        price = _safe_float(trade.get("fill_price") if trade.get("fill_price") is not None else trade.get("price"))
        if qty <= 0 or price <= 0:
            continue
        fee = _safe_float(trade.get("total_fee"))
        if fee == 0.0:
            fee = (
                _safe_float(trade.get("commission"))
                + _safe_float(trade.get("transfer_fee"))
                + _safe_float(trade.get("stamp_duty"))
            )
        if side == "BUY":
            total_cost = qty * price + fee
            unit_cost = total_cost / qty
            lots.setdefault(symbol, []).append([qty, unit_cost])
        elif side == "SELL":
            sym_lots = lots.get(symbol, [])
            remaining = qty
            cost_basis = 0.0
            sold = 0.0
            while remaining > _EPS and sym_lots:
                lot = sym_lots[0]
                take = min(remaining, lot[0])
                cost_basis += take * lot[1]
                lot[0] -= take
                remaining -= take
                sold += take
                if lot[0] <= _EPS:
                    sym_lots.pop(0)
            if sold > _EPS:
                proceeds = sold * price - fee * (sold / qty)
                pnls.append(proceeds - cost_basis)
    return pnls


def compute_risk_metrics(
    equity_curve: Sequence[dict[str, Any]],
    trades: Sequence[dict[str, Any]] | None = None,
    *,
    initial_capital: float | None = None,
    risk_free_rate: float = 0.02,
    trading_days_per_year: int = 252,
) -> dict[str, Any]:
    """Compute strategy performance and risk metrics from an equity curve and trades."""
    if trading_days_per_year <= 0:
        raise ValueError("trading_days_per_year must be positive")

    trade_list = list(trades) if trades else []
    executed_trades = [
        t
        for t in trade_list
        if str(t.get("status", "FILLED")).upper() not in {"SKIPPED", "REJECTED", "CANCELLED"}
        and str(t.get("side") or t.get("action") or "").upper() != "HOLD"
    ]
    num_trades = len(executed_trades)

    total_fees = 0.0
    total_notional = 0.0
    for t in executed_trades:
        fee = _safe_float(t.get("total_fee"))
        if fee == 0.0:
            fee = (
                _safe_float(t.get("commission"))
                + _safe_float(t.get("transfer_fee"))
                + _safe_float(t.get("stamp_duty"))
            )
        total_fees += fee
        notional = _safe_float(t.get("notional"))
        if notional == 0.0:
            qty = _safe_float(t.get("quantity") or t.get("filled_qty"))
            px = _safe_float(t.get("fill_price") if t.get("fill_price") is not None else t.get("price"))
            notional = abs(qty * px)
        else:
            notional = abs(notional)
        total_notional += notional

    closed_pnls = _extract_closed_pnls(executed_trades)
    num_closed_trades = len(closed_pnls)
    if num_closed_trades > 0:
        wins = sum(1 for p in closed_pnls if p > 0.0)
        win_rate = wins / num_closed_trades
        gross_profit = sum(p for p in closed_pnls if p > 0.0)
        gross_loss = abs(sum(p for p in closed_pnls if p < 0.0))
        if gross_profit <= _EPS:
            profit_factor: float | None = 0.0
        elif gross_loss <= _EPS:
            profit_factor = None
        else:
            profit_factor = gross_profit / gross_loss
    else:
        win_rate = 0.0
        profit_factor = 0.0

    rows = sorted(
        equity_curve,
        key=lambda row: str(row.get("date") or row.get("day") or "")[:10],
    )
    n = len(rows)

    if n == 0:
        return {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "cagr": 0.0,
            "annualized_volatility": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "calmar_ratio": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_duration_days": 0,
            "win_rate": round(win_rate, 6),
            "profit_factor": round(profit_factor, 6) if profit_factor is not None else None,
            "turnover": 0.0,
            "annualized_turnover": 0.0,
            "var_95": 0.0,
            "cvar_95": 0.0,
            "downside_deviation": 0.0,
            "num_bars": 0,
            "num_trades": num_trades,
            "total_trades": num_trades,
            "num_closed_trades": num_closed_trades,
            "total_fees": round(total_fees, 2),
        }

    equities = [_safe_float(r.get("equity")) for r in rows]
    dates = [_parse_date(r.get("date") or r.get("day")) for r in rows]

    c0 = _safe_float(initial_capital) if initial_capital is not None else equities[0]
    e_last = equities[-1]

    total_return = (e_last / c0 - 1.0) if c0 > _EPS else 0.0

    if n < 2 or c0 <= _EPS:
        annualized_return = 0.0
    else:
        years = (n - 1) / float(trading_days_per_year)
        if years <= _EPS:
            annualized_return = 0.0
        elif e_last <= 0.0:
            annualized_return = -1.0
        else:
            ratio = e_last / c0
            annualized_return = ratio ** (1.0 / years) - 1.0
            if not math.isfinite(annualized_return):
                annualized_return = 0.0

    returns: list[float] = []
    for idx in range(1, n):
        prev_e = equities[idx - 1]
        cur_e = equities[idx]
        if prev_e > _EPS:
            returns.append((cur_e - prev_e) / prev_e)
        else:
            returns.append(0.0)

    m = len(returns)
    rf_daily = risk_free_rate / float(trading_days_per_year)

    if m >= 2:
        mean_r = statistics.fmean(returns)
        sigma_d = statistics.stdev(returns)
        annualized_volatility = sigma_d * math.sqrt(trading_days_per_year)
        sharpe_ratio = ((mean_r - rf_daily) / sigma_d) * math.sqrt(trading_days_per_year) if sigma_d > _EPS else 0.0

        downside_diffs = [min(0.0, r - rf_daily) for r in returns]
        sigma_down_daily = math.sqrt(sum(d * d for d in downside_diffs) / m)
        downside_deviation = sigma_down_daily * math.sqrt(trading_days_per_year)
        sortino_ratio = (
            ((mean_r - rf_daily) / sigma_down_daily) * math.sqrt(trading_days_per_year)
            if sigma_down_daily > _EPS
            else 0.0
        )
    elif m == 1:
        sigma_d = 0.0
        annualized_volatility = 0.0
        sharpe_ratio = 0.0
        downside_diff = min(0.0, returns[0] - rf_daily)
        downside_deviation = abs(downside_diff) * math.sqrt(trading_days_per_year)
        sortino_ratio = 0.0
    else:
        annualized_volatility = 0.0
        sharpe_ratio = 0.0
        downside_deviation = 0.0
        sortino_ratio = 0.0

    peak = max(c0, equities[0]) if c0 > _EPS else equities[0]
    peak_date = dates[0]
    max_drawdown = 0.0
    max_drawdown_duration_days = 0
    for idx in range(n):
        eq = equities[idx]
        dt = dates[idx]
        if eq >= peak:
            peak = eq
            peak_date = dt
        else:
            dd = (peak - eq) / peak if peak > _EPS else 0.0
            if dd > max_drawdown:
                max_drawdown = dd
            if n >= 2 and dt is not None and peak_date is not None:
                dur = (dt - peak_date).days
                if dur > max_drawdown_duration_days:
                    max_drawdown_duration_days = dur

    calmar_ratio = (annualized_return / max_drawdown) if (n >= 2 and max_drawdown > _EPS) else 0.0

    var_95, cvar_95 = _historical_var_cvar(returns, confidence=0.95)

    mean_equity = statistics.fmean(equities) if equities else 0.0
    turnover = (total_notional / mean_equity) if mean_equity > _EPS else 0.0
    annualized_turnover = (turnover * trading_days_per_year / max(n - 1, 1)) if turnover > 0.0 else 0.0

    def _clean(val: float, ndigits: int = 6) -> float:
        if not math.isfinite(val):
            return 0.0
        return round(float(val), ndigits)

    return {
        "total_return": _clean(total_return),
        "annualized_return": _clean(annualized_return),
        "cagr": _clean(annualized_return),
        "annualized_volatility": _clean(annualized_volatility),
        "sharpe_ratio": _clean(sharpe_ratio),
        "sortino_ratio": _clean(sortino_ratio),
        "calmar_ratio": _clean(calmar_ratio),
        "max_drawdown": _clean(max_drawdown),
        "max_drawdown_duration_days": int(max_drawdown_duration_days),
        "win_rate": _clean(win_rate),
        "profit_factor": _clean(profit_factor) if profit_factor is not None else None,
        "turnover": _clean(turnover),
        "annualized_turnover": _clean(annualized_turnover),
        "var_95": _clean(var_95),
        "cvar_95": _clean(cvar_95),
        "downside_deviation": _clean(downside_deviation),
        "num_bars": int(n),
        "num_trades": int(num_trades),
        "total_trades": int(num_trades),
        "num_closed_trades": int(num_closed_trades),
        "total_fees": round(float(total_fees), 2),
    }


def _extract_price_series(
    entries: Sequence[dict[str, Any] | float | int],
) -> tuple[bool, list[tuple[str, float]]]:
    """Normalize a symbol's price history into (has_dates, [(date_key, close)])."""
    if (
        isinstance(entries, (str, bytes, bytearray, memoryview, dict))
        or not isinstance(entries, Sequence)
    ):
        raise ValueError(
            f"price series must be a sequence of numbers or price objects, got {type(entries).__name__}"
        )
    if len(entries) == 0:
        return False, []

    saw_dict = False
    saw_scalar = False
    has_dates = False
    out: list[tuple[str, float]] = []

    for idx, item in enumerate(entries):
        if isinstance(item, dict):
            if saw_scalar:
                raise ValueError(
                    "price series cannot mix numeric values and price objects"
                )
            saw_dict = True
            if ("date" in item and item["date"] is not None) or (
                "day" in item and item["day"] is not None
            ):
                has_dates = True
            px = item.get("close")
            if px is None:
                px = item.get("price")
            if (
                px is None
                or isinstance(px, (bool, str, bytes, bytearray, dict, list, tuple, set))
            ):
                raise ValueError(
                    f"price series entry[{idx}] close/price must be a finite number, got {px!r}"
                )
            try:
                px_val = float(px)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"price series entry[{idx}] close/price must be a finite number, got {px!r}"
                ) from exc
            if not math.isfinite(px_val) or px_val <= 0.0:
                raise ValueError(
                    f"price series entry[{idx}] close/price must be finite and > 0, got {px!r}"
                )
            dt_raw = item.get("date") if item.get("date") is not None else item.get("day")
            dt_key = str(dt_raw).strip()[:10] if dt_raw is not None and str(dt_raw).strip() else f"{idx:08d}"
            out.append((dt_key, px_val))
        else:
            if saw_dict:
                raise ValueError(
                    "price series cannot mix price objects and non-object values"
                )
            saw_scalar = True
            if (
                item is None
                or isinstance(item, (bool, str, bytes, bytearray, dict, list, tuple, set))
            ):
                raise ValueError(
                    f"price series entry[{idx}] must be a finite number or price object, got {item!r}"
                )
            try:
                px_val = float(item)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"price series entry[{idx}] must be a finite number or price object, got {item!r}"
                ) from exc
            if not math.isfinite(px_val) or px_val <= 0.0:
                raise ValueError(
                    f"price series entry[{idx}] must be finite and > 0, got {item!r}"
                )
            out.append((f"{idx:08d}", px_val))

    if has_dates:
        out.sort(key=lambda pair: pair[0])
    return has_dates, out


def _series_returns(prices: Sequence[float]) -> list[float]:
    res: list[float] = []
    for i in range(1, len(prices)):
        prev_p = prices[i - 1]
        cur_p = prices[i]
        res.append((cur_p / prev_p - 1.0) if prev_p > _EPS else 0.0)
    return res


def _pearson_corr(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return 0.0
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if den_x <= _EPS or den_y <= _EPS:
        return 0.0
    corr = num / (den_x * den_y)
    return max(-1.0, min(1.0, corr))


def compute_portfolio_risk(
    holdings: Sequence[dict[str, Any]],
    price_history: dict[str, Sequence[dict[str, Any] | float]],
    benchmark_history: Sequence[dict[str, Any] | float] | None = None,
    *,
    confidence: float = 0.95,
    trading_days_per_year: int = 252,
) -> dict[str, Any]:
    """Compute portfolio VaR/CVaR, correlation matrix, HHI, exposures, vol & beta."""
    if not (0.0 < confidence < 1.0):
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    if trading_days_per_year <= 0:
        raise ValueError("trading_days_per_year must be positive")

    if holdings is not None and (
        isinstance(holdings, (str, bytes, bytearray, dict))
        or not isinstance(holdings, Sequence)
    ):
        raise ValueError(
            f"holdings must be a sequence of holding dicts, got {type(holdings).__name__}"
        )
    if price_history is not None and not isinstance(price_history, dict):
        raise ValueError(
            f"price_history must be a mapping of symbol -> price series, got {type(price_history).__name__}"
        )

    b_extracted: tuple[bool, list[tuple[str, float]]] | None = (
        _extract_price_series(benchmark_history)
        if benchmark_history is not None
        else None
    )

    norm_history: dict[str, tuple[bool, list[tuple[str, float]]]] = {}
    for sym_key, entries in (price_history or {}).items():
        sym = str(sym_key).strip().upper()
        if sym:
            norm_history[sym] = _extract_price_series(entries)

    warnings: list[str] = []
    aggregated: dict[str, dict[str, Any]] = {}
    for idx, h in enumerate(holdings or []):
        if not isinstance(h, dict):
            raise ValueError(f"holdings[{idx}] must be a dict, got {type(h).__name__}")
        sym = str(h.get("symbol") or "").strip().upper()
        if not sym:
            continue
        mv_raw = h.get("market_value")
        if mv_raw is None:
            mv_raw = h.get("value")
        if mv_raw is not None:
            ok, mv = _parse_finite_float(mv_raw)
            if not ok:
                warnings.append(f"invalid_or_non_finite_holding:{sym}")
                continue
        elif h.get("quantity") is not None:
            ok, qty_val = _parse_finite_float(h.get("quantity"))
            if not ok:
                warnings.append(f"invalid_or_non_finite_holding:{sym}")
                continue
            if qty_val < 0.0:
                warnings.append(f"negative_holding_value:{sym}")
                continue
            if sym in norm_history and norm_history[sym][1]:
                mv = qty_val * norm_history[sym][1][-1][1]
            else:
                mv = 0.0
        elif h.get("weight") is not None:
            ok, mv = _parse_finite_float(h.get("weight"))
            if not ok:
                warnings.append(f"invalid_or_non_finite_holding:{sym}")
                continue
        else:
            mv = 0.0

        if mv < 0.0:
            warnings.append(f"negative_holding_value:{sym}")
            continue
        if mv == 0.0:
            continue

        sector = str(h.get("sector") or "Unknown").strip() or "Unknown"
        market = str(h.get("market") or "UNKNOWN").strip().upper() or "UNKNOWN"
        if sym in aggregated:
            aggregated[sym]["market_value"] += mv
        else:
            aggregated[sym] = {"market_value": mv, "sector": sector, "market": market}

    total_value = sum(item["market_value"] for item in aggregated.values())
    if not aggregated or total_value <= _EPS:
        return {
            "var_95": 0.0,
            "cvar_95": 0.0,
            "correlation_matrix": {},
            "hhi": 0.0,
            "effective_n": 0.0,
            "max_position_weight": 0.0,
            "concentration_level": "diversified",
            "sector_exposure": {},
            "market_exposure": {},
            "portfolio_volatility": 0.0,
            "diversification_ratio": 1.0,
            "portfolio_beta": None,
            "weights": {},
            "warnings": [*warnings, "empty_or_zero_portfolio"],
        }

    symbols = sorted(aggregated.keys())
    weights: dict[str, float] = {
        sym: aggregated[sym]["market_value"] / total_value for sym in symbols
    }

    hhi = sum(w * w for w in weights.values())
    effective_n = (1.0 / hhi) if hhi > _EPS else 0.0
    max_weight = max(weights.values()) if weights else 0.0
    if hhi > 0.25:
        concentration_level = "high"
    elif hhi > 0.15:
        concentration_level = "moderate"
    else:
        concentration_level = "diversified"

    sector_exposure: dict[str, float] = {}
    market_exposure: dict[str, float] = {}
    for sym in symbols:
        sec = aggregated[sym]["sector"]
        mkt = aggregated[sym]["market"]
        w = weights[sym]
        sector_exposure[sec] = sector_exposure.get(sec, 0.0) + w
        market_exposure[mkt] = market_exposure.get(mkt, 0.0) + w

    present_syms = [s for s in symbols if s in norm_history and len(norm_history[s][1]) >= 2]
    missing_syms = [s for s in symbols if s not in present_syms]
    for ms in missing_syms:
        warnings.append(f"missing_or_short_price_history:{ms}")

    aligned_dates: list[str] = []
    symbol_returns: dict[str, list[float]] = {s: [] for s in symbols}

    if present_syms:
        all_dated = all(norm_history[s][0] for s in present_syms)
        if all_dated:
            date_maps = {s: dict(norm_history[s][1]) for s in present_syms}
            common_dates = sorted(set.intersection(*(set(m.keys()) for m in date_maps.values())))
            if len(common_dates) >= 2:
                aligned_dates = common_dates[1:]
                for s in present_syms:
                    px_seq = [date_maps[s][d] for d in common_dates]
                    symbol_returns[s] = _series_returns(px_seq)
            else:
                warnings.append("insufficient_overlapping_dates")
        else:
            min_len = min(len(norm_history[s][1]) for s in present_syms)
            if min_len >= 2:
                for s in present_syms:
                    px_seq = [px for _, px in norm_history[s][1][-min_len:]]
                    symbol_returns[s] = _series_returns(px_seq)

    t_len = max((len(v) for v in symbol_returns.values()), default=0)
    for s in symbols:
        if len(symbol_returns[s]) != t_len:
            symbol_returns[s] = [0.0] * t_len

    portfolio_returns: list[float] = []
    if t_len > 0:
        for t in range(t_len):
            portfolio_returns.append(sum(weights[s] * symbol_returns[s][t] for s in symbols))

    var_95, cvar_95 = _historical_var_cvar(portfolio_returns, confidence=confidence)

    corr_matrix: dict[str, dict[str, float]] = {}
    for s1 in symbols:
        corr_matrix[s1] = {}
        for s2 in symbols:
            if s1 == s2:
                corr_matrix[s1][s2] = 1.0
            elif t_len < 2 or s1 in missing_syms or s2 in missing_syms:
                corr_matrix[s1][s2] = 0.0
            else:
                corr_matrix[s1][s2] = round(_pearson_corr(symbol_returns[s1], symbol_returns[s2]), 6)

    if t_len >= 2:
        port_vol_daily = statistics.stdev(portfolio_returns)
        portfolio_volatility = port_vol_daily * math.sqrt(trading_days_per_year)
        weighted_vol_sum = 0.0
        for s in symbols:
            s_vol = statistics.stdev(symbol_returns[s]) * math.sqrt(trading_days_per_year)
            weighted_vol_sum += weights[s] * s_vol
        diversification_ratio = (
            (weighted_vol_sum / portfolio_volatility) if portfolio_volatility > _EPS else 1.0
        )
    else:
        portfolio_volatility = 0.0
        diversification_ratio = 1.0

    portfolio_beta: float | None = None
    if b_extracted is not None and t_len >= 2:
        b_dated, b_series = b_extracted
        if len(b_series) >= 2:
            if b_dated and aligned_dates:
                b_map = dict(b_series)
                # Align benchmark prices on common dates (including day 0 before aligned_dates[0])
                # Re-derive common dates with benchmark
                date_maps = {s: dict(norm_history[s][1]) for s in present_syms}
                common_with_b = sorted(
                    set(b_map.keys()).intersection(*(set(m.keys()) for m in date_maps.values()))
                )
                if len(common_with_b) >= 3:
                    b_ret = _series_returns([b_map[d] for d in common_with_b])
                    p_ret: list[float] = []
                    sym_b_rets = {
                        s: _series_returns([date_maps[s][d] for d in common_with_b])
                        for s in present_syms
                    }
                    for t in range(len(b_ret)):
                        p_ret.append(sum(weights[s] * sym_b_rets.get(s, [0.0] * len(b_ret))[t] for s in symbols))
                    mean_p = statistics.fmean(p_ret)
                    mean_b = statistics.fmean(b_ret)
                    var_b = sum((rb - mean_b) ** 2 for rb in b_ret)
                    if var_b > _EPS:
                        cov_pb = sum((rp - mean_p) * (rb - mean_b) for rp, rb in zip(p_ret, b_ret))
                        portfolio_beta = round(cov_pb / var_b, 6)
            else:
                b_ret = _series_returns([px for _, px in b_series])
                common_len = min(len(portfolio_returns), len(b_ret))
                if common_len >= 2:
                    p_sub = portfolio_returns[-common_len:]
                    b_sub = b_ret[-common_len:]
                    mean_p = statistics.fmean(p_sub)
                    mean_b = statistics.fmean(b_sub)
                    var_b = sum((rb - mean_b) ** 2 for rb in b_sub)
                    if var_b > _EPS:
                        cov_pb = sum((rp - mean_p) * (rb - mean_b) for rp, rb in zip(p_sub, b_sub))
                        portfolio_beta = round(cov_pb / var_b, 6)

    return {
        "var_95": round(var_95, 6),
        "cvar_95": round(cvar_95, 6),
        "correlation_matrix": corr_matrix,
        "hhi": round(hhi, 6),
        "effective_n": round(effective_n, 6),
        "max_position_weight": round(max_weight, 6),
        "concentration_level": concentration_level,
        "sector_exposure": {k: round(v, 6) for k, v in sorted(sector_exposure.items())},
        "market_exposure": {k: round(v, 6) for k, v in sorted(market_exposure.items())},
        "portfolio_volatility": round(portfolio_volatility, 6),
        "diversification_ratio": round(diversification_ratio, 6),
        "portfolio_beta": portfolio_beta,
        "weights": {k: round(v, 6) for k, v in weights.items()},
        "warnings": warnings,
    }
