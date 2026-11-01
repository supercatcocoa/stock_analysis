"""コマンドライン入口。

  python -m stock_app analyze 6723 8035 E02144   # 機能1（複数可）
  python -m stock_app watch                       # 機能2（毎日 cron で実行）
  python -m stock_app list                        # 保存済み企業と判定一覧
  python -m stock_app show 6723                   # 保存済みデータと判定の詳細
  python -m stock_app report 6723                 # 判定結果を PDF にして Discord へ
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import db


def cmd_analyze(args) -> int:
    from .service import analyze_company

    ok = True
    for code in args.codes:
        try:
            res = analyze_company(code, force=args.force, use_bayes=args.bayes, notify=args.notify)
            print("\n".join(res.messages))
            ev = res.evaluation
            print(f"\n📝 {ev['summary']}")
            for label, key in (("強み", "strengths"), ("リスク", "risks"), ("次に見る点", "watch_points")):
                print(f"  [{label}] " + " / ".join(ev.get(key, [])))
            if args.pdf:
                from .report import send_report

                send_report(res.company["edinet_code"])
                print("📨 PDF レポートを Discord に送信しました")
            print("-" * 60)
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"❌ {code}: {e}")
    return 0 if ok else 1


def cmd_watch(args) -> int:
    from .watcher import run_watch

    run_watch(dry_run=args.dry_run, force_notify=args.test)
    return 0


def cmd_list(args) -> int:
    with db.connect() as conn:
        rows = db.list_companies(conn)
    if not rows:
        print("保存済みの企業はありません")
        return 0
    df = pd.DataFrame(rows)
    df["watching"] = df["watching"].map({1: "👀", 0: ""})
    print(df.to_string(index=False))
    return 0


def cmd_show(args) -> int:
    with db.connect() as conn:
        c = db.find_company(conn, args.code)
        if not c:
            print("見つかりません。先に analyze を実行してください")
            return 1
        fin = pd.DataFrame(db.get_financials(conn, c["edinet_code"])).drop(columns=["edinet_code"])
        ev = db.latest_evaluation(conn, c["edinet_code"])
    print(f"{c['name']} ({c['ticker']} / {c['edinet_code']})\n")
    print(fin.to_string(index=False))
    if ev:
        d = ev["detail"]
        print(f"\n判定: {d['verdict']} スコア {d['score']}（{ev['evaluated_at']}, {ev['model']}）\n{d['summary']}")
        for k in ("profitability", "cash_flow", "growth", "financial_health"):
            print(f"- {k}: {d.get(k)}")
    return 0


def cmd_report(args) -> int:
    from pathlib import Path

    from .report import build_report_for, send_report

    if args.no_send:
        company, pdf = build_report_for(args.code)
    else:
        pdf = send_report(args.code)
        print("📨 Discord に送信しました")
    if args.out:
        Path(args.out).write_bytes(pdf)
        print(f"💾 {args.out} に保存しました")
    return 0


def cmd_db_info(args) -> int:
    print(f"保存先: {db.backend_name()}")
    for t, n in db.table_counts().items():
        print(f"  {t:12s} {n} 件")
    return 0


def cmd_import_sqlite(args) -> int:
    print(f"{args.path} → {db.backend_name()} にコピーします")
    for t, n in db.import_sqlite(args.path).items():
        print(f"  {t:12s} {n} 件")
    return 0


def cmd_test_discord(args) -> int:
    from .discord import send

    send("【テスト通知】株価ウォッチアプリから Discord への接続テストです ✅")
    print("送信しました")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="stock_app", description="財務判定 & 株価ウォッチ")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="企業コードを指定して財務集計 + Claude 判定")
    a.add_argument("codes", nargs="+", help="証券コード(6723) / ティッカー(6723.T) / EDINETコード(E02081) / 社名")
    a.add_argument("--force", action="store_true", help="決算データに更新が無くても Claude で再判定する")
    a.add_argument("--bayes", action="store_true", help="ベータを PyMC のベイズ推定で求める（要 pymc）")
    a.add_argument("--notify", action="store_true", help="優良判定の追加/解除を Discord に通知する")
    a.add_argument("--pdf", action="store_true", help="判定結果を PDF にして Discord に送信する")
    a.set_defaults(func=cmd_analyze)

    r = sub.add_parser("report", help="保存済みの判定結果を PDF にして Discord に送信")
    r.add_argument("code")
    r.add_argument("-o", "--out", help="PDF をファイルにも保存する（例: report.pdf）")
    r.add_argument("--no-send", action="store_true", help="Discord には送らない（--out と併用）")
    r.set_defaults(func=cmd_report)

    w = sub.add_parser("watch", help="ウォッチリストの買いシグナルを Discord に送る")
    w.add_argument("--dry-run", action="store_true", help="判定だけ行い Discord には送らない")
    w.add_argument("--test", action="store_true", help="条件外でも送信する（通知テスト用）")
    w.set_defaults(func=cmd_watch)

    sub.add_parser("list", help="保存済み企業と判定の一覧").set_defaults(func=cmd_list)
    s = sub.add_parser("show", help="保存済みの財務データと判定を表示")
    s.add_argument("code")
    s.set_defaults(func=cmd_show)
    sub.add_parser("test-discord", help="Discord 通知テスト").set_defaults(func=cmd_test_discord)
    sub.add_parser("db-info", help="保存先（Turso / SQLite）と件数を表示").set_defaults(func=cmd_db_info)
    im = sub.add_parser("import-sqlite", help="ローカルの SQLite ファイルの中身を Turso にコピー")
    im.add_argument("path", help="例: data/stock_app.db")
    im.set_defaults(func=cmd_import_sqlite)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
