"""財務諸表の集計（ノートブックの操作1をベースに整理）とベータ / WACC の推定。"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import settings

QUARTER_KEYWORDS = ("Q1", "Q2", "Q3", "Q4", "QUARTER", "HALF", "H1")


def _num(v) -> float:
    try:
        f = float(v)
        return 0.0 if math.isnan(f) else f
    except (TypeError, ValueError):
        return 0.0


def _oku(v) -> float:
    return round(_num(v) / 1e8, 2)


def filter_full_year(records: list[dict], years: int) -> list[dict]:
    """通期レコードのみを新しい順に years 件取り出す。"""
    full = []
    for r in records:
        period_type = str(r.get("period_type") or r.get("term") or "").upper()
        if any(kw in period_type for kw in QUARTER_KEYWORDS):
            continue
        full.append(r)
    full.sort(key=lambda x: str(x.get("fiscal_year") or x.get("period_end") or ""), reverse=True)

    # 同じ年度が重複する場合は先頭（連結優先で返ってくる想定）を採用
    seen, uniq = set(), []
    for r in full:
        p = str(r.get("fiscal_year") or r.get("period_end"))
        if p not in seen:
            seen.add(p)
            uniq.append(r)
    return uniq[:years]


def interest_bearing_debt(r: dict) -> float:
    """有利子負債。明細があれば合算し、無ければ負債合計で代用（ノートブックの近似と同じ）。"""
    direct = _num(r.get("interest_bearing_debt") or r.get("total_debt"))
    if direct > 0:
        return direct
    ibd = _num(r.get("ibd_current")) + _num(r.get("ibd_noncurrent"))
    if ibd > 0:
        return ibd
    parts = sum(
        _num(r.get(k))
        for k in ("short_term_loans", "long_term_loans", "current_portion_lt_loans", "bonds_payable", "commercial_papers")
    )
    if parts > 0:
        return parts
    total_assets, net_assets = _num(r.get("total_assets")), _num(r.get("net_assets"))
    return _num(r.get("total_liabilities")) or max(0.0, total_assets - net_assets)


def build_financial_rows(edinet_code: str, records: list[dict], years: int, beta: float) -> list[dict]:
    """年度ごとの集計行を作る。DB の financials テーブルと同じ列構成。"""
    capm = settings.risk_free_rate + beta * settings.market_risk_premium
    rows = []
    for r in filter_full_year(records, years):
        revenue = _num(r.get("revenue"))
        op_income = _num(r.get("operating_income"))
        net_income = _num(r.get("net_income"))
        total_assets = _num(r.get("total_assets"))
        net_assets = _num(r.get("net_assets") or r.get("total_equity"))
        cf_op, cf_inv = _num(r.get("cf_operating")), _num(r.get("cf_investing"))

        debt = interest_bearing_debt(r)
        equity = net_assets if net_assets > 0 else 1.0
        total_capital = equity + debt
        w_e = equity / total_capital if total_capital > 0 else 1.0
        w_d = debt / total_capital if total_capital > 0 else 0.0

        interest = _num(r.get("interest_expense"))
        cost_of_debt = interest / debt if interest > 0 and debt > 0 else settings.default_cost_of_debt
        wacc = w_e * capm + w_d * cost_of_debt * (1 - settings.tax_rate)

        rows.append(
            {
                "edinet_code": edinet_code,
                "period": str(r.get("fiscal_year") or r.get("period_end")),
                "revenue_oku": _oku(revenue),
                "operating_income_oku": _oku(op_income),
                "net_income_oku": _oku(net_income),
                "operating_cf_oku": _oku(cf_op),
                "investing_cf_oku": _oku(cf_inv),
                "fcf_oku": _oku(cf_op + cf_inv),
                "operating_margin_pct": round(op_income / revenue * 100, 2) if revenue else None,
                "roa_pct": round(net_income / total_assets * 100, 2) if total_assets else None,
                "roe_pct": round(net_income / net_assets * 100, 2) if net_assets > 0 else None,
                "equity_ratio_pct": round(net_assets / total_assets * 100, 2) if total_assets else None,
                "weight_equity": round(w_e, 4),
                "weight_debt": round(w_d, 4),
                "cost_of_debt": round(cost_of_debt, 4),
                "beta": round(beta, 3),
                "wacc_pct": round(wacc * 100, 2),
            }
        )
    return rows


@dataclass
class BetaEstimate:
    beta: float
    method: str
    std: float | None = None


def _weekly_returns(ticker: str) -> pd.Series:
    import yfinance as yf

    close = yf.download(ticker, period="3y", interval="1wk", auto_adjust=True, progress=False)["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return close.pct_change().dropna()


def estimate_beta(ticker: str, use_bayes: bool = False) -> BetaEstimate:
    """3年週次リターンの対日経平均回帰でベータを推定。失敗時は yfinance の beta → 1.0。"""
    try:
        combined = pd.DataFrame(
            {"stock": _weekly_returns(ticker), "market": _weekly_returns(settings.market_index)}
        ).dropna()
        if len(combined) >= 30:
            if use_bayes:
                est = _bayes_beta(combined["stock"].values, combined["market"].values)
                if est:
                    return est
            cov = np.cov(combined["stock"], combined["market"])
            return BetaEstimate(beta=float(cov[0, 1] / cov[1, 1]), method="OLS(3y週次)")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ ベータ推定に失敗 ({ticker}): {e}")

    try:
        import yfinance as yf

        b = yf.Ticker(ticker).info.get("beta")
        if b:
            return BetaEstimate(beta=float(b), method="yfinance")
    except Exception:  # noqa: BLE001
        pass
    return BetaEstimate(beta=1.0, method="default")


def _bayes_beta(stock: np.ndarray, market: np.ndarray) -> BetaEstimate | None:
    """ノートブック操作2の PyMC によるベイズ回帰。pymc が無ければ None。"""
    try:
        import pymc as pm
    except ImportError:
        print("⚠️ pymc が未インストールのため OLS で代替します")
        return None
    with pm.Model():
        alpha = pm.Normal("alpha", mu=0, sigma=0.05)
        beta = pm.Normal("beta", mu=1.0, sigma=1.0)
        sigma = pm.HalfNormal("sigma", sigma=0.05)
        pm.Normal("y_obs", mu=alpha + beta * market, sigma=sigma, observed=stock)
        trace = pm.sample(draws=1000, tune=1000, target_accept=0.95, random_seed=42, progressbar=False)
    samples = trace.posterior["beta"].values.flatten()
    return BetaEstimate(beta=float(samples.mean()), std=float(samples.std()), method="Bayes(PyMC)")


def summarize(rows: list[dict]) -> dict:
    """Claude に渡す・画面に出すための会社単位のサマリー指標。rows は新しい順。"""
    if not rows:
        return {}
    df = pd.DataFrame(rows).sort_values("period")
    latest = df.iloc[-1]
    n = len(df)
    first_rev, last_rev = df["revenue_oku"].iloc[0], df["revenue_oku"].iloc[-1]
    cagr = ((last_rev / first_rev) ** (1 / (n - 1)) - 1) * 100 if n > 1 and first_rev > 0 and last_rev > 0 else None
    roe = df["roe_pct"].dropna()
    return {
        "periods": f"{df['period'].iloc[0]}〜{df['period'].iloc[-1]}",
        "revenue_cagr_pct": round(cagr, 2) if cagr is not None else None,
        "avg_roe_pct": round(float(roe.mean()), 2) if len(roe) else None,
        "latest_roe_pct": latest["roe_pct"],
        "latest_wacc_pct": latest["wacc_pct"],
        "latest_roe_wacc_spread_pct": round(latest["roe_pct"] - latest["wacc_pct"], 2)
        if pd.notna(latest["roe_pct"])
        else None,
        "fcf_positive_years": f"{int((df['fcf_oku'] > 0).sum())}/{n}",
        "operating_cf_positive_years": f"{int((df['operating_cf_oku'] > 0).sum())}/{n}",
        "latest_operating_margin_pct": latest["operating_margin_pct"],
        "latest_equity_ratio_pct": latest["equity_ratio_pct"],
        "beta": latest["beta"],
    }
