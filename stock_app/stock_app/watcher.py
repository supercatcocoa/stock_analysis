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
from .rebound import BottomCheck, check_bottom  # noqa: E402

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
    reasons: list[str]  # 買いシグナル（GC / 押し目）
    oversold: bool = False  # 本日 RSI が売られすぎ水準を下回っている
    oversold_cross: bool = False  # 本日、売られすぎ水準を新たに下抜けた（前日は水準以上）
    rebound_started: bool = False  # 本日、下げ止まり監視を開始した
    bottom: "BottomCheck | None" = None  # 下げ止まり監視中の判定結果
    bottom_notified: bool = False  # 本日、下げ止まりサインとして通知対象になった

    @property
    def is_buy(self) -> bool:
        return bool(self.reasons)

    @property
    def should_notify(self) -> bool:
        return self.is_buy or self.rebound_started or self.bottom_notified


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["SMA_S"] = df["Close"].rolling(settings.sma_short).mean()
    df["SMA_L"] = df["Close"].rolling(settings.sma_long).mean()
    df["RSI"] = calculate_rsi(df["Close"], settings.rsi_window)
    return df


def detect_signal(ticker: str, df: pd.DataFrame) -> Signal | None:
    """ノートブックと同じ判定（短期GC / 上昇トレンド中の押し目）と、売られすぎかどうか。"""
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

    return Signal(
        ticker=ticker,
        date=df.index[-1].strftime("%Y-%m-%d"),
        close=float(last["Close"]),
        rsi=float(last["RSI"]),
        sma_short=float(last["SMA_S"]),
        sma_long=float(last["SMA_L"]),
        reasons=reasons,
        oversold=bool(last["RSI"] < settings.rsi_oversold),
        oversold_cross=bool(prev["RSI"] >= settings.rsi_oversold > last["RSI"]),
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


def build_message(sig: Signal, item: dict, evaluation: dict | None, rebound: dict | None = None) -> str:
    titles = []
    if sig.is_buy:
        titles.append("📈 買いチャンス検知")
    if sig.bottom_notified:
        titles.append("🔄 下げ止まりサイン")
    if sig.rebound_started:
        titles.append("⚠️ 売られすぎ検知 → 下げ止まり監視を開始")
    lines = [
        "【" + " ／ ".join(titles) + "】",
        f"・企業名: **{item['name']}** ({sig.ticker})",
        f"・終値: ¥{sig.close:,.1f}（{sig.date}）",
        f"・RSI({settings.rsi_window}): {sig.rsi:.1f} / SMA{settings.sma_short}: {sig.sma_short:,.1f} / "
        f"SMA{settings.sma_long}: {sig.sma_long:,.1f}",
    ]
    if sig.is_buy:
        lines.append("・シグナル: " + " / ".join(sig.reasons))
    if sig.bottom_notified and sig.bottom and rebound:
        b = sig.bottom
        lines += [
            f"・下げ止まりサイン {b.score}/5（{settings.bottom_min_signals}つ以上で通知）",
            *[f"　✅ {m}" for m in b.met],
            f"・監視開始 {rebound['started_date']}（¥{rebound['start_close']:,.1f}, RSI {rebound['start_rsi']:.1f}）"
            f"から {b.days_since_start} 営業日",
            f"・最安値 ¥{b.low_close:,.1f}（{b.low_date}, 開始時比 {b.drawdown_pct:+.1f}%）"
            f" → 現在 開始時比 {b.change_from_start_pct:+.1f}%",
        ]
    if sig.rebound_started:
        lines.append(
            f"・RSI が {settings.rsi_oversold:.0f} を割りました。すぐには買わず、下げ止まりサインが"
            f" {settings.bottom_min_signals} つそろった時点で改めて通知します（最長 {settings.oversold_watch_days} 営業日）"
        )
    if evaluation:
        lines.append(f"・財務判定: {evaluation['verdict']}（スコア {evaluation['score']}, {evaluation['evaluated_at'][:10]}）")
        lines.append(f"> {evaluation['summary']}")
    lines.append("※ テクニカル条件の機械的な検知です。投資判断はご自身で。")
    return "\n".join(lines)


def _update_rebound_state(conn, sig: Signal, item: dict, df: pd.DataFrame, persist: bool) -> dict | None:
    """売られすぎ → 下げ止まり監視の状態を進める。persist=False（dry-run）なら DB を変えない。"""
    entry = db.get_rebound(conn, sig.ticker)
    watching = entry is not None and entry["status"] == "watching"

    if watching:
        bottom = check_bottom(add_indicators(df), entry)
        if bottom is not None:
            sig.bottom = bottom
            if bottom.is_bottom:
                sig.bottom_notified = True
                if persist:
                    db.update_rebound(conn, sig.ticker, status="bottomed", ended_date=sig.date,
                                      last_score=bottom.score, low_close=bottom.low_close, low_date=bottom.low_date)  # fmt: skip
            elif bottom.days_since_start >= settings.oversold_watch_days:
                print(f"  ⌛ {item['name']}: {settings.oversold_watch_days} 営業日たっても下げ止まらず監視終了")
                if persist:
                    db.update_rebound(conn, sig.ticker, status="expired", ended_date=sig.date,
                                      last_score=bottom.score, low_close=bottom.low_close, low_date=bottom.low_date)  # fmt: skip
            elif persist:
                db.update_rebound(conn, sig.ticker, last_score=bottom.score,
                                  low_close=bottom.low_close, low_date=bottom.low_date)  # fmt: skip
    # 初めての銘柄は売られすぎなら開始。監視を終えた銘柄は、新たに水準を下抜けた日だけ再開する
    elif (entry is None and sig.oversold) or (entry is not None and sig.oversold_cross):
        sig.rebound_started = True
        if persist:
            db.start_rebound(conn, item, sig.date, sig.close, sig.rsi)
        entry = {"started_date": sig.date, "start_close": sig.close, "start_rsi": sig.rsi}
    return entry


def run_watch(dry_run: bool = False, force_notify: bool = False, price_loader=download_prices, sender=None,
              db_path=None) -> list[Signal]:  # fmt: skip
    """ウォッチリスト全銘柄をチェック。dry_run=True なら Discord に送らず、監視状態も変えない。"""
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

            already = db.signal_already_sent(conn, sig.ticker, sig.date)
            # 同じ日の再実行では監視状態を二重に進めない
            rebound = _update_rebound_state(conn, sig, item, df, persist=not dry_run and not already)
            if sig.bottom and not sig.bottom_notified:
                print(f"  👀 {item['name']}: 下げ止まり監視中 {sig.bottom.score}/5 "
                      f"(RSI {sig.rsi:.1f}, {sig.bottom.days_since_start}営業日目)")  # fmt: skip

            if force_notify and not sig.should_notify:
                sig.reasons = ["（テスト送信）"]
            if not sig.should_notify:
                print(f"  {item['name']} ({item['ticker']}): 条件外 (RSI {sig.rsi:.1f})")
                continue

            hits.append(sig)
            if already and not force_notify:
                print(f"  🔁 {item['name']}: {sig.date} のシグナルは通知済み")
                continue

            msg = build_message(sig, item, db.latest_evaluation(conn, item["edinet_code"]), rebound)
            kinds = [k for k, v in (("買い", sig.is_buy), ("下げ止まり", sig.bottom_notified),
                                    ("売られすぎ→監視開始", sig.rebound_started)) if v]  # fmt: skip
            print(f"  🎯 {item['name']} ({sig.ticker}): " + " / ".join(kinds))
            if not dry_run:
                chart = generate_chart(df, f"{sig.ticker} - last 3 months")
                sender(msg, file=chart, filename=f"{sig.ticker}_chart.png")
            reasons = sig.reasons + (["下げ止まり"] if sig.bottom_notified else []) + (
                ["売られすぎ→監視開始"] if sig.rebound_started else [])  # fmt: skip
            db.record_signal(conn, sig.ticker, sig.date, reasons, sig.close, sig.rsi, notified=not dry_run)

    print(f"✨ 完了。通知対象 {len(hits)} 件")
    return hits
