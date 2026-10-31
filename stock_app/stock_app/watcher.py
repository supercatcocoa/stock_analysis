"""機能2: 優良判定銘柄の株価を毎日チェックし、買いシグナルを Discord に送る。"""
from __future__ import annotations

import io
from dataclasses import dataclass

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from . import db  # noqa: E402
from .config import settings  # noqa: E402

try:  # 日本語フォント（無くても動く）
    import japanize_matplotlib  # noqa: F401
except Exception:  # noqa: BLE001
    pass


def calculate_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window=window).mean()
    loss = (-delta.clip(upper=0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


@dataclass
class Signal:
    ticker: str
    date: str
    close: float
    rsi: float
    sma_short: float
    sma_long: float
    reasons: list[str]

    @property
    def is_buy(self) -> bool:
        return bool(self.reasons)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["SMA_S"] = df["Close"].rolling(settings.sma_short).mean()
    df["SMA_L"] = df["Close"].rolling(settings.sma_long).mean()
    df["RSI"] = calculate_rsi(df["Close"], settings.rsi_window)
    return df


def detect_signal(ticker: str, df: pd.DataFrame) -> Signal | None:
    """ノートブックと同じ判定（短期GC / 上昇トレンド中の押し目）＋売られすぎ。"""
    df = df.dropna(subset=["Close"])
    if len(df) < settings.sma_long + 50:
        return None
    df = add_indicators(df)
    last, prev = df.iloc[-1], df.iloc[-2]

    reasons = []
    if prev["SMA_S"] < prev["SMA_L"] and last["SMA_S"] > last["SMA_L"]:
        reasons.append(f"ゴールデンクロス (SMA{settings.sma_short}がSMA{settings.sma_long}を上抜け)")
    if last["Close"] > last["SMA_L"] and last["RSI"] < settings.rsi_pullback_max:
        reasons.append(f"上昇トレンド中の押し目 (終値>SMA{settings.sma_long} かつ RSI<{settings.rsi_pullback_max:.0f})")
    if last["RSI"] < settings.rsi_oversold:
        reasons.append(f"売られすぎ (RSI<{settings.rsi_oversold:.0f})")

    return Signal(
        ticker=ticker,
        date=df.index[-1].strftime("%Y-%m-%d"),
        close=float(last["Close"]),
        rsi=float(last["RSI"]),
        sma_short=float(last["SMA_S"]),
        sma_long=float(last["SMA_L"]),
        reasons=reasons,
    )


def generate_chart(df: pd.DataFrame, title: str) -> io.BytesIO:
    df = add_indicators(df).iloc[-60:]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(df.index, df["Close"], label="Close", color="black")
    ax1.plot(df.index, df["SMA_S"], label=f"SMA {settings.sma_short}", color="blue", alpha=0.7)
    ax1.plot(df.index, df["SMA_L"], label=f"SMA {settings.sma_long}", color="orange", alpha=0.7)
    ax1.set_title(title)
    ax1.legend()
    ax1.grid(True, alpha=0.4)
    ax2.plot(df.index, df["RSI"], color="purple")
    ax2.axhline(settings.rsi_oversold, color="gray", ls="--", lw=0.8)
    ax2.axhline(70, color="gray", ls="--", lw=0.8)
    ax2.set_ylabel("RSI")
    ax2.set_ylim(0, 100)
    ax2.grid(True, alpha=0.4)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def download_prices(tickers: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    data = yf.download(tickers, period="6mo", group_by="ticker", auto_adjust=False, progress=False)
    out = {}
    for t in tickers:
        try:
            df = data[t] if isinstance(data.columns, pd.MultiIndex) else data
            out[t] = df.dropna(subset=["Close"]).copy()
        except KeyError:
            print(f"⚠️ {t}: 株価が取得できませんでした")
    return out


def build_message(sig: Signal, item: dict, evaluation: dict | None) -> str:
    lines = [
        "【📈 買いチャンス検知】",
        f"・企業名: **{item['name']}** ({sig.ticker})",
        f"・終値: ¥{sig.close:,.1f}（{sig.date}）",
        f"・RSI({settings.rsi_window}): {sig.rsi:.1f} / SMA{settings.sma_short}: {sig.sma_short:,.1f} / "
        f"SMA{settings.sma_long}: {sig.sma_long:,.1f}",
        "・シグナル: " + " / ".join(sig.reasons),
    ]
    if evaluation:
        lines.append(f"・財務判定: {evaluation['verdict']}（スコア {evaluation['score']}, {evaluation['evaluated_at'][:10]}）")
        lines.append(f"> {evaluation['summary']}")
    lines.append("※ テクニカル条件の機械的な検知です。投資判断はご自身で。")
    return "\n".join(lines)


def run_watch(dry_run: bool = False, force_notify: bool = False, price_loader=download_prices, sender=None,
              db_path=None) -> list[Signal]:  # fmt: skip
    """ウォッチリスト全銘柄をチェック。dry_run=True なら Discord に送らない。"""
    if sender is None:
        from .discord import send as sender

    with db.connect(db_path) as conn:
        items = db.active_watchlist(conn)
        if not items:
            print("ℹ️ ウォッチリストが空です。先に analyze で優良企業を登録してください。")
            return []
        print(f"🚀 {len(items)} 銘柄をチェックします…")
        prices = price_loader([i["ticker"] for i in items])
        hits: list[Signal] = []

        for item in items:
            df = prices.get(item["ticker"])
            sig = detect_signal(item["ticker"], df) if df is not None else None
            if sig is None:
                print(f"  {item['name']} ({item['ticker']}): データ不足のためスキップ")
                continue
            if force_notify and not sig.reasons:
                sig.reasons = ["（テスト送信）"]
            if not sig.is_buy:
                print(f"  {item['name']} ({item['ticker']}): 条件外 (RSI {sig.rsi:.1f})")
                continue

            hits.append(sig)
            if db.signal_already_sent(conn, sig.ticker, sig.date) and not force_notify:
                print(f"  🔁 {item['name']}: {sig.date} のシグナルは通知済み")
                continue

            msg = build_message(sig, item, db.latest_evaluation(conn, item["edinet_code"]))
            print(f"  🎯 {item['name']} ({sig.ticker}): " + " / ".join(sig.reasons))
            if not dry_run:
                chart = generate_chart(df, f"{sig.ticker} - last 3 months")
                sender(msg, chart, f"{sig.ticker}_chart.png")
            db.record_signal(conn, sig.ticker, sig.date, sig.reasons, sig.close, sig.rsi, notified=not dry_run)

    print(f"✨ 完了。シグナル {len(hits)} 件")
    return hits
