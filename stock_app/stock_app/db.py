"""SQLite への保存。財務データは (edinet_code, period) をキーに Upsert する。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .config import settings

FINANCIAL_COLUMNS = [
    "revenue_oku", "operating_income_oku", "net_income_oku", "operating_cf_oku", "investing_cf_oku",
    "fcf_oku", "operating_margin_pct", "roa_pct", "roe_pct", "equity_ratio_pct", "weight_equity",
    "weight_debt", "cost_of_debt", "beta", "wacc_pct",
]  # fmt: skip

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS companies (
    edinet_code TEXT PRIMARY KEY,
    sec_code    TEXT,
    ticker      TEXT,
    name        TEXT,
    industry    TEXT,
    beta_method TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS financials (
    edinet_code TEXT NOT NULL,
    period      TEXT NOT NULL,
    {", ".join(f"{c} REAL" for c in FINANCIAL_COLUMNS)},
    updated_at  TEXT,
    PRIMARY KEY (edinet_code, period)
);
CREATE TABLE IF NOT EXISTS evaluations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    edinet_code  TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    model        TEXT,
    verdict      TEXT,
    is_excellent INTEGER,
    score        INTEGER,
    summary      TEXT,
    detail_json  TEXT,
    metrics_json TEXT
);
CREATE TABLE IF NOT EXISTS watchlist (
    edinet_code TEXT PRIMARY KEY,
    ticker      TEXT NOT NULL,
    name        TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    added_at    TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    ticker      TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    reasons     TEXT,
    close       REAL,
    rsi         REAL,
    notified_at TEXT,
    PRIMARY KEY (ticker, signal_date)
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path | str | None = None):
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_company(conn, info, beta_method: str | None = None) -> None:
    conn.execute(
        """INSERT INTO companies (edinet_code, sec_code, ticker, name, industry, beta_method, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(edinet_code) DO UPDATE SET
             sec_code=excluded.sec_code, ticker=excluded.ticker, name=excluded.name,
             industry=COALESCE(excluded.industry, companies.industry),
             beta_method=COALESCE(excluded.beta_method, companies.beta_method),
             updated_at=excluded.updated_at""",
        (info.edinet_code, info.sec_code, info.ticker, info.name, info.industry, beta_method, now()),
    )


# 市場データ由来で毎回揺れる列。これだけが変わっても「決算データ更新」とはみなさない
MARKET_COLUMNS = {"beta", "wacc_pct"}
FUNDAMENTAL_COLUMNS = [c for c in FINANCIAL_COLUMNS if c not in MARKET_COLUMNS]


def upsert_financials(conn, rows: list[dict]) -> int:
    """財務行を Upsert する。

    戻り値は「決算データ（ベータ/WACC 以外）が新規追加・変更された行数」。
    ベータ/WACC だけが変わった行も DB には反映されるが、カウントはしない。
    """
    existing = {r["period"]: r for r in get_financials(conn, rows[0]["edinet_code"])} if rows else {}
    fundamental_changed = 0
    for row in rows:
        old = existing.get(row["period"])
        if old is None or any(_differs(old.get(c), row.get(c)) for c in FUNDAMENTAL_COLUMNS):
            fundamental_changed += 1
    _upsert_rows(conn, rows)
    return fundamental_changed


def _differs(a, b) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    return abs(float(a) - float(b)) > 1e-9


def _upsert_rows(conn, rows: list[dict]) -> None:
    cols = ["edinet_code", "period", *FINANCIAL_COLUMNS, "updated_at"]
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in [*FINANCIAL_COLUMNS, "updated_at"])
    # 値に差分がある場合だけ UPDATE する（差分が無ければ changes=0 になる）
    diff_clause = " OR ".join(f"financials.{c} IS NOT excluded.{c}" for c in FINANCIAL_COLUMNS)
    sql = (
        f"INSERT INTO financials ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(edinet_code, period) DO UPDATE SET {set_clause} WHERE {diff_clause}"
    )
    ts = now()
    for row in rows:
        conn.execute(sql, [*(row.get(c) for c in cols[:-1]), ts])


def get_financials(conn, edinet_code: str) -> list[dict]:
    cur = conn.execute("SELECT * FROM financials WHERE edinet_code=? ORDER BY period DESC", (edinet_code,))
    return [dict(r) for r in cur.fetchall()]


def get_company(conn, edinet_code: str) -> dict | None:
    r = conn.execute("SELECT * FROM companies WHERE edinet_code=?", (edinet_code,)).fetchone()
    return dict(r) if r else None


def find_company(conn, code: str) -> dict | None:
    code = code.strip().upper().removesuffix(".T")
    r = conn.execute(
        "SELECT * FROM companies WHERE edinet_code=? OR sec_code=? OR name LIKE ?", (code, code, f"%{code}%")
    ).fetchone()
    return dict(r) if r else None


def insert_evaluation(conn, edinet_code: str, model: str, result: dict, metrics: dict) -> None:
    conn.execute(
        """INSERT INTO evaluations (edinet_code, evaluated_at, model, verdict, is_excellent, score,
                                    summary, detail_json, metrics_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            edinet_code, now(), model, result["verdict"], int(bool(result["is_excellent"])),
            int(result["score"]), result["summary"],
            json.dumps(result, ensure_ascii=False), json.dumps(metrics, ensure_ascii=False, default=str),
        ),
    )  # fmt: skip


def latest_evaluation(conn, edinet_code: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM evaluations WHERE edinet_code=? ORDER BY id DESC LIMIT 1", (edinet_code,)
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["detail"] = json.loads(d.pop("detail_json") or "{}")
    return d


def latest_financial_update(conn, edinet_code: str) -> str | None:
    r = conn.execute("SELECT MAX(updated_at) FROM financials WHERE edinet_code=?", (edinet_code,)).fetchone()
    return r[0] if r else None


def set_watch(conn, edinet_code: str, ticker: str, name: str, active: bool) -> None:
    ts = now()
    conn.execute(
        """INSERT INTO watchlist (edinet_code, ticker, name, active, added_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(edinet_code) DO UPDATE SET
             ticker=excluded.ticker, name=excluded.name, active=excluded.active, updated_at=excluded.updated_at""",
        (edinet_code, ticker, name, int(active), ts, ts),
    )


def active_watchlist(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM watchlist WHERE active=1 ORDER BY ticker").fetchall()]


def list_companies(conn) -> list[dict]:
    cur = conn.execute(
        """SELECT c.edinet_code, c.ticker, c.name, e.verdict, e.score, e.evaluated_at,
                  COALESCE(w.active, 0) AS watching
           FROM companies c
           LEFT JOIN evaluations e ON e.id = (SELECT MAX(id) FROM evaluations WHERE edinet_code=c.edinet_code)
           LEFT JOIN watchlist w ON w.edinet_code = c.edinet_code
           ORDER BY e.score DESC NULLS LAST, c.ticker"""
    )
    return [dict(r) for r in cur.fetchall()]


def signal_already_sent(conn, ticker: str, signal_date: str) -> bool:
    r = conn.execute(
        "SELECT 1 FROM signals WHERE ticker=? AND signal_date=? AND notified_at IS NOT NULL", (ticker, signal_date)
    ).fetchone()
    return r is not None


def record_signal(conn, ticker: str, signal_date: str, reasons: list[str], close: float, rsi: float, notified: bool):
    conn.execute(
        """INSERT INTO signals (ticker, signal_date, reasons, close, rsi, notified_at) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(ticker, signal_date) DO UPDATE SET reasons=excluded.reasons, close=excluded.close,
             rsi=excluded.rsi, notified_at=COALESCE(excluded.notified_at, signals.notified_at)""",
        (ticker, signal_date, " / ".join(reasons), close, rsi, now() if notified else None),
    )
