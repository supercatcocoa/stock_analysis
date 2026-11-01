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
    # 判定文（日本語 JSON）の出力上限。切れた場合は自動で 2 倍にして 1 回だけ再試行する
    claude_max_tokens: int = field(default_factory=lambda: int(os.getenv("CLAUDE_MAX_TOKENS", "8000")))

    # --- 保存先 ---
    # Turso を使う場合（推奨）: URL とトークンを設定すると Turso に保存する
    turso_database_url: str = field(default_factory=lambda: os.getenv("TURSO_DATABASE_URL", ""))
    turso_auth_token: str = field(default_factory=lambda: os.getenv("TURSO_AUTH_TOKEN", ""))
    # Turso の読み取り用に手元へ置く複製ファイル（消えても次回接続時に再同期される）
    turso_replica_path: Path = field(
        default_factory=lambda: Path(os.getenv("TURSO_REPLICA_PATH", str(PROJECT_ROOT / "data" / "turso_replica.db")))
    )
    # Turso 未設定時のローカル SQLite
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "stock_app.db")))
    )

    # --- EDINET DB ---
    edinetdb_base_url: str = "https://edinetdb.jp/v1"
    analysis_years: int = 3  # 集計する決算期数

    # --- 資本コストの前提（ノートブックと同じ値） ---
    market_index: str = "^N225"
    # 安全資産利回り（日本10年国債）。.env の RISK_FREE_RATE=0.028 のように上書き可
    risk_free_rate: float = field(default_factory=lambda: float(os.getenv("RISK_FREE_RATE", "0.025")))
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
