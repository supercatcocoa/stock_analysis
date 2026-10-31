"""機能1: 企業コード → 財務集計 → DB Upsert → Claude 判定 → ウォッチリスト反映。"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import db
from .analyst import evaluate_company
from .config import settings
from .edinet import EdinetDBClient, resolve_company
from .metrics import build_financial_rows, estimate_beta, summarize


@dataclass
class AnalysisResult:
    company: dict
    rows: list[dict]
    summary: dict
    evaluation: dict
    changed_rows: int
    reevaluated: bool
    watch_changed: str | None = None  # "added" / "removed" / None
    messages: list[str] = field(default_factory=list)


def analyze_company(
    code: str,
    force: bool = False,
    use_bayes: bool = False,
    notify: bool = False,
    edinet_client: EdinetDBClient | None = None,
    claude_client=None,
    beta_estimator=estimate_beta,
    db_path=None,
) -> AnalysisResult:
    log: list[str] = []
    edinet_client = edinet_client or EdinetDBClient()

    info = resolve_company(edinet_client, code)
    log.append(f"🔎 {info.name} ({info.ticker} / {info.edinet_code})")

    records = edinet_client.get_financials(info.edinet_code, years=max(settings.analysis_years + 3, 6))
    if not records:
        raise RuntimeError(f"{info.name} の財務データが取得できませんでした")

    beta = beta_estimator(info.ticker, use_bayes=use_bayes)
    log.append(f"📐 Beta={beta.beta:.3f} ({beta.method})")
    rows = build_financial_rows(info.edinet_code, records, settings.analysis_years, beta.beta)

    with db.connect(db_path) as conn:
        db.upsert_company(conn, info, beta.method)
        changed = db.upsert_financials(conn, rows)
        log.append(f"💾 財務データ Upsert: {len(rows)}期中 {changed}期が新規/更新")

        company = db.get_company(conn, info.edinet_code)
        stored_rows = db.get_financials(conn, info.edinet_code)[: settings.analysis_years]
        summary = summarize(stored_rows)
        prev = db.latest_evaluation(conn, info.edinet_code)

        reevaluate = force or prev is None or changed > 0
        if reevaluate:
            log.append(f"🤖 Claude ({settings.claude_model}) で判定中…")
            evaluation = evaluate_company(company, stored_rows, summary, client=claude_client)
            db.insert_evaluation(conn, info.edinet_code, settings.claude_model, evaluation, summary)
        else:
            evaluation = prev["detail"]
            log.append(f"♻️ 決算データに更新が無いため前回判定（{prev['evaluated_at']}）を再利用")

        # ウォッチリストの更新
        was_watching = any(w["edinet_code"] == info.edinet_code for w in db.active_watchlist(conn))
        db.set_watch(conn, info.edinet_code, info.ticker, info.name, evaluation["is_excellent"])
        watch_changed = None
        if evaluation["is_excellent"] and not was_watching:
            watch_changed = "added"
        elif not evaluation["is_excellent"] and was_watching:
            watch_changed = "removed"

    log.append(f"🏁 判定: {evaluation['verdict']}（スコア {evaluation['score']}）")
    if watch_changed == "added":
        log.append("👀 ウォッチリストに追加しました")
    elif watch_changed == "removed":
        log.append("🚫 ウォッチリストから外しました")

    if notify and reevaluate and watch_changed:
        from . import discord

        head = "🌟 新たに優良判定" if watch_changed == "added" else "⚠️ 優良判定を解除"
        discord.send(f"【{head}】**{info.name}** ({info.ticker}) スコア {evaluation['score']}\n{evaluation['summary']}")

    return AnalysisResult(company, stored_rows, summary, evaluation, changed, reevaluate, watch_changed, log)
