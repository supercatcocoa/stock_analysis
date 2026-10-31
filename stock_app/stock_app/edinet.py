"""EDINET DB (https://edinetdb.jp) API クライアントと企業コードの解決。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import requests

from .config import settings

EDINET_CODE_RE = re.compile(r"^E\d{5}$", re.IGNORECASE)


class EdinetDBError(RuntimeError):
    pass


@dataclass
class CompanyInfo:
    edinet_code: str
    sec_code: str  # 証券コード（4桁 or 英数字4桁）
    ticker: str  # yfinance 用 (例: 6723.T)
    name: str
    industry: str | None = None


class EdinetDBClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: int = 30):
        self.api_key = api_key if api_key is not None else settings.edinetdb_api_key
        self.base_url = (base_url or settings.edinetdb_base_url).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        if self.api_key:
            self.session.headers["X-API-Key"] = self.api_key

    def _get(self, path: str, params: dict | None = None) -> Any:
        res = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        if res.status_code != 200:
            raise EdinetDBError(f"EDINET DB API エラー {res.status_code}: {path} {res.text[:200]}")
        return res.json()

    def search(self, query: str) -> list[dict]:
        body = self._get("/search", {"q": query})
        return _extract_list(body)

    def get_company(self, edinet_code: str) -> dict:
        body = self._get(f"/companies/{edinet_code}")
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            return body["data"]
        return body

    def get_financials(self, edinet_code: str, years: int = 6) -> list[dict]:
        body = self._get(f"/companies/{edinet_code}/financials", {"years": years})
        return _extract_list(body)


def _extract_list(body: Any) -> list[dict]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("data", "results", "companies", "items"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def _pick(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def normalize_sec_code(raw: str | None) -> str:
    """'67230' / '6723' / '6723.T' / '283A0' → '6723' / '283A'"""
    if not raw:
        return ""
    s = str(raw).strip().upper().removesuffix(".T")
    if len(s) == 5 and s.endswith("0"):
        s = s[:4]
    return s


def to_company_info(d: dict) -> CompanyInfo:
    if isinstance(d.get("data"), dict):
        d = d["data"]
    sec = normalize_sec_code(_pick(d, "sec_code", "securities_code", "stock_code", "ticker"))
    return CompanyInfo(
        edinet_code=str(_pick(d, "edinet_code", "code") or "").upper(),
        sec_code=sec,
        ticker=f"{sec}.T" if sec else "",
        name=str(_pick(d, "name", "company_name", "filer_name", "name_ja") or ""),
        industry=_pick(d, "industry", "industry_name", "sector"),
    )


def resolve_company(client: EdinetDBClient, code: str) -> CompanyInfo:
    """EDINETコード / 証券コード / ティッカー / 社名 のどれでも企業を特定する。"""
    code = code.strip()
    if EDINET_CODE_RE.match(code):
        info = to_company_info(client.get_company(code.upper()))
        info.edinet_code = info.edinet_code or code.upper()
        return info

    query = normalize_sec_code(code) if re.match(r"^[0-9A-Za-z]{4,5}(\.T)?$", code) else code
    results = [to_company_info(r) for r in client.search(query)]
    results = [r for r in results if r.edinet_code]
    if not results:
        raise EdinetDBError(f"企業が見つかりませんでした: {code}")

    exact = [r for r in results if r.sec_code == query.upper()]
    chosen = (exact or results)[0]
    if not chosen.ticker:
        raise EdinetDBError(f"{chosen.name} の証券コードが取得できません（非上場の可能性）")
    return chosen
