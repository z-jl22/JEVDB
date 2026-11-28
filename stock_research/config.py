"""Environment-driven configuration for the peripheral JEVDB stock research app."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from stock_research.settings_store import read_overrides

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_DATA_PATH = Path("stock_research/data/baseline")
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    runtime_dir: Path
    cache_dir: Path
    db_path: Path
    ledger_db_path: Path
    strategy_dir: Path
    baseline_data_dir: Path
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    finnhub_api_key: str
    sec_user_agent: str
    shim_host: str
    shim_port: int
    llm_batch_k: int
    llm_threads: int
    llm_timeout: float


def get_settings(ensure_dirs: bool = True, *, use_saved: bool = True) -> Settings:
    """Resolve environment defaults and saved workbench overrides."""
    repo_root = Path(os.getenv("JEVDB_REPO_ROOT", str(DEFAULT_REPO_ROOT)))
    runtime_dir = Path(
        os.getenv("STOCK_RESEARCH_RUNTIME_DIR", str(repo_root / ".tmp" / "stock_research"))
    )
    cache_dir = Path(os.getenv("STOCK_RESEARCH_CACHE_DIR", str(runtime_dir / "cache")))
    db_path = Path(
        os.getenv("STOCK_RESEARCH_DB_PATH", str(runtime_dir / "market_research.duckdb"))
    )
    ledger_db_path = Path(
        os.getenv("STOCK_RESEARCH_LEDGER_DB_PATH", str(runtime_dir / "paper_ledger.duckdb"))
    )
    strategy_dir = Path(
        os.getenv("STOCK_RESEARCH_STRATEGY_DIR", str(runtime_dir / "strategies"))
    )
    baseline_data_dir = Path(
        os.getenv(
            "STOCK_RESEARCH_BASELINE_DATA_DIR",
            os.getenv("STOCK_RESEARCH_STATIC_EXAMPLES_DIR", str(repo_root / BASELINE_DATA_PATH)),
        )
    )

    if ensure_dirs:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        strategy_dir.mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    defaults = Settings(
        repo_root=repo_root,
        runtime_dir=runtime_dir,
        cache_dir=cache_dir,
        db_path=db_path,
        ledger_db_path=ledger_db_path,
        strategy_dir=strategy_dir,
        baseline_data_dir=baseline_data_dir,
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        deepseek_base_url=os.getenv(
            "DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL
        ).rstrip("/"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "").strip() or DEFAULT_DEEPSEEK_MODEL,
        finnhub_api_key=os.getenv("FINNHUB_API_KEY", "").strip(),
        sec_user_agent=os.getenv(
            "SEC_USER_AGENT", "JEVDBStockResearch/0.1 (research@example.org)"
        ).strip(),
        shim_host=os.getenv("JEVDB_SHIM_HOST", "127.0.0.1").strip(),
        shim_port=int(os.getenv("JEVDB_SHIM_PORT", "18089")),
        llm_batch_k=int(os.getenv("JEVDB_LLM_BATCH_K", "16")),
        llm_threads=int(os.getenv("JEVDB_LLM_THREADS", "4")),
        llm_timeout=float(os.getenv("JEVDB_LLM_TIMEOUT", "120.0")),
    )
    if not use_saved:
        return defaults
    overrides = read_overrides(runtime_dir)
    if "deepseek_api_key" not in overrides and overrides.get("deepseek_base_url", defaults.deepseek_base_url) != defaults.deepseek_base_url:
        overrides["deepseek_api_key"] = ""
    return replace(defaults, **overrides)
