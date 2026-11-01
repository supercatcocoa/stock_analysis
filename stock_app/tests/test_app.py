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
    def __init__(self, verdict="優良", score=78, truncate_first=False):
        self.truncate_first = truncate_first
        self.max_tokens_seen = []
        self.calls = 0
        self.verdict, self.score = verdict, score
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        self.last_kwargs = kw
        self.max_tokens_seen.append(kw["max_tokens"])
        if self.truncate_first and self.calls == 1:
            return SimpleNamespace(stop_reason="max_tokens",
                                   content=[SimpleNamespace(type="text", text='{"verdict": "優良", "summary": "途中')])
        out = {
            "verdict": self.verdict, "score": self.score, "summary": "ROEがWACCを上回る。",
            "profitability": "良好", "cash_flow": "安定", "growth": "横ばい", "financial_health": "健全",
            "strengths": ["高ROE"], "risks": ["市況"], "watch_points": ["在庫"],
        }  # fmt: skip
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=json.dumps(out, ensure_ascii=False))])


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
    # ROIC = 2000億×0.7 / (1.8兆+6000億) = 5.83%
    assert r["roic_pct"] == pytest.approx(5.83, abs=0.01)
    # デュポン分解の積が ROE と一致する
    assert r["net_margin_pct"] * r["asset_turnover"] * r["financial_leverage"] == pytest.approx(r["roe_pct"], abs=0.02)
    s = summarize(rows)
    assert s["latest_roic_wacc_spread_pct"] is not None and len(s["dupont"]) == 3
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


def test_migrate_old_db(dbp):
    """ROIC 列が無い旧 DB でも列が追加され、次回 analyze で一度だけ再判定される。"""
    import sqlite3

    old_cols = [c for c in db.FINANCIAL_COLUMNS if c not in ("roic_pct", "net_margin_pct", "asset_turnover",
                                                             "financial_leverage")]  # fmt: skip
    con = sqlite3.connect(dbp)
    con.execute(f"CREATE TABLE financials (edinet_code TEXT, period TEXT, {', '.join(c + ' REAL' for c in old_cols)},"
                " updated_at TEXT, PRIMARY KEY (edinet_code, period))")  # fmt: skip
    con.commit()
    con.close()
    claude = FakeClaude()
    analyze_company("6723", edinet_client=FakeEdinet(), claude_client=claude, beta_estimator=fake_beta(1.0), db_path=dbp)
    with db.connect(dbp) as conn:
        assert db.get_financials(conn, "E02081")[0]["roic_pct"] == pytest.approx(5.83, abs=0.01)
    assert "roic_pct" in claude.last_kwargs["messages"][0]["content"]


def test_truncated_output_retried():
    from stock_app.analyst import evaluate_company

    claude = FakeClaude(truncate_first=True)
    rows = build_financial_rows("E02081", FakeEdinet().records, 3, beta=1.0)
    result = evaluate_company({"name": "x"}, rows, summarize(rows), client=claude)
    assert result["verdict"] == "優良" and claude.calls == 2
    assert claude.max_tokens_seen[1] == claude.max_tokens_seen[0] * 2


def test_report_pdf_sent(dbp):
    from stock_app.report import send_report

    analyze_company("6723", edinet_client=FakeEdinet(), claude_client=FakeClaude(), beta_estimator=fake_beta(1.0),
                    db_path=dbp)  # fmt: skip
    sent = []
    pdf = send_report("6723", db_path=dbp, sender=lambda m, file, filename, mime: sent.append((m, file.getvalue(), filename, mime)))
    assert pdf.startswith(b"%PDF") and len(pdf) > 10_000
    msg, body, fn, mime = sent[0]
    assert mime == "application/pdf" and fn.endswith(".pdf") and body == pdf and "優良" in msg


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
    sender = lambda msg, file, filename: sent.append((msg, file.getvalue()[:4], filename))  # noqa: E731
    loader = lambda tickers: {t: make_prices("golden") for t in tickers}  # noqa: E731
    hits = run_watch(price_loader=loader, sender=sender, db_path=dbp)
    assert len(hits) == 1 and len(sent) == 1
    assert "ルネサス" in sent[0][0] and "財務判定: 優良" in sent[0][0] and sent[0][1] == b"\x89PNG"
    run_watch(price_loader=loader, sender=sender, db_path=dbp)  # 同じ日の再実行
    assert len(sent) == 1  # 重複通知しない


def test_turso_backend(tmp_path, monkeypatch):
    """TURSO_DATABASE_URL 設定時は libsql 経由で接続する。sqlite3.Row に頼らず動くことも確認。"""
    import sqlite3
    import sys
    import types

    from stock_app.config import settings

    calls = {}

    class FakeLibsqlConn:  # libsql の接続を模したもの（row_factory は持たない）
        def __init__(self, path, sync_url, auth_token):
            calls.update(path=path, sync_url=sync_url, auth_token=auth_token, syncs=0)
            self._c = sqlite3.connect(path)

        def sync(self):
            calls["syncs"] += 1

        def execute(self, *a):
            return self._c.execute(*a)

        def commit(self):
            self._c.commit()

        def close(self):
            self._c.close()

    monkeypatch.setitem(sys.modules, "libsql", types.SimpleNamespace(connect=FakeLibsqlConn))
    monkeypatch.setattr(settings, "turso_database_url", "libsql://test.turso.io")
    monkeypatch.setattr(settings, "turso_auth_token", "tok")
    monkeypatch.setattr(settings, "turso_replica_path", tmp_path / "replica.db")

    # 既存のローカル SQLite を作ってから Turso にインポート
    local = tmp_path / "local.db"
    analyze_company("6723", edinet_client=FakeEdinet(), claude_client=FakeClaude(), beta_estimator=fake_beta(1.0),
                    db_path=local)  # fmt: skip
    counts = db.import_sqlite(str(local))
    assert counts["financials"] == 3 and counts["watchlist"] == 1
    assert calls["sync_url"] == "libsql://test.turso.io" and calls["syncs"] >= 1
    assert db.backend_name() == "Turso"

    # Turso 側のデータでウォッチと判定参照が動く
    with db.connect() as conn:
        assert [w["ticker"] for w in db.active_watchlist(conn)] == ["6723.T"]
        assert db.latest_evaluation(conn, "E02081")["verdict"] == "優良"
        assert db.find_company(conn, "6723")["name"] == "ルネサスエレクトロニクス"
    sent = []
    run_watch(price_loader=lambda t: {x: make_prices("golden") for x in t},
              sender=lambda m, file, filename: sent.append(m))  # fmt: skip
    assert len(sent) == 1
