# 優良企業判定 & 株価ウォッチ（個人用）

ノートブックの分析ロジック（EDINET DB → 財務・CF・WACC 集計、ベイズβ、RSI/ゴールデンクロス → Discord）を 1 つのアプリにまとめたもの。

| 機能 | 内容 |
|---|---|
| 機能1 `analyze` | 企業コード → EDINET DB から財務取得 → 年度別に集計 → **SQLite に Upsert** → **Claude API** が解釈して「優良 / 要観察 / 非優良」を判定 → 優良ならウォッチリストに自動登録（非優良になったら自動解除） |
| 機能2 `watch` | ウォッチリスト（＝優良判定の企業）の株価を毎日チェック → 買いシグナルを **チャート画像付きで Discord** に送信 |

## セットアップ

```bash
pip install -r requirements.txt
cp .env.example .env   # EDINETDB_API_KEY / ANTHROPIC_API_KEY / DISCORD_WEBHOOK_URL を記入
python -m stock_app test-discord   # 通知テスト
```

## 使い方

```bash
# 機能1: 企業を判定（証券コード / 6723.T / EDINETコード / 社名 どれでも可、複数指定可）
python -m stock_app analyze 6723 8035 6141
python -m stock_app analyze 6723 --force     # 決算に更新が無くても再判定
python -m stock_app analyze 6723 --bayes     # ベータを PyMC で推定（要 pip install pymc arviz）
python -m stock_app analyze 6723 --notify    # 優良判定の追加/解除を Discord に通知

python -m stock_app analyze 6723 --pdf       # 判定後に PDF レポートを Discord に送信

# 保存済みの判定を PDF 化して Discord に送る
python -m stock_app report 6723
python -m stock_app report 6723 -o report.pdf --no-send   # 送らずファイル保存だけ

python -m stock_app list         # 保存済み企業・判定・ウォッチ状況
python -m stock_app show 6723    # 年度別データと判定の詳細

# 機能2: 株価ウォッチ
python -m stock_app watch --dry-run   # 判定だけ（送信しない）
python -m stock_app watch             # 本番
python -m stock_app watch --test      # 条件外でも送信（通知テスト）

# 画面で操作したい場合
streamlit run app.py
```

## 保存と更新（Upsert）の仕組み

SQLite（`data/stock_app.db`）に保存。

- `financials` … キーは `(edinet_code, period)`。`INSERT … ON CONFLICT DO UPDATE` で、値が変わった行だけ更新
- `evaluations` … Claude の判定履歴（全件残る）
- `watchlist` … 最新判定が「優良」の企業
- `signals` … 通知済みシグナル（同じ日の重複通知を防止）

**Claude の再判定は「決算データ（売上・利益・CF 等）に新規期の追加や修正があったとき」だけ**行う。β・WACC は株価で毎回わずかに変わるが、それだけでは再判定しない（API 費用の節約）。定期的に `analyze` を流しておけば、新しい有報が出た企業だけ自動で再判定される。

## データの保存先（Turso）

`.env`（または Streamlit Cloud の Secrets）に `TURSO_DATABASE_URL` と `TURSO_AUTH_TOKEN` を設定すると、データを Turso に保存する。
Streamlit Cloud・GitHub Actions・自分の PC のどこから実行しても同じデータを読み書きするので、画面で優良判定した企業が、翌日の自動チェックの対象に入る。未設定の場合はローカルの `data/stock_app.db` に保存する。

```bash
# Turso CLI（https://docs.turso.tech）で DB を作り、URL とトークンを取得
turso db create stock-app
turso db show --url stock-app        # → TURSO_DATABASE_URL
turso db tokens create stock-app     # → TURSO_AUTH_TOKEN

python -m stock_app db-info                          # 保存先と件数の確認
python -m stock_app import-sqlite data/stock_app.db  # 手元の SQLite のデータを Turso に移す（任意）
```

## 毎日の自動実行（GitHub Actions・PC 不要）

`.github/workflows/daily_watch.yml` が平日 16:00 JST に `watch` を実行する。

1. `.github` フォルダは**リポジトリの一番上**に置く（`app.py` と同じ `stock_app/` の中では動かない）
2. `app.py` がリポジトリ直下にある場合は、yml の `working-directory: stock_app` を `.` に変える
3. GitHub の Settings → Secrets and variables → Actions に次の 5 つを登録する
   `EDINETDB_API_KEY` / `ANTHROPIC_API_KEY` / `DISCORD_WEBHOOK_URL` / `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN`
4. Actions タブ → daily-watch → 「Run workflow」で手動実行して動作を確認する

自分の PC で動かしたい場合は cron（Mac/Linux）やタスク スケジューラ（Windows）で `python -m stock_app watch` を登録してもよい。ただしスリープ中は動かない。

## 判定ロジック

- **財務**: ROE と WACC のスプレッド、FCF・営業CF の黒字年数、売上 CAGR、営業利益率、自己資本比率を Claude に渡し、構造化出力（JSON Schema）で `verdict / score / 強み / リスク / 次の決算で見る点` を返させる。基準は `stock_app/analyst.py` の `SYSTEM_PROMPT` で調整
- **株価**: 次のどれかに当たれば買いシグナル（`stock_app/config.py` で閾値変更可）
  - SMA5 が SMA25 を上抜け（ゴールデンクロス）
  - 終値 > SMA25 かつ RSI < 50（上昇トレンド中の押し目）
  - RSI < 30（売られすぎ）

## ノートブックからの主な変更点

- 有利子負債: 「総資産 − 純資産」（＝負債合計）ではなく、借入金・社債などの明細（`short_term_loans`, `long_term_loans`, `bonds_payable` 等）を優先して合算。明細が無い場合のみ負債合計で代用
- ベータ: yfinance の `info["beta"]` ではなく、3 年週次リターンを日経平均に回帰して推定（`--bayes` でノートブックの PyMC 版）
- ウォッチ対象: 手書きの辞書ではなく、Claude が「優良」と判定した企業を DB から取得
- 最後のセルで未定義だった `send_to_discord` を `python -m stock_app test-discord` として実装

## テスト

```bash
pytest -q   # 外部 API はモックしているのでキー不要
```

> 判定は有報ベースの機械的なスクリーニングで、投資助言ではありません。
