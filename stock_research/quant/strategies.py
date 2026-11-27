"""Quant & value strategy registry with safe declarative AST rule evaluation.

Zero import-time dependencies on ``stock_research.config`` or ``stock_research.storage``.
Uses Python 3.12 stdlib only (with optional DuckDB table sync when ``con`` is passed).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Any
from uuid import uuid4

_STRATEGY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VALID_MARKETS = {"CN", "US", "BOTH", "GLOBAL"}
_VALID_SOURCES = {"builtin", "agent", "user"}
_MAX_AST_DEPTH = 16
_MAX_AST_NODES = 256

_ALLOWED_OPS = {
    ">",
    ">=",
    "<",
    "<=",
    "==",
    "!=",
    "gt",
    "gte",
    "lt",
    "lte",
    "eq",
    "ne",
    "between",
    "in",
    "cross_above",
    "cross_below",
}

_LEFT_KEYS = ("field", "left", "indicator", "metric")
_RIGHT_KEYS = ("value", "param", "ref", "right")
_ALLOWED_LEAF_KEYS = set(_LEFT_KEYS) | set(_RIGHT_KEYS) | {"op"}


class StrategyNotFoundError(KeyError):
    """Raised when a strategy_id cannot be found in the registry."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_strategy_dir() -> Path:
    env_dir = os.environ.get("STOCK_RESEARCH_STRATEGY_DIR")
    if env_dir:
        return Path(env_dir)
    try:
        from stock_research.config import get_settings  # type: ignore

        settings = get_settings()
        if getattr(settings, "strategy_dir", None):
            return Path(settings.strategy_dir)
    except Exception:
        pass
    return Path(__file__).resolve().parents[2] / ".tmp" / "stock_research" / "strategies"


def _validate_strategy_id(strategy_id: str) -> str:
    if not isinstance(strategy_id, str) or not _STRATEGY_ID_RE.match(strategy_id):
        raise ValueError(
            f"Invalid strategy_id {strategy_id!r}: must match {_STRATEGY_ID_RE.pattern}"
        )
    return strategy_id


def _is_finite_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val))


def _validate_json_safe(val: Any, path: str) -> None:
    if val is None or isinstance(val, (bool, str, int)):
        return
    if isinstance(val, float):
        if not math.isfinite(val):
            raise ValueError(f"Non-finite float {val!r} at {path} is not valid JSON")
        return
    if isinstance(val, (list, tuple)):
        for idx, item in enumerate(val):
            _validate_json_safe(item, f"{path}[{idx}]")
        return
    if isinstance(val, dict):
        for k, v in val.items():
            if not isinstance(k, str):
                raise ValueError(f"Non-string dict key {k!r} at {path} is not valid JSON")
            _validate_json_safe(v, f"{path}.{k}")
        return
    raise ValueError(f"Unsupported non-JSON value of type {type(val).__name__} at {path}")


def _validate_strategy_params(params: dict[str, Any]) -> None:
    if not isinstance(params, dict):
        raise ValueError(f"StrategySpec.params must be a dict, got {type(params).__name__}")
    _validate_json_safe(params, "StrategySpec.params")
    for pct_key in ("stop_loss_pct", "take_profit_pct"):
        if pct_key in params and params[pct_key] is not None:
            val = params[pct_key]
            if not _is_finite_number(val) or float(val) < 0.0:
                raise ValueError(
                    f"StrategySpec.params.{pct_key} must be a non-negative finite number, got {val!r}"
                )
    if "min_hold_days" in params and params["min_hold_days"] is not None:
        mh = params["min_hold_days"]
        if isinstance(mh, bool) or not isinstance(mh, int) or mh < 0:
            raise ValueError(
                f"StrategySpec.params.min_hold_days must be a non-negative integer, got {mh!r}"
            )


def _reject_json_constant(constant: str) -> Any:
    raise ValueError(f"Non-standard JSON constant {constant!r} is not allowed")


def _loads_strict_json(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_json_constant)


def _validate_ast_node(node: Any, path: str, depth: int, counter: list[int]) -> None:
    if depth > _MAX_AST_DEPTH:
        raise ValueError(f"Rule depth exceeds maximum {_MAX_AST_DEPTH} at {path}")
    counter[0] += 1
    if counter[0] > _MAX_AST_NODES:
        raise ValueError(f"Rule node count exceeds maximum {_MAX_AST_NODES} at {path}")

    if not isinstance(node, dict):
        raise ValueError(
            f"Rule node at {path} must be a dict (string/code expressions are forbidden), got {type(node).__name__}"
        )
    if not node:
        raise ValueError(f"Rule node at {path} must not be empty")

    # Combinators
    for key in ("all", "and"):
        if key in node:
            if len(node) != 1:
                raise ValueError(f"Combinator '{key}' at {path} must be the sole key")
            children = node[key]
            if not isinstance(children, list):
                raise ValueError(f"Combinator '{key}' at {path} requires a list")
            for idx, child in enumerate(children):
                _validate_ast_node(child, f"{path}.{key}[{idx}]", depth + 1, counter)
            return

    for key in ("any", "or"):
        if key in node:
            if len(node) != 1:
                raise ValueError(f"Combinator '{key}' at {path} must be the sole key")
            children = node[key]
            if not isinstance(children, list):
                raise ValueError(f"Combinator '{key}' at {path} requires a list")
            for idx, child in enumerate(children):
                _validate_ast_node(child, f"{path}.{key}[{idx}]", depth + 1, counter)
            return

    if "not" in node:
        if len(node) != 1:
            raise ValueError(f"Combinator 'not' at {path} must be the sole key")
        _validate_ast_node(node["not"], f"{path}.not", depth + 1, counter)
        return

    if "const" in node:
        if len(node) != 1:
            raise ValueError(f"Literal 'const' at {path} must be the sole key")
        if not isinstance(node["const"], bool):
            raise ValueError(f"Literal 'const' at {path} requires a bool")
        return

    # Comparison leaf
    unknown_keys = set(node.keys()) - _ALLOWED_LEAF_KEYS
    if unknown_keys:
        raise ValueError(f"Unknown rule keys {sorted(unknown_keys)} at {path}")

    left_present = [k for k in _LEFT_KEYS if k in node]
    if len(left_present) != 1:
        raise ValueError(f"Comparison node at {path} must specify exactly one field key")
    field_name = node[left_present[0]]
    if not isinstance(field_name, str) or not field_name.strip():
        raise ValueError(f"Field name at {path} must be a non-empty string")

    op = node.get("op")
    if not isinstance(op, str) or op not in _ALLOWED_OPS:
        raise ValueError(f"Unsupported or missing operator {op!r} at {path}")

    right_present = [k for k in _RIGHT_KEYS if k in node]
    if len(right_present) != 1:
        raise ValueError(
            f"Comparison node at {path} must specify exactly one of value/param/ref/right"
        )

    rkey = right_present[0]
    rval = node[rkey]
    if rkey in {"param", "ref"}:
        if not isinstance(rval, str) or not rval.strip():
            raise ValueError(f"'{rkey}' at {path} must be a non-empty string")
    elif rkey == "right":
        if isinstance(rval, dict):
            if set(rval.keys()) - {"param", "field", "ref"} or len(rval) != 1:
                raise ValueError(f"Invalid 'right' reference dict at {path}: {rval!r}")
            inner_val = next(iter(rval.values()))
            if not isinstance(inner_val, str) or not inner_val.strip():
                raise ValueError(f"Invalid 'right' reference target at {path}")
    if op == "between" and rkey == "value":
        if not isinstance(rval, (list, tuple)) or len(rval) != 2:
            raise ValueError(f"'between' at {path} requires a 2-element [lo, hi] list")
    if op == "in" and rkey == "value":
        if not isinstance(rval, (list, tuple)):
            raise ValueError(f"'in' at {path} requires a list or tuple, got {type(rval).__name__}")
    if rkey == "value" or (rkey == "right" and not isinstance(rval, dict)):
        _validate_json_safe(rval, f"{path}.{rkey}")


def validate_rules(rules: dict[str, Any]) -> None:
    """Validate declarative strategy rule blocks (buy/sell/ranking) without eval/exec."""
    if not isinstance(rules, dict):
        raise ValueError(f"rules must be a dict, got {type(rules).__name__}")
    allowed_top = {"buy", "sell", "ranking"}
    unknown = set(rules.keys()) - allowed_top
    if unknown:
        raise ValueError(f"Unknown top-level rule keys: {sorted(unknown)}")

    if "buy" in rules and rules["buy"] is not None:
        _validate_ast_node(rules["buy"], "rules.buy", depth=1, counter=[0])
    if "sell" in rules and rules["sell"] is not None:
        _validate_ast_node(rules["sell"], "rules.sell", depth=1, counter=[0])
    if "ranking" in rules and rules["ranking"] is not None:
        ranking = rules["ranking"]
        if not isinstance(ranking, dict):
            raise ValueError("rules.ranking must be a dict")
        r_unknown = set(ranking.keys()) - {"field", "order"}
        if r_unknown:
            raise ValueError(f"Unknown keys in rules.ranking: {sorted(r_unknown)}")
        r_field = ranking.get("field")
        if not isinstance(r_field, str) or not r_field.strip():
            raise ValueError("rules.ranking.field must be a non-empty string")
        order = ranking.get("order", "desc")
        if order not in {"asc", "desc"}:
            raise ValueError(f"rules.ranking.order must be 'asc' or 'desc', got {order!r}")


def _resolve_operand(
    node: dict[str, Any],
    features: dict[str, Any],
    params: dict[str, Any],
    prev_features: dict[str, Any] | None,
) -> tuple[Any, Any, bool]:
    """Return (cur_right, prev_right, is_explicit_none_literal)."""
    if "param" in node:
        val = params.get(node["param"])
        return val, val, False
    if "ref" in node:
        ref_key = node["ref"]
        cur_val = features.get(ref_key)
        prev_val = prev_features.get(ref_key) if prev_features is not None else features.get(f"{ref_key}_prev")
        return cur_val, prev_val, False
    if "value" in node:
        val = node["value"]
        return val, val, val is None
    right = node["right"]
    if isinstance(right, dict):
        if "param" in right:
            val = params.get(right["param"])
            return val, val, False
        ref_key = right.get("field") or right.get("ref")
        cur_val = features.get(ref_key)
        prev_val = prev_features.get(ref_key) if prev_features is not None else features.get(f"{ref_key}_prev")
        return cur_val, prev_val, False
    return right, right, right is None


def _compare_values(
    op: str,
    left: Any,
    right: Any,
    prev_left: Any,
    prev_right: Any,
    explicit_none: bool,
) -> bool:
    if op in {"==", "eq"}:
        if explicit_none:
            return left is None
        if left is None or right is None:
            return False
        if isinstance(left, bool) or isinstance(right, bool):
            return bool(left is right) if isinstance(left, bool) and isinstance(right, bool) else False
        if _is_finite_number(left) and _is_finite_number(right):
            return float(left) == float(right)
        return left == right

    if op in {"!=", "ne"}:
        if explicit_none:
            return left is not None
        if left is None or right is None:
            return False
        if isinstance(left, bool) or isinstance(right, bool):
            return left is not right if isinstance(left, bool) and isinstance(right, bool) else False
        if _is_finite_number(left) and _is_finite_number(right):
            return float(left) != float(right)
        return left != right

    if op == "in":
        if left is None or not isinstance(right, (list, tuple, set)):
            return False
        if _is_finite_number(left):
            lf = float(left)
            return any(
                (_is_finite_number(item) and lf == float(item)) or (not _is_finite_number(item) and left == item)
                for item in right
            )
        return left in right

    if op == "between":
        if not _is_finite_number(left) or not isinstance(right, (list, tuple)) or len(right) != 2:
            return False
        lo, hi = right[0], right[1]
        if not (_is_finite_number(lo) and _is_finite_number(hi)):
            return False
        return float(lo) <= float(left) <= float(hi)

    if op in {"cross_above", "cross_below"}:
        if not (
            _is_finite_number(left)
            and _is_finite_number(right)
            and _is_finite_number(prev_left)
            and _is_finite_number(prev_right)
        ):
            return False
        lf, rf = float(left), float(right)
        plf, prf = float(prev_left), float(prev_right)
        if op == "cross_above":
            return plf <= prf and lf > rf
        return plf >= prf and lf < rf

    # Ordered numeric comparisons
    if not (_is_finite_number(left) and _is_finite_number(right)):
        return False
    lf, rf = float(left), float(right)
    if op in {">", "gt"}:
        return lf > rf
    if op in {">=", "gte"}:
        return lf >= rf
    if op in {"<", "lt"}:
        return lf < rf
    if op in {"<=", "lte"}:
        return lf <= rf
    return False


def _eval_validated_node(
    node: dict[str, Any],
    features: dict[str, Any],
    params: dict[str, Any],
    prev_features: dict[str, Any] | None,
) -> bool:
    if "all" in node:
        return all(_eval_validated_node(c, features, params, prev_features) for c in node["all"])
    if "and" in node:
        return all(_eval_validated_node(c, features, params, prev_features) for c in node["and"])
    if "any" in node:
        return any(_eval_validated_node(c, features, params, prev_features) for c in node["any"])
    if "or" in node:
        return any(_eval_validated_node(c, features, params, prev_features) for c in node["or"])
    if "not" in node:
        return not _eval_validated_node(node["not"], features, params, prev_features)
    if "const" in node:
        return bool(node["const"])

    left_key = next(node[k] for k in _LEFT_KEYS if k in node)
    cur_left = features.get(left_key)
    prev_left = prev_features.get(left_key) if prev_features is not None else features.get(f"{left_key}_prev")
    cur_right, prev_right, explicit_none = _resolve_operand(node, features, params, prev_features)
    return _compare_values(node["op"], cur_left, cur_right, prev_left, prev_right, explicit_none)


def evaluate_rule(
    node: dict[str, Any],
    features: dict[str, Any],
    params: dict[str, Any] | None = None,
    prev_features: dict[str, Any] | None = None,
) -> bool:
    """Safely evaluate a declarative AST rule node against feature dicts."""
    _validate_ast_node(node, "rule", depth=1, counter=[0])
    return _eval_validated_node(node, features, params or {}, prev_features)


@dataclass
class StrategySpec:
    strategy_id: str
    name: str
    style: str
    market: str = "CN"
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    rules: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    source: str = "user"
    version: int = 1

    def __post_init__(self) -> None:
        self.strategy_id = _validate_strategy_id(self.strategy_id)
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("StrategySpec.name must be a non-empty string")
        self.name = self.name.strip()
        if not isinstance(self.style, str) or not self.style.strip():
            raise ValueError("StrategySpec.style must be a non-empty string")
        self.style = self.style.strip()
        if not isinstance(self.market, str) or not self.market.strip():
            raise ValueError("StrategySpec.market must be a non-empty string")
        self.market = self.market.strip().upper()
        if self.market not in _VALID_MARKETS:
            raise ValueError(f"StrategySpec.market must be one of {sorted(_VALID_MARKETS)}, got {self.market!r}")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("StrategySpec.source must be a non-empty string")
        self.source = self.source.strip().lower()
        if self.source not in _VALID_SOURCES:
            raise ValueError(f"StrategySpec.source must be one of {sorted(_VALID_SOURCES)}, got {self.source!r}")
        if not self.created_at:
            self.created_at = _utc_now_iso()
        _validate_strategy_params(self.params)
        validate_rules(self.rules)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategySpec":
        if not isinstance(data, dict):
            raise ValueError("Strategy data must be a dict")
        raw_sid = data["strategy_id"]
        raw_name = data["name"]
        raw_style = data["style"]
        if not isinstance(raw_sid, str) or not isinstance(raw_name, str) or not isinstance(raw_style, str):
            raise ValueError("strategy_id, name, and style must be strings")

        raw_market = data.get("market", "CN")
        if raw_market is None:
            raw_market = "CN"
        elif not isinstance(raw_market, str) or not raw_market.strip():
            raise ValueError("StrategySpec.market must be a non-empty string")

        raw_source = data.get("source", "user")
        if raw_source is None:
            raw_source = "user"
        elif not isinstance(raw_source, str) or not raw_source.strip():
            raise ValueError("StrategySpec.source must be a non-empty string")

        raw_desc = data.get("description", "")
        if raw_desc is None:
            raw_desc = ""
        elif not isinstance(raw_desc, str):
            raise ValueError("StrategySpec.description must be a string")

        raw_params = data.get("params")
        if raw_params is None:
            params: dict[str, Any] = {}
        elif isinstance(raw_params, dict):
            params = dict(raw_params)
        else:
            raise ValueError(f"StrategySpec.params must be a dict, got {type(raw_params).__name__}")

        raw_rules = data.get("rules")
        if raw_rules is None:
            rules: dict[str, Any] = {}
        elif isinstance(raw_rules, dict):
            rules = dict(raw_rules)
        else:
            raise ValueError(f"StrategySpec.rules must be a dict, got {type(raw_rules).__name__}")

        raw_version = data.get("version", 1)
        if raw_version is None:
            version = 1
        elif isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ValueError("StrategySpec.version must be an integer")
        else:
            version = raw_version

        return cls(
            strategy_id=raw_sid,
            name=raw_name,
            style=raw_style,
            market=raw_market,
            description=raw_desc,
            params=params,
            rules=rules,
            created_at=str(data.get("created_at") or ""),
            source=raw_source,
            version=version,
        )


def _compute_signal_score(spec: StrategySpec, features: dict[str, Any]) -> float:
    ranking = spec.rules.get("ranking")
    if isinstance(ranking, dict):
        field_name = ranking.get("field")
        order = ranking.get("order", "desc")
        val = features.get(field_name) if (isinstance(field_name, str) and field_name) else None
        if _is_finite_number(val):
            return -float(val) if order == "asc" else float(val)
    for fallback_key, sign in (
        ("earnings_yield", 1.0),
        ("momentum_return", 1.0),
        ("zscore_close", -1.0),
    ):
        val = features.get(fallback_key)
        if _is_finite_number(val):
            return sign * float(val)
    return 0.0


def evaluate_strategy_signals(
    spec: StrategySpec,
    features: dict[str, Any],
    prev_features: dict[str, Any] | None = None,
    *,
    has_position: bool = False,
    hold_days: int = 0,
) -> dict[str, Any]:
    """Evaluate a StrategySpec causally on a single day's features."""
    validate_rules(spec.rules)
    ctx = dict(features)
    ctx["has_position"] = bool(has_position)
    ctx["hold_days"] = int(hold_days)
    score = round(_compute_signal_score(spec, ctx), 6)

    params = spec.params if spec.params is not None else {}
    _validate_strategy_params(params)
    if has_position:
        unrealized = ctx.get("unrealized_return")
        stop_loss = params.get("stop_loss_pct")
        if stop_loss is not None and _is_finite_number(unrealized):
            if float(unrealized) <= -abs(float(stop_loss)):
                return {"action": "SELL", "score": score, "reason": "stop_loss"}

        take_profit = params.get("take_profit_pct")
        if take_profit is not None and _is_finite_number(unrealized):
            if float(unrealized) >= abs(float(take_profit)):
                return {"action": "SELL", "score": score, "reason": "take_profit"}

        sell_rule = spec.rules.get("sell")
        if sell_rule is not None:
            raw_min_hold = params.get("min_hold_days")
            min_hold = 0 if raw_min_hold is None else int(raw_min_hold)
            if int(hold_days) >= min_hold and evaluate_rule(sell_rule, ctx, params, prev_features):
                return {"action": "SELL", "score": score, "reason": "sell_rule"}

    buy_rule = spec.rules.get("buy")
    if buy_rule is not None and evaluate_rule(buy_rule, ctx, params, prev_features):
        return {"action": "BUY", "score": score, "reason": "buy_rule"}

    return {"action": "HOLD", "score": score, "reason": "no_signal"}


BUILTIN_STRATEGIES: dict[str, StrategySpec] = {
    "value_quality_margin_of_safety": StrategySpec(
        strategy_id="value_quality_margin_of_safety",
        name="Value Quality & Margin of Safety",
        style="value",
        market="CN",
        description=(
            "Medium/long-term value investing strategy targeting low PE/PB, high ROE, "
            "healthy leverage, and disciplined RSI entry; includes bar-only value discount "
            "fallback when fundamentals are unavailable."
        ),
        params={
            "max_pe": 22.0,
            "exit_pe": 35.0,
            "max_pb": 3.5,
            "min_roe": 0.10,
            "max_debt_ratio": 0.65,
            "sma_short_window": 3,
            "sma_long_window": 10,
            "rsi_window": 6,
            "zscore_window": 5,
            "max_entry_rsi": 60.0,
            "fallback_entry_z": -0.3,
            "min_hold_days": 5,
            "stop_loss_pct": 0.15,
            "take_profit_pct": 0.35,
            "max_weight": 0.25,
        },
        rules={
            "buy": {
                "any": [
                    {
                        "all": [
                            {"field": "has_fundamentals", "op": "==", "value": True},
                            {"field": "pe_ttm", "op": ">", "value": 0.0},
                            {"field": "pe_ttm", "op": "<=", "param": "max_pe"},
                            {"field": "pb", "op": "<=", "param": "max_pb"},
                            {"field": "roe", "op": ">=", "param": "min_roe"},
                            {"field": "debt_ratio", "op": "<=", "param": "max_debt_ratio"},
                            {"field": "rsi", "op": "<=", "param": "max_entry_rsi"},
                        ]
                    },
                    {
                        "all": [
                            {"field": "has_fundamentals", "op": "==", "value": False},
                            {"field": "zscore_close", "op": "<=", "param": "fallback_entry_z"},
                            {"field": "rsi", "op": "<=", "param": "max_entry_rsi"},
                        ]
                    },
                ]
            },
            "sell": {
                "any": [
                    {
                        "all": [
                            {"field": "has_fundamentals", "op": "==", "value": True},
                            {"field": "pe_ttm", "op": ">=", "param": "exit_pe"},
                        ]
                    },
                    {"field": "rsi", "op": ">=", "value": 78.0},
                    {
                        "all": [
                            {"field": "has_fundamentals", "op": "==", "value": False},
                            {"field": "zscore_close", "op": ">=", "value": 1.2},
                            {"field": "hold_days", "op": ">=", "param": "min_hold_days"},
                        ]
                    },
                ]
            },
            "ranking": {"field": "earnings_yield", "order": "desc"},
        },
        created_at="2026-11-26T00:00:00Z",
        source="builtin",
        version=1,
    ),
    "cn_daily_momentum_sentiment": StrategySpec(
        strategy_id="cn_daily_momentum_sentiment",
        name="CN Daily Momentum & News Sentiment",
        style="sentiment_hybrid",
        market="CN",
        description=(
            "Daily-frequency A-share momentum and news-sentiment hybrid strategy entering on "
            "positive trend/momentum with non-negative sentiment and exiting on sentiment reversal "
            "or momentum breakdown."
        ),
        params={
            "sma_short_window": 3,
            "sma_long_window": 6,
            "momentum_window": 3,
            "rsi_window": 6,
            "min_momentum": 0.003,
            "min_sentiment": 0.0,
            "negative_sentiment_exit": -0.4,
            "rsi_overbought": 82.0,
            "sentiment_lookback_days": 3,
            "stop_loss_pct": 0.06,
            "take_profit_pct": 0.15,
            "max_weight": 0.20,
        },
        rules={
            "buy": {
                "all": [
                    {"field": "sentiment_score", "op": ">=", "param": "min_sentiment"},
                    {
                        "any": [
                            {"field": "sma_short", "op": ">", "ref": "sma_long"},
                            {"field": "momentum_return", "op": ">=", "param": "min_momentum"},
                            {"field": "sentiment_score", "op": ">=", "value": 0.5},
                        ]
                    },
                ]
            },
            "sell": {
                "any": [
                    {"field": "sentiment_score", "op": "<=", "param": "negative_sentiment_exit"},
                    {
                        "all": [
                            {"field": "close", "op": "<", "ref": "sma_short"},
                            {"field": "momentum_return", "op": "<", "value": 0.0},
                        ]
                    },
                    {"field": "rsi", "op": ">=", "param": "rsi_overbought"},
                ]
            },
            "ranking": {"field": "momentum_return", "order": "desc"},
        },
        created_at="2026-11-26T00:00:00Z",
        source="builtin",
        version=1,
    ),
    "cn_mean_reversion_daily": StrategySpec(
        strategy_id="cn_mean_reversion_daily",
        name="CN Daily Mean Reversion (Z-Score + RSI)",
        style="quant_daily",
        market="CN",
        description=(
            "Short-term daily quantitative mean-reversion strategy buying oversold pullbacks "
            "and exiting on mean reversion or overbought bounces while avoiding severe negative news."
        ),
        params={
            "zscore_window": 5,
            "rsi_window": 6,
            "entry_zscore": -0.8,
            "exit_zscore": 0.6,
            "rsi_oversold": 35.0,
            "rsi_overbought": 72.0,
            "min_sentiment": -0.5,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.08,
            "max_weight": 0.20,
        },
        rules={
            "buy": {
                "all": [
                    {"field": "sentiment_score", "op": ">", "param": "min_sentiment"},
                    {
                        "any": [
                            {"field": "zscore_close", "op": "<=", "param": "entry_zscore"},
                            {"field": "rsi", "op": "<=", "param": "rsi_oversold"},
                        ]
                    },
                ]
            },
            "sell": {
                "any": [
                    {"field": "zscore_close", "op": ">=", "param": "exit_zscore"},
                    {"field": "rsi", "op": ">=", "param": "rsi_overbought"},
                ]
            },
            "ranking": {"field": "zscore_close", "order": "asc"},
        },
        created_at="2026-11-26T00:00:00Z",
        source="builtin",
        version=1,
    ),
}


def _ensure_duckdb_table(con: Any) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS quant_strategies (
            strategy_id VARCHAR PRIMARY KEY,
            name VARCHAR,
            style VARCHAR,
            market VARCHAR,
            description VARCHAR,
            params_json VARCHAR,
            rules_json VARCHAR,
            created_at VARCHAR,
            source VARCHAR,
            version INTEGER,
            spec_json VARCHAR
        )
        """
    )


def _upsert_duckdb(con: Any, spec: StrategySpec) -> None:
    _ensure_duckdb_table(con)
    payload = spec.to_dict()
    con.execute(
        """
        INSERT OR REPLACE INTO quant_strategies (
            strategy_id, name, style, market, description,
            params_json, rules_json, created_at, source, version, spec_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            spec.strategy_id,
            spec.name,
            spec.style,
            spec.market,
            spec.description,
            json.dumps(spec.params, ensure_ascii=False, sort_keys=True, allow_nan=False),
            json.dumps(spec.rules, ensure_ascii=False, sort_keys=True, allow_nan=False),
            spec.created_at,
            spec.source,
            spec.version,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
        ],
    )


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    payload = (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = (
        path.parent
        / f".tmp_{path.stem}_{os.getpid()}_{threading.get_ident()}_{uuid4().hex[:8]}.json"
    )
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


def ensure_builtin_strategies(
    strategy_dir: str | Path | None = None,
    *,
    overwrite: bool = False,
    con: Any | None = None,
) -> list[StrategySpec]:
    """Ensure all built-in strategies exist in the JSON registry (and optional DuckDB)."""
    target_dir = Path(strategy_dir) if strategy_dir is not None else _default_strategy_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    result: list[StrategySpec] = []
    for sid, spec in BUILTIN_STRATEGIES.items():
        file_path = target_dir / f"{sid}.json"
        if overwrite or not file_path.exists():
            _atomic_write_json(file_path, spec.to_dict())
            stored = spec
        else:
            stored = StrategySpec.from_dict(_loads_strict_json(file_path.read_text(encoding="utf-8")))
        if con is not None:
            _upsert_duckdb(con, stored)
        result.append(stored)
    return result


def save_strategy(
    spec: StrategySpec | dict[str, Any],
    strategy_dir: str | Path | None = None,
    *,
    con: Any | None = None,
) -> StrategySpec:
    """Validate and atomically persist a StrategySpec to JSON (and optional DuckDB)."""
    parsed = spec if isinstance(spec, StrategySpec) else StrategySpec.from_dict(spec)
    # Re-validate explicitly in case StrategySpec fields were mutated after construction
    validated = StrategySpec.from_dict(parsed.to_dict())
    target_dir = Path(strategy_dir) if strategy_dir is not None else _default_strategy_dir()
    file_path = target_dir / f"{validated.strategy_id}.json"
    _atomic_write_json(file_path, validated.to_dict())
    if con is not None:
        _upsert_duckdb(con, validated)
    return validated


def get_strategy(
    strategy_id: str,
    strategy_dir: str | Path | None = None,
    *,
    con: Any | None = None,
) -> StrategySpec:
    """Load a strategy by ID from JSON store, built-ins, or optional DuckDB."""
    sid = _validate_strategy_id(strategy_id)
    target_dir = Path(strategy_dir) if strategy_dir is not None else _default_strategy_dir()
    file_path = target_dir / f"{sid}.json"
    if file_path.exists():
        return StrategySpec.from_dict(_loads_strict_json(file_path.read_text(encoding="utf-8")))
    if sid in BUILTIN_STRATEGIES:
        ensure_builtin_strategies(target_dir, overwrite=False, con=con)
        if file_path.exists():
            return StrategySpec.from_dict(_loads_strict_json(file_path.read_text(encoding="utf-8")))
        return BUILTIN_STRATEGIES[sid]
    if con is not None:
        try:
            row = con.execute(
                "SELECT spec_json FROM quant_strategies WHERE strategy_id = ?",
                [sid],
            ).fetchone()
            if row and row[0]:
                return StrategySpec.from_dict(_loads_strict_json(row[0]))
        except Exception:
            pass
    raise StrategyNotFoundError(f"Strategy not found: {sid}")


def list_strategies(
    strategy_dir: str | Path | None = None,
    *,
    style: str | None = None,
    market: str | None = None,
    ensure_builtins: bool = True,
    con: Any | None = None,
) -> list[StrategySpec]:
    """List persisted strategies, optionally filtering by style or market."""
    target_dir = Path(strategy_dir) if strategy_dir is not None else _default_strategy_dir()
    if ensure_builtins:
        ensure_builtin_strategies(target_dir, overwrite=False, con=con)

    specs: list[StrategySpec] = []
    if target_dir.exists():
        for path in sorted(target_dir.glob("*.json")):
            if path.name.startswith("."):
                continue
            data = _loads_strict_json(path.read_text(encoding="utf-8"))
            spec = StrategySpec.from_dict(data)
            if style is not None and spec.style != style.strip():
                continue
            if market is not None:
                mkt = market.strip().upper()
                if spec.market not in {mkt, "BOTH", "GLOBAL"}:
                    continue
            specs.append(spec)
    return specs


def delete_strategy(
    strategy_id: str,
    strategy_dir: str | Path | None = None,
    *,
    allow_builtin: bool = False,
    con: Any | None = None,
) -> bool:
    """Delete a strategy from JSON store (and optional DuckDB). Protects built-ins by default."""
    sid = _validate_strategy_id(strategy_id)
    if sid in BUILTIN_STRATEGIES and not allow_builtin:
        raise ValueError(f"Cannot delete built-in strategy {sid!r} without allow_builtin=True")
    target_dir = Path(strategy_dir) if strategy_dir is not None else _default_strategy_dir()
    file_path = target_dir / f"{sid}.json"
    deleted = False
    if file_path.exists():
        file_path.unlink()
        deleted = True
    if con is not None:
        try:
            _ensure_duckdb_table(con)
            res = con.execute(
                "DELETE FROM quant_strategies WHERE strategy_id = ? RETURNING strategy_id",
                [sid],
            ).fetchall()
            if res:
                deleted = True
        except Exception:
            pass
    return deleted
