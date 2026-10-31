"""Streamlit の画面。起動: streamlit run app.py"""
import pandas as pd
import streamlit as st

from stock_app import db
from stock_app.config import settings

st.set_page_config(page_title="優良企業判定 & 株価ウォッチ", page_icon="📈", layout="wide")
st.title("📈 優良企業判定 & 株価ウォッチ")

missing = [k for k, v in {"EDINETDB_API_KEY": settings.edinetdb_api_key,
                          "ANTHROPIC_API_KEY": settings.anthropic_api_key,
                          "DISCORD_WEBHOOK_URL": settings.discord_webhook_url}.items() if not v]  # fmt: skip
if missing:
    st.warning("未設定の環境変数: " + ", ".join(missing) + "（.env に記入してください）")

tab_analyze, tab_list, tab_watch = st.tabs(["🔎 企業を判定", "🗂 保存済み企業", "👀 株価ウォッチ"])


def render_company(edinet_code: str):
    with db.connect() as conn:
        c = db.get_company(conn, edinet_code)
        fin = db.get_financials(conn, edinet_code)[: settings.analysis_years]
        ev = db.latest_evaluation(conn, edinet_code)
    if not c:
        return
    st.subheader(f"{c['name']}（{c['ticker']} / {c['edinet_code']}）")
    if ev:
        d = ev["detail"]
        color = {"優良": "green", "要観察": "orange", "非優良": "red"}[d["verdict"]]
        st.markdown(f"### :{color}[{d['verdict']}]　スコア **{d['score']}** / 100")
        st.caption(f"判定日時 {ev['evaluated_at']} ・ モデル {ev['model']}")
        st.write(d["summary"])
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**収益性**: " + d["profitability"])
            st.markdown("**成長性**: " + d["growth"])
            st.markdown("**強み**\n" + "\n".join(f"- {s}" for s in d["strengths"]))
        with c2:
            st.markdown("**キャッシュフロー**: " + d["cash_flow"])
            st.markdown("**財務健全性**: " + d["financial_health"])
            st.markdown("**リスク**\n" + "\n".join(f"- {s}" for s in d["risks"]))
        if d.get("watch_points"):
            st.info("次の決算で見る点: " + " / ".join(d["watch_points"]))

    if fin:
        df = pd.DataFrame(fin).drop(columns=["edinet_code", "updated_at"]).sort_values("period").set_index("period")
        g1, g2, g3 = st.columns(3)
        g1.caption("売上高・営業利益（億円）")
        g1.bar_chart(df[["revenue_oku", "operating_income_oku"]], stack=False)
        g2.caption("営業CF・投資CF・FCF（億円）")
        g2.bar_chart(df[["operating_cf_oku", "investing_cf_oku", "fcf_oku"]], stack=False)
        g3.caption("ROE vs WACC（%）")
        g3.line_chart(df[["roe_pct", "wacc_pct"]])
        st.dataframe(df, use_container_width=True)


with tab_analyze:
    with st.form("analyze"):
        code = st.text_input("企業コード", placeholder="6723 / 6723.T / E02081 / 社名")
        c1, c2, c3 = st.columns(3)
        force = c1.checkbox("更新が無くても再判定する")
        bayes = c2.checkbox("ベイズでベータ推定（遅い）")
        notify = c3.checkbox("優良判定の変化を Discord 通知")
        submitted = st.form_submit_button("判定する", type="primary")
    if submitted and code:
        from stock_app.service import analyze_company

        with st.spinner("財務データを取得し、Claude が判定しています…"):
            try:
                res = analyze_company(code, force=force, use_bayes=bayes, notify=notify)
                st.session_state["last_code"] = res.company["edinet_code"]
                for m in res.messages:
                    st.write(m)
            except Exception as e:  # noqa: BLE001
                st.error(f"エラー: {e}")
    if st.session_state.get("last_code"):
        render_company(st.session_state["last_code"])

with tab_list:
    with db.connect() as conn:
        rows = db.list_companies(conn)
    if rows:
        df = pd.DataFrame(rows)
        df["watching"] = df["watching"].astype(bool)
        st.dataframe(df, use_container_width=True, hide_index=True)
        pick = st.selectbox("詳細を見る", df["edinet_code"], format_func=lambda x: df.set_index("edinet_code").loc[x, "name"])
        render_company(pick)
    else:
        st.info("まだ判定した企業がありません")

with tab_watch:
    with db.connect() as conn:
        wl = db.active_watchlist(conn)
    st.write(f"ウォッチ中: {len(wl)} 銘柄（優良判定の企業が自動で入ります）")
    if wl:
        st.dataframe(pd.DataFrame(wl)[["ticker", "name", "added_at"]], hide_index=True)
    c1, c2 = st.columns(2)
    if c1.button("今すぐチェック（Discordに送らない）"):
        from stock_app.watcher import run_watch

        with st.spinner("株価をチェック中…"):
            hits = run_watch(dry_run=True)
        st.write(pd.DataFrame([h.__dict__ for h in hits]) if hits else "シグナルはありません")
    if c2.button("チェックして Discord に送信"):
        from stock_app.watcher import run_watch

        with st.spinner("株価をチェック中…"):
            hits = run_watch()
        st.success(f"{len(hits)} 件のシグナル")
