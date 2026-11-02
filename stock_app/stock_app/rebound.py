"""売られすぎ銘柄の「下げ止まり」監視。

RSI が売られすぎ水準を割った銘柄を監視リスト（oversold_watch テーブル）に入れ、
翌日以降、次の 5 つの下げ止まりサインのうち settings.bottom_min_signals 個以上が
そろった日に通知する。

  1. RSI 回復        : 監視開始後に売られすぎ水準を割り、現在は上回っている
  2. 安値更新ストップ : 監視開始後の最安値から 3 営業日以上、安値を更新していない
  3. 5 日線回復       : 終値が 5 日移動平均を上回った
  4. 下ヒゲ（ハンマー）: 直近 3 日に、下ヒゲが実体の 2 倍以上かつ値幅の半分以上ある足が出た
  5. 強気ダイバージェンス: 監視開始時より株価は安値を更新したのに、RSI は開始時より高い
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .config import settings

STOP_DAYS = 3  # 何営業日安値を更新しなければ「止まった」とみなすか
HAMMER_LOOKBACK = 3


@dataclass
class BottomCheck:
    met: list[str] = field(default_factory=list)
    days_since_start: int = 0
    low_close: float = 0.0
    low_date: str = ""
    drawdown_pct: float = 0.0  # 監視開始時の終値からの下落率（最安値時点）
    change_from_start_pct: float = 0.0  # 監視開始時の終値から現在までの騰落率

    @property
    def score(self) -> int:
        return len(self.met)

    @property
    def is_bottom(self) -> bool:
        return self.score >= settings.bottom_min_signals


def _is_hammer(row) -> bool:
    try:
        o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
    except (KeyError, TypeError, ValueError):
        return False
    rng = h - l
    if rng <= 0:
        return False
    body = abs(c - o)
    lower = min(o, c) - l
    return lower >= 2 * max(body, rng * 0.02) and lower >= rng * 0.5


def check_bottom(df: pd.DataFrame, entry: dict) -> BottomCheck | None:
    """df は add_indicators 済みの日足。entry は oversold_watch の行。開始日の翌日以降のみ判定する。"""
    since = df[df.index.strftime("%Y-%m-%d") >= entry["started_date"]]
    if len(since) < 2:
        return None  # 監視開始日当日はまだ判定しない

    last = since.iloc[-1]
    low_pos = int(since["Close"].values.argmin())
    low_row = since.iloc[low_pos]
    start_close = float(entry["start_close"])
    start_rsi = float(entry["start_rsi"])
    oversold = settings.rsi_oversold

    met = []
    if last["RSI"] >= oversold and since["RSI"].min() < oversold:
        met.append(f"RSI が {oversold:.0f} を上回って回復（{last['RSI']:.1f}）")
    days_from_low = len(since) - 1 - low_pos
    if days_from_low >= STOP_DAYS:
        met.append(f"安値更新が {days_from_low} 営業日止まっている（最安値 ¥{low_row['Close']:,.1f}）")
    if pd.notna(last["SMA_S"]) and last["Close"] > last["SMA_S"]:
        met.append(f"終値が {settings.sma_short} 日線を回復")
    recent = since.iloc[-HAMMER_LOOKBACK:]
    hammer_days = [d.strftime("%m/%d") for d, r in recent.iterrows() if _is_hammer(r)]
    if hammer_days:
        met.append(f"下ヒゲの長い足（ハンマー）が出現（{', '.join(hammer_days)}）")
    if low_row["Close"] < start_close and low_row["RSI"] > start_rsi:
        met.append(
            f"強気ダイバージェンス（株価は開始時より安値更新、RSI は {start_rsi:.1f}→{low_row['RSI']:.1f} と切り上げ）"
        )

    return BottomCheck(
        met=met,
        days_since_start=len(since) - 1,
        low_close=float(low_row["Close"]),
        low_date=since.index[low_pos].strftime("%Y-%m-%d"),
        drawdown_pct=round((float(low_row["Close"]) / start_close - 1) * 100, 2),
        change_from_start_pct=round((float(last["Close"]) / start_close - 1) * 100, 2),
    )
