"""外部 API をモックした結合テスト。 pytest -q で実行。"""
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from stock_app import db
from stock_app.edinet import normalize_sec_code, resolve_company
from stock_app.metrics import BetaEstimate, build_financial_rows, summarize
from stock_app.service import analyze_company
from stock_app.watcher import detect_signal, run_watch


def fin_record(year, revenue=1.3e12, op=2.0e11, ni=1.5e11):
    return {
        "fiscal_year": year, "revenue": revenue, "operating_income": op, "net_income": ni,
        "total_assets": 3.0e12, "net_assets": 1.8e12, "total_liabilities": 1.2e12,
        "short_term_loans": 1e11, "long_term_loans": 5e11, "cf_operating": 4.5e11, "cf_investing": -1.2e11,
    }  # fmt: skip


class FakeEdinet:
    def __init__(self):
        self.records = [fin_record(y) for y in (2025, 2024, 2023, 2022, 2021)]
        self.records.append({**fin_record(2025), "period_type": "Q2"})  # 四半期は除外される

    def search(self, q):
        return [{"edinet_code": "E02081", "sec_code": "67230", "name": "ルネサスエレクトロニクス"},
                {"edinet_code": "E99999", "sec_code": "67231", "name": "別会社"}]  # fmt: skip

    def get_company(self, code):
        return {"data": {"edinet_code": code, "sec_code": "67230", "name": "ルネサスエレクトロニクス"}}

    def get_financials(self, code, years=6):
        return self.records


class FakeClaude:
    def __init__(self, verdict="優良", score=78):
        self.calls = 0
        self.verdict, self.score = verdict, score
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        self.last_kwargs = kw
        out = {
            "verdict": self.verdict, "score": self.score, "summary": "ROEがWACCを上回る。",
            "profitability": "良好", "cash_flow": "安定", "growth": "横ばい", "financial_health": "健全",
            "strengths": ["高ROE"], "risks": ["市況"], "watch_points": ["在庫"],
        }  # fmt: skip
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(out, ensure_ascii=False))])


def fake_beta(beta):
    return lambda ticker, use_bayes=False: BetaEstimate(beta=beta, method="test")


@pytest.fixture
def dbp(tmp_path):
    return tmp_path / "t.db"


def test_resolve_codes():
    e = FakeEdinet()
    assert normalize_sec_code("283A0") == "283A"
    assert resolve_company(e, "6723.T").edinet_code == "E02081"
    assert resolve_company(e, "6723").ticker == "6723.T"
    assert resolve_company(e, "E02081").name == "ルネサスエレクトロニクス"


def test_rows_and_summary():
    rows = build_financial_rows("E02081", FakeEdinet().records, 3, beta=1.3)
    assert [r["period"] for r in rows] == ["2025", "2024", "2023"]
    r = rows[0]
    assert r["fcf_oku"] == 3300.0 and r["roe_pct"] == pytest.approx(8.33, abs=0.01)
    # 有利子負債 = 短期+長期借入 = 6000億 → D/V = 0.6/(1.8+0.6)
    assert r["weight_debt"] == pytest.approx(0.25)
    s = summarize(rows)
    assert s["fcf_positive_years"] == "3/3" and s["latest_roe_wacc_spread_pct"] is not None


def test_analyze_upsert_and_reuse(dbp):
    edinet, claude = FakeEdinet(), FakeClaude()
    r1 = analyze_company("6723", edinet_client=edinet, claude_client=claude, beta_estimator=fake_beta(1.3), db_path=dbp)
    assert r1.changed_rows == 3 and r1.reevaluated and r1.watch_changed == "added"
    assert claude.last_kwargs["output_config"]["format"]["type"] == "json_schema"

    # 2回目: ベータだけ変化 → 決算データ更新なし → Claude を呼ばない
    r2 = analyze_company("6723", edinet_client=edinet, claude_client=claude, beta_estimator=fake_beta(1.4), db_path=dbp)
    assert r2.changed_rows == 0 and not r2.reevaluated and claude.calls == 1
    with db.connect(dbp) as conn:
        assert db.get_financials(conn, "E02081")[0]["beta"] == 1.4  # ベータは Upsert 済み
        assert len(db.get_financials(conn, "E02081")) == 3

    # 3回目: 新しい決算期が追加 → 再判定 → 非優良ならウォッチ解除
    edinet.records.insert(0, fin_record(2026, ni=-5e10))
    claude.verdict, claude.score = "非優良", 30
    r3 = analyze_company("6723", edinet_client=edinet, claude_client=claude, beta_estimator=fake_beta(1.4), db_path=dbp)
    assert r3.changed_rows == 1 and r3.reevaluated and r3.watch_changed == "removed"
    with db.connect(dbp) as conn:
        assert db.active_watchlist(conn) == []
        assert len(db.get_financials(conn, "E02081")) == 4  # 2023〜2026 が保存されている


def make_prices(kind):
    idx = pd.bdate_range(end="2026-10-30", periods=120)
    if kind == "golden":  # 下落→最終日に急反発してSMA5がSMA25を上抜け
        close = np.r_[np.linspace(1000, 800, 119), [1000]]
    else:  # ずっと下落
        close = np.linspace(1000, 600, 120)
    return pd.DataFrame({"Close": close}, index=idx)


def test_detect_signal():
    assert any("ゴールデンクロス" in r for r in detect_signal("X", make_prices("golden")).reasons)
    assert not any("ゴールデンクロス" in r for r in detect_signal("X", make_prices("down")).reasons)


def test_watch_notifies_once(dbp):
    analyze_company("6723", edinet_client=FakeEdinet(), claude_client=FakeClaude(), beta_estimator=fake_beta(1.0),
                    db_path=dbp)  # fmt: skip
    sent = []
    sender = lambda msg, img, fn: sent.append((msg, img.getvalue()[:4], fn))  # noqa: E731
    loader = lambda tickers: {t: make_prices("golden") for t in tickers}  # noqa: E731
    hits = run_watch(price_loader=loader, sender=sender, db_path=dbp)
    assert len(hits) == 1 and len(sent) == 1
    assert "ルネサス" in sent[0][0] and "財務判定: 優良" in sent[0][0] and sent[0][1] == b"\x89PNG"
    run_watch(price_loader=loader, sender=sender, db_path=dbp)  # 同じ日の再実行
    assert len(sent) == 1  # 重複通知しない

