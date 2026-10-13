# エンプラ フォーム営業ワーカー管理システム

Chatwork と Google スプレッドシートを使い、フォーム営業ワーカーの「募集 → 参加者への個別依頼 → 日報集計 → 月末請求」と「Indeed 応募者への案内・質問への回答」を自動化するシステムです。
**初期運用は通知・承認型（半自動）** です。重要な外部操作は、管理者（渡邉勇貴）がスマートフォンで承認した場合にだけ実行されます。

- API の実現可能性（実装できる部分と制約のある部分）: [docs/feasibility.md](docs/feasibility.md)
- 構成・安全設計・クラウド比較・段階的な移行: [docs/architecture.md](docs/architecture.md)

## 主な機能

| 機能 | 初期モード | 概要 |
|---|---|---|
| 毎日 19:00 の募集投稿 | A | 18:30 に文面を作って通知し、承認済みなら 19:00 に投稿します。未承認なら送信せずに再通知します |
| 参加者への個別依頼 | A | 募集への「参加」返信を集計し（重複・取り消しを処理）、一括または個別の承認後に全員へ送ります |
| Indeed 応募者への案内 | A | CSV で取り込み、未対応者を抽出して Chatwork ID を i4snxm1oompx に差し替え、承認後に送信用の文面を用意します |
| コンタクト上限の監視 | — | 15 名で警告し、20 名で新規の承認を停止します。申請の承認には API を使います |
| 新規ワーカーの招待 | A | 既存メンバーとマージしたうえでグループに追加し、挨拶を送ります |
| 質問への回答 | A | 承認済みの回答と FAQ から回答案を作ります。根拠がない質問や報酬・契約の質問は常に確認必須です |
| 日報・実績の集計 | B | 終了報告を読み取ってシートと照合し、「企業フォーム送付_記録」に反映します。不一致があれば確認に回します |
| 月末請求書 | A | 単価や税区分が不明な人は除外し、承認後に中島さんへ送付します（振込は行いません） |

実行モードは業務ごとに、管理画面の `/modes` から変更できます（A：確認必須／B：自動実行と結果通知／C：完全自動）。システムが勝手にモードを上げることはありません。

## ローカルでの起動（DRY_RUN）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env    # 値を設定（DRY_RUN=true のまま）
python -m enpra.cli hash-password        # ADMIN_PASSWORD_HASH に設定
set -a; . ./.env; set +a
python -m enpra.cli verify               # API の実地検証（読み取りのみ）
uvicorn enpra.main:app --reload --port 8080
python -m enpra.cli run-job recruit-prepare   # ジョブの手動実行
pytest -q
```

## デプロイ

`deploy/cloud-run.sh`：Cloud Run、Cloud SQL、Cloud Scheduler、Secret Manager を使う構成です。秘密情報は Secret Manager に登録し、それ以外の設定は `deploy/env.yaml` に記載します。

## 安全フラグ

| 変数 | 既定値 | 意味 |
|---|---|---|
| `DRY_RUN` | `true` | ワーカー・応募者・中島さんへの送信と、シートの更新を行いません（読み取りは行います） |
| `NOTIFY_DRY_RUN` | `true` | 管理者への通知も送らず、記録だけを残します |

本番への切り替えは「DRY_RUN で検証 → 管理者の承認 → フラグを `false` に変更」の順で行います。

## ディレクトリ

```
enpra/
  approvals.py   承認エンジン（モード・一度だけの実行・期限切れの扱い）
  chatwork.py    Chatwork API クライアント（DRY_RUN・再試行・メンバーのマージ）
  notifier.py    管理者への通知（Chatwork → メール → DB）
  web.py         管理画面・承認リンク・ジョブ API・Webhook
  jobs/          業務ごとの処理
  messages/      文面テンプレート（管理画面からの上書きも可能）
  templates/     管理画面の HTML
tests/           自動テスト
docs/            実現可能性の検証・設計
deploy/          Cloud Run のデプロイスクリプト
```
