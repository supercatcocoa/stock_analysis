"""アプリ全体の設定。値は .env もしくは環境変数から読み込む。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv が無くても動くように
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    # --- API キー類 ---
    edinetdb_api_key: str = field(default_factory=lambda: os.getenv("EDINETDB_API_KEY", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    discord_webhook_url: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", ""))

    # --- Claude ---
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"))
    claude_max_tokens: int = 2000

    # --- 保存先 ---
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "stock_app.db")))
    )

    # --- EDINET DB ---
    edinetdb_base_url: str = "https://edinetdb.jp/v1"
    analysis_years: int = 3  # 集計する決算期数

    # --- 資本コストの前提（ノートブックと同じ値） ---
    market_index: str = "^N225"
    risk_free_rate: float = 0.005
    market_risk_premium: float = 0.06
    tax_rate: float = 0.30
    default_cost_of_debt: float = 0.015

    # --- テクニカル判定 ---
    sma_short: int = 5
    sma_long: int = 25
    rsi_window: int = 14
    rsi_pullback_max: float = 50.0  # 上昇トレンド中の押し目判定に使う RSI 上限
    rsi_oversold: float = 30.0  # 売られすぎ判定


settings = Settings()
