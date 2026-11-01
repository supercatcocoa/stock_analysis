"""判定結果の PDF レポート生成と Discord 送信。"""
from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle  # noqa: E402

from . import db  # noqa: E402

# 画面・PDF 共通の列ラベル
METRIC_LABELS = {
    "revenue_oku": "売上高（億円）",
    "operating_income_oku": "営業利益（億円）",
    "net_income_oku": "純利益（億円）",
    "operating_margin_pct": "営業利益率（%）",
    "operating_cf_oku": "営業CF（億円）",
    "investing_cf_oku": "投資CF（億円）",
    "fcf_oku": "FCF（億円）",
    "roa_pct": "ROA（%）",
    "roe_pct": "ROE（%）",
    "net_margin_pct": "　└ 売上高純利益率（%）",
    "asset_turnover": "　└ 総資産回転率（回）",
    "financial_leverage": "　└ 財務レバレッジ（倍）",
    "roic_pct": "ROIC（%）",
    "wacc_pct": "WACC（%）",
    "equity_ratio_pct": "自己資本比率（%）",
    "weight_debt": "D/V（有利子負債比率）",
    "cost_of_debt": "負債コスト（%）",
    "beta": "Beta",
}

# 表示時の倍率と書式（負債コストは小数→%表示、Beta は 3 桁）
DISPLAY_FORMAT = {
    "cost_of_debt": (100, "{:.2f}"),
    "beta": (1, "{:.3f}"),
    "weight_debt": (1, "{:.3f}"),
    "asset_turnover": (1, "{:.3f}"),
    "financial_leverage": (1, "{:.3f}"),
}

VERDICT_COLORS = {"優良": "#1a7f37", "要観察": "#bf8700", "非優良": "#cf222e"}

# 埋め込み用の日本語 TrueType フォント候補（見つからなければ PDF 標準の日本語 CID フォントを使う）
FONT_CANDIDATES = [
    os.getenv("PDF_FONT_PATH", ""),
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "C:/Windows/Fonts/meiryo.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf",
    "/usr/share/fonts/truetype/takao-gothic/TakaoPGothic.ttf",
]

_FONT_NAME: str | None = None


def _register_font() -> str:
    global _FONT_NAME
    if _FONT_NAME:
        return _FONT_NAME
    for path in FONT_CANDIDATES:
        if path and Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont("JP", path, subfontIndex=0))
                _FONT_NAME = "JP"
                return _FONT_NAME
            except Exception:  # noqa: BLE001  CFF 形式などは読めないので次の候補へ
                continue
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    _FONT_NAME = "HeiseiKakuGo-W5"
    return _FONT_NAME


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_chart_png(fin: pd.DataFrame) -> io.BytesIO:
    """PDF 用の 3 段チャート。棒は必ず 0 基準で描く。ラベルは英語（フォント非依存）。"""
    df = fin.sort_values("period")
    x = range(len(df))
    periods = df["period"].astype(str).tolist()
    fig, axes2 = plt.subplots(2, 2, figsize=(12, 7))
    axes = axes2.flatten()

    ax = axes[0]
    w = 0.38
    ax.bar([i - w / 2 for i in x], df["revenue_oku"], w, label="Revenue", color="#0068c9")
    ax.bar([i + w / 2 for i in x], df["operating_income_oku"], w, label="Operating income", color="#ff9f43")
    ax.set_title("Revenue & Operating income (100M JPY)", fontsize=10)

    ax = axes[1]
    w = 0.26
    ax.bar([i - w for i in x], df["operating_cf_oku"], w, label="Operating CF", color="#2e8b57")
    ax.bar(list(x), df["investing_cf_oku"], w, label="Investing CF", color="#cd5c5c")
    ax.bar([i + w for i in x], df["fcf_oku"], w, label="FCF", color="#8a6fdf")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("Cash flow (100M JPY)", fontsize=10)

    ax = axes[2]
    ax.plot(list(x), df["roe_pct"], marker="o", color="crimson", label="ROE %")
    if "roic_pct" in df and df["roic_pct"].notna().any():
        ax.plot(list(x), df["roic_pct"], marker="D", color="seagreen", label="ROIC %")
    ax.plot(list(x), df["wacc_pct"], marker="s", color="dodgerblue", label="WACC %")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("ROE / ROIC vs WACC (%)", fontsize=10)

    ax = axes[3]
    if "net_margin_pct" in df and df["net_margin_pct"].notna().any():
        ax.bar(list(x), df["net_margin_pct"], 0.5, color="#f4a261", label="Net margin % (left)")
        ax.axhline(0, color="black", lw=0.8)
        ax2 = ax.twinx()
        ax2.plot(list(x), df["asset_turnover"], marker="o", color="#264653", label="Asset turnover x (right)")
        ax2.plot(list(x), df["financial_leverage"], marker="s", color="#e76f51", label="Leverage x (right)")
        ax2.set_ylim(0, max(df["financial_leverage"].max(), df["asset_turnover"].max()) * 1.3)
        ax2.legend(fontsize=8, loc="upper right")
    ax.set_title("ROE DuPont: Net margin x Asset turnover x Leverage", fontsize=10)

    for ax in axes:
        ax.set_xticks(list(x))
        ax.set_xticklabels(periods)
        ax.grid(axis="y", ls="--", alpha=0.5)
        ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160)
    plt.close(fig)
    buf.seek(0)
    return buf


def build_pdf(company: dict, fin_rows: list[dict], evaluation: dict | None) -> bytes:
    font = _register_font()
    base = dict(fontName=font, wordWrap="CJK")
    st = {
        "title": ParagraphStyle("t", fontSize=16, leading=22, **base),
        "verdict": ParagraphStyle("v", fontSize=14, leading=20, **base),
        "small": ParagraphStyle("s", fontSize=8, leading=11, textColor=colors.grey, **base),
        "h": ParagraphStyle("h", fontSize=11, leading=16, spaceBefore=6, **base),
        "body": ParagraphStyle("b", fontSize=9, leading=14, **base),
        "cell": ParagraphStyle("c", fontSize=8, leading=10, **base),
    }

    story = [Paragraph(_esc(f"{company['name']}（{company['ticker']} / {company['edinet_code']}）"), st["title"])]
    if evaluation:
        d = evaluation["detail"]
        color = VERDICT_COLORS.get(d["verdict"], "#000000")
        story += [
            Paragraph(f'<font color="{color}"><b>{_esc(d["verdict"])}</b></font>　スコア {d["score"]} / 100', st["verdict"]),
            Paragraph(_esc(f"判定日時 {evaluation['evaluated_at']} ・ モデル {evaluation['model']} ・ "
                           f"出力 {datetime.now():%Y-%m-%d %H:%M}"), st["small"]),  # fmt: skip
            Spacer(1, 4),
            Paragraph(_esc(d["summary"]), st["body"]),
        ]
        for label, key in (("収益性", "profitability"), ("キャッシュフロー", "cash_flow"),
                           ("成長性", "growth"), ("財務健全性", "financial_health")):  # fmt: skip
            story += [Paragraph(f"<b>{label}</b>", st["h"]), Paragraph(_esc(d.get(key, "")), st["body"])]
        for label, key in (("強み", "strengths"), ("リスク", "risks"), ("次の決算で見る点", "watch_points")):
            items = d.get(key) or []
            if items:
                story.append(Paragraph(f"<b>{label}</b>", st["h"]))
                story += [Paragraph("・" + _esc(s), st["body"]) for s in items]
    else:
        story.append(Paragraph("判定結果はまだありません", st["body"]))

    if fin_rows:
        fin = pd.DataFrame(fin_rows)
        story += [
            PageBreak(),
            Paragraph("財務指標の推移", st["h"]),
            Image(make_chart_png(fin), width=180 * mm, height=105 * mm),
        ]

        df = fin.sort_values("period").set_index("period")
        periods = [str(p) for p in df.index]
        data = [[Paragraph("指標", st["cell"])] + [Paragraph(p, st["cell"]) for p in periods]]
        for col, label in METRIC_LABELS.items():
            if col in df:
                scale, fmt = DISPLAY_FORMAT.get(col, (1, "{:,.2f}"))
                vals = ["" if pd.isna(v) else fmt.format(v * scale) for v in df[col]]
                data.append([Paragraph(label, st["cell"])] + vals)
        table = Table(data, colWidths=[55 * mm] + [30 * mm] * len(periods), repeatRows=1)
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")),
            ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c0c0c0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
        ]))  # fmt: skip
        story += [Spacer(1, 6), table]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm, title=f"{company['name']} 財務判定レポート")  # fmt: skip
    doc.build(story)
    return buf.getvalue()


def build_report_for(code: str, db_path=None) -> tuple[dict, bytes]:
    """保存済みデータから PDF を作る。code は EDINET コード / 証券コード / 社名。"""
    with db.connect(db_path) as conn:
        company = db.find_company(conn, code)
        if not company:
            raise RuntimeError(f"{code} は未保存です。先に analyze を実行してください")
        from .config import settings

        fin = db.get_financials(conn, company["edinet_code"])[: settings.analysis_years]
        ev = db.latest_evaluation(conn, company["edinet_code"])
    return company, build_pdf(company, fin, ev)


def send_report(code: str, db_path=None, sender=None) -> bytes:
    """PDF を作って Discord に送信する。"""
    if sender is None:
        from .discord import send as sender
    company, pdf = build_report_for(code, db_path)
    with db.connect(db_path) as conn:
        ev = db.latest_evaluation(conn, company["edinet_code"])
    head = f"📄 **{company['name']}** ({company['ticker']}) 財務判定レポート"
    if ev:
        head += f"\n判定: {ev['verdict']}（スコア {ev['score']}）"
    filename = f"{company['sec_code'] or company['edinet_code']}_report_{datetime.now():%Y%m%d}.pdf"
    sender(head, io.BytesIO(pdf), filename, "application/pdf")
    return pdf
