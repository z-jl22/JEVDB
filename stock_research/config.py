"""Environment-driven configuration for the peripheral JEVDB stock research app."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_REPO_ROOT = Path("/home/tao/JEVDB")


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    runtime_dir: Path
    cache_dir: Path
    db_path: Path
    ledger_db_path: Path
    strategy_dir: Path
    static_examples_dir: Path
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


def get_settings(ensure_dirs: bool = True) -> Settings:
    """Build runtime settings dynamically from environment variables."""
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
    static_examples_dir = Path(
        os.getenv(
            "STOCK_RESEARCH_STATIC_EXAMPLES_DIR",
            str(repo_root / "examples" / "stock_trading"),
        )
    )

    if ensure_dirs:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        strategy_dir.mkdir(parents=True, exist_ok=True)
        db_path.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        repo_root=repo_root,
        runtime_dir=runtime_dir,
        cache_dir=cache_dir,
        db_path=db_path,
        ledger_db_path=ledger_db_path,
        strategy_dir=strategy_dir,
        static_examples_dir=static_examples_dir,
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        deepseek_base_url=os.getenv(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
        ).rstrip("/"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash").strip(),
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
