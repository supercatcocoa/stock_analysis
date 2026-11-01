"""DB への保存。財務データは (edinet_code, period) をキーに Upsert する。

TURSO_DATABASE_URL が設定されていれば Turso（クラウドの SQLite 互換 DB）に保存し、
Streamlit Cloud と GitHub Actions など複数の実行環境で同じデータを共有する。
未設定ならローカルの SQLite ファイル（DB_PATH）に保存する。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings

FINANCIAL_COLUMNS = [
    "revenue_oku", "operating_income_oku", "net_income_oku", "operating_cf_oku", "investing_cf_oku",
    "fcf_oku", "operating_margin_pct", "roa_pct", "roe_pct", "equity_ratio_pct", "weight_equity",
    "weight_debt", "cost_of_debt", "beta", "wacc_pct",
    # ROIC とデュポン分解（後から追加した列。既存 DB には connect() 時に自動で ALTER TABLE する）
    "roic_pct", "net_margin_pct", "asset_turnover", "financial_leverage",
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


JST = ZoneInfo("Asia/Tokyo")


def now() -> str:
    # 実行環境（Streamlit Cloud / GitHub Actions は UTC）に関係なく日本時間で記録する
    return datetime.now(JST).replace(tzinfo=None).isoformat(timespec="seconds")


def backend_name(db_path=None) -> str:
    return "Turso" if settings.turso_database_url and db_path is None else f"SQLite ({db_path or settings.db_path})"


def _open(db_path):
    if settings.turso_database_url and db_path is None:
        import libsql  # pip install libsql

        replica = Path(settings.turso_replica_path)
        replica.parent.mkdir(parents=True, exist_ok=True)
        # Embedded replica: 読み取りは手元の複製、書き込みは Turso 本体へ送られる
        conn = libsql.connect(
            str(replica), sync_url=settings.turso_database_url, auth_token=settings.turso_auth_token
        )
        conn.sync()  # 他の環境で書かれた最新データを取り込む
        return conn
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


@contextmanager
def connect(db_path: Path | str | None = None):
    """db_path を明示した場合（テストなど）は常にローカル SQLite を使う。"""
    conn = _open(db_path)
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    _migrate(conn)
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        close = getattr(conn, "close", None)
        if close:
            close()


def _dicts(cur) -> list[dict]:
    """sqlite3 / libsql どちらのカーソルでも、列名つき dict のリストにする。"""
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _one(cur) -> dict | None:
    rows = _dicts(cur)
    return rows[0] if rows else None


def _migrate(conn) -> None:
    """古いバージョンで作った DB に、追加された列を足す。"""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(financials)").fetchall()}
    for col in FINANCIAL_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE financials ADD COLUMN {col} REAL")


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
    return _dicts(cur)


def get_company(conn, edinet_code: str) -> dict | None:
    return _one(conn.execute("SELECT * FROM companies WHERE edinet_code=?", (edinet_code,)))


def find_company(conn, code: str) -> dict | None:
    code = code.strip().upper().removesuffix(".T")
    return _one(
        conn.execute(
            "SELECT * FROM companies WHERE edinet_code=? OR sec_code=? OR name LIKE ? LIMIT 1",
            (code, code, f"%{code}%"),
        )
    )


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
    d = _one(conn.execute("SELECT * FROM evaluations WHERE edinet_code=? ORDER BY id DESC LIMIT 1", (edinet_code,)))
    if not d:
        return None
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
    return _dicts(conn.execute("SELECT * FROM watchlist WHERE active=1 ORDER BY ticker"))


def list_companies(conn) -> list[dict]:
    cur = conn.execute(
        """SELECT c.edinet_code, c.ticker, c.name, e.verdict, e.score, e.evaluated_at,
                  COALESCE(w.active, 0) AS watching
           FROM companies c
           LEFT JOIN evaluations e ON e.id = (SELECT MAX(id) FROM evaluations WHERE edinet_code=c.edinet_code)
           LEFT JOIN watchlist w ON w.edinet_code = c.edinet_code
           ORDER BY e.score DESC NULLS LAST, c.ticker"""
    )
    return _dicts(cur)


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


TABLES = ["companies", "financials", "evaluations", "watchlist", "signals"]


def import_sqlite(src_path: str, db_path=None) -> dict[str, int]:
    """ローカルの SQLite ファイルの内容を、現在の保存先（Turso など）へコピーする。既存行は上書き。"""
    src = sqlite3.connect(src_path)
    counts = {}
    with connect(db_path) as dst:
        for table in TABLES:
            try:
                cur = src.execute(f"SELECT * FROM {table}")
            except sqlite3.OperationalError:
                continue  # 古い DB に無いテーブル
            rows = _dicts(cur)
            if not rows:
                counts[table] = 0
                continue
            dst_cols = {r[1] for r in dst.execute(f"PRAGMA table_info({table})").fetchall()}
            cols = [c for c in rows[0] if c in dst_cols]
            sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
            for r in rows:
                dst.execute(sql, [r[c] for c in cols])
            counts[table] = len(rows)
    src.close()
    return counts


def table_counts(db_path=None) -> dict[str, int]:
    with connect(db_path) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchall()[0][0] for t in TABLES}
