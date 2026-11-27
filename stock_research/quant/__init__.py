"""Quant strategy, backtesting, risk analytics, and paper-trading engine."""

from .backtest import (
    BacktestResult,
    ComparisonResult,
    DEFAULT_CN_RULE,
    DEFAULT_US_RULE,
    MarketRule,
    compare_strategies,
    load_bars_from_csv,
    load_bars_from_duckdb,
    run_backtest,
)
from .ledger import PaperAccountService
from .risk import compute_portfolio_risk, compute_risk_metrics
from .strategies import (
    BUILTIN_STRATEGIES,
    StrategyNotFoundError,
    StrategySpec,
    delete_strategy,
    ensure_builtin_strategies,
    evaluate_rule,
    evaluate_strategy_signals,
    get_strategy,
    list_strategies,
    save_strategy,
    validate_rules,
)

__all__ = [
    "BUILTIN_STRATEGIES",
    "BacktestResult",
    "ComparisonResult",
    "DEFAULT_CN_RULE",
    "DEFAULT_US_RULE",
    "MarketRule",
    "PaperAccountService",
    "StrategyNotFoundError",
    "StrategySpec",
    "compare_strategies",
    "compute_portfolio_risk",
    "compute_risk_metrics",
    "delete_strategy",
    "ensure_builtin_strategies",
    "evaluate_rule",
    "evaluate_strategy_signals",
    "get_strategy",
    "list_strategies",
    "load_bars_from_csv",
    "load_bars_from_duckdb",
    "run_backtest",
    "save_strategy",
    "validate_rules",
]
