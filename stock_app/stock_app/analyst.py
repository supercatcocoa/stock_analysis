"""Claude API による財務データの解釈と「優良企業」判定。"""
from __future__ import annotations

import json

from .config import settings

VERDICTS = ["優良", "要観察", "非優良"]

EVALUATION_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "score": {"type": "integer", "description": "0〜100 の総合スコア"},
        "summary": {"type": "string", "description": "3〜4文の総評"},
        "profitability": {
            "type": "string",
            "description": "収益性（ROE のデュポン分解・ROIC-WACC スプレッド・利益率）の解釈",
        },
        "cash_flow": {"type": "string", "description": "キャッシュフローの質の解釈"},
        "growth": {"type": "string", "description": "成長性の解釈"},
        "financial_health": {"type": "string", "description": "財務健全性・資本構成の解釈"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "watch_points": {"type": "array", "items": {"type": "string"}, "description": "次の決算で確認すべき点"},
    },
    "required": [
        "verdict", "score", "summary", "profitability", "cash_flow", "growth",
        "financial_health", "strengths", "risks", "watch_points",
    ],
    "additionalProperties": False,
}  # fmt: skip

SYSTEM_PROMPT = """あなたは日本株のファンダメンタル分析を行う慎重なアナリストです。
与えられた有価証券報告書ベースの集計データだけを根拠に、企業が「長期保有に値する優良企業」かを判定してください。

指標の定義:
- ROIC = 営業利益 ×（1 − 税率30%）÷（純資産 + 有利子負債）。事業そのものの稼ぐ力を示す。
- デュポン分解: ROE = 売上高純利益率(net_margin_pct) × 総資産回転率(asset_turnover) × 財務レバレッジ(financial_leverage)。
  ROE の水準・変化が「利益率」「資産効率」「借入による押し上げ」のどれによるものかを必ず説明すること。
  レバレッジ主導の高 ROE は質が低いとみなす。

判定の目安:
- 優良: ROIC と ROE が WACC を安定的に上回り（価値創造）、営業CF・FCF が概ね黒字、売上・利益が横ばい以上、財務が健全。
- 要観察: 一部の指標は良いが、一過性の赤字・FCFのマイナス・減収減益など判断を保留すべき要素がある。
- 非優良: ROIC・ROE が WACC を継続的に下回る、営業CFが不安定、過大な負債など構造的な懸念がある。

注意:
- 有利子負債の明細が無い企業は負債合計で代用しているため、ROIC・WACC が実態より低めに出ることがある。
- 大型買収などで投資CFが一時的に大きくマイナスになった年は、その旨を考慮して解釈すること。
- 業種特性（金融業の負債構成など）で指標が歪む場合はその点を明記すること。
- データにない事実（株価水準・ニュース等）は推測しないこと。迷う場合は保守的に「要観察」とする。
- 出力は日本語。score は 優良=70以上、要観察=40〜69、非優良=39以下 を目安にする。"""


def build_prompt(company: dict, rows: list[dict], summary: dict) -> str:
    table_cols = [
        "period", "revenue_oku", "operating_income_oku", "net_income_oku", "operating_margin_pct",
        "operating_cf_oku", "investing_cf_oku", "fcf_oku", "roa_pct", "roe_pct", "net_margin_pct",
        "asset_turnover", "financial_leverage", "roic_pct", "equity_ratio_pct", "weight_debt", "beta", "wacc_pct",
    ]  # fmt: skip
    lines = [" | ".join(table_cols)]
    for r in sorted(rows, key=lambda x: x["period"]):
        lines.append(" | ".join("" if r.get(c) is None else str(r.get(c)) for c in table_cols))
    return (
        f"# 対象企業\n{company.get('name')}（証券コード {company.get('sec_code')}, "
        f"EDINET {company.get('edinet_code')}, 業種: {company.get('industry') or '不明'}）\n\n"
        f"# 年度別データ（金額は億円、比率は%）\n" + "\n".join(lines) + "\n\n"
        f"# サマリー指標\n{json.dumps(summary, ensure_ascii=False, default=str, indent=2)}\n\n"
        "上記データに基づき、優良企業かどうかを判定してください。"
    )


def evaluate_company(company: dict, rows: list[dict], summary: dict, client=None) -> dict:
    """Claude に判定させ、スキーマ通りの dict を返す。is_excellent を付与する。"""
    if client is None:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None)

    prompt = build_prompt(company, rows, summary)
    max_tokens = settings.claude_max_tokens
    for attempt in range(2):
        response = client.messages.create(
            model=settings.claude_model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": EVALUATION_SCHEMA}},
        )
        # 出力上限に達すると JSON が途中で切れるので、上限を 2 倍にして再試行
        if getattr(response, "stop_reason", None) == "max_tokens":
            if attempt == 0:
                print(f"⚠️ 判定文が出力上限 ({max_tokens} トークン) で切れたため、上限を増やして再試行します")
                max_tokens *= 2
                continue
            raise RuntimeError(
                f"Claude の判定文が出力上限 ({max_tokens} トークン) を超えました。"
                ".env の CLAUDE_MAX_TOKENS を増やしてください"
            )
        break

    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Claude の判定結果を JSON として読めませんでした: {e} / 先頭: {text[:200]}") from e
    result["score"] = max(0, min(100, int(result["score"])))
    result["is_excellent"] = result["verdict"] == "優良"
    return result
