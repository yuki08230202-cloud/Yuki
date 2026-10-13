# システム構成・運用設計

## 全体像

```
Cloud Scheduler ──(X-Job-Secret)──▶ Cloud Run: FastAPI (enpra)
  18:30 recruit-prepare                ├─ 承認エンジン（MODE A/B/C・一度だけ実行・期限切れは不実行）
  19:00 recruit-send                   ├─ 業務ジョブ（募集／参加集計／個別依頼／Indeed／コンタクト／質問／日報／請求書）
  22:00 participation                  ├─ スマホ管理画面（ログイン必須・CSRF 対策・一度限りの承認トークン）
  07:00 daily-report                   └─ Chatwork Webhook（署名検証）
  21:00 invoices（月末のみ作成）            │
  5分毎 qa-poll / maintenance              ├─▶ Chatwork API（DRY_RUN 時は書き込まない）
                                          ├─▶ Google Sheets API（更新前後の値を保存）
                                          ├─▶ Claude API（FAQ 回答案。任意）
                                          └─▶ Cloud SQL (PostgreSQL)：承認・送信履歴・監査ログ
管理者 ◀── Chatwork 通知（Bot）／メール（代替）── 承認リンク → ログイン → 承認・修正・スキップ・保留・自動化
```

## 承認の安全設計

| 要件 | 実装 |
|---|---|
| 承認なしで外部送信しない | MODE A の依頼は `pending` で止まります。実行されるのは管理者の `decide()` を経た場合だけです |
| タイムアウトで自動承認しない | 期限が切れると `expired`（実行しない）になり、管理者に再通知します |
| 承認後は一度だけ実行 | 状態を比較しながら更新し（`approved → executing`）、さらに送信ごとに一意のキー（`OutboundMessage.idempotency_key`）を付けています |
| リンクだけで実行できない | `/a/{token}` は管理者のログインが必要です。トークンは有効期限付き・一度限りで、DB にはハッシュだけを保存します。操作は POST と CSRF トークンで行います |
| 自動でモードが上がらない | モードを変えられるのは `change_mode()`（管理者の操作）だけです。システム自身を実行者とした変更は拒否します |
| 常に確認が必要な業務 | `qa_answer`（根拠なし・報酬・契約）と `daily_report_mismatch` は MODE A に固定しています |
| 失敗時 | `failed` として記録し、管理者に通知します。再実行は管理画面から行い、送信済みの相手には再送しません |
| 履歴 | 監査ログ（`AuditLog`）、送信履歴（`OutboundMessage`）、シート変更の前後値（`SheetChange`）を保存します |

## 業務と初期モード

| 業務 | job_type | 初期モード | 変更 |
|---|---|---|---|
| 毎日 19:00 の募集投稿 | recruit_post | A | 可 |
| 参加者への個別依頼 | individual_request | A | 可 |
| Indeed 応募者への案内 | indeed_guide | A | 可 |
| コンタクト申請の承認 | contact_approve | A | 可 |
| グループ招待 | group_invite | A | 可 |
| 質問への回答（FAQ に根拠あり） | qa_answer_faq | A | 可 |
| 質問への回答（根拠なし・報酬・契約） | qa_answer | A | **固定** |
| 日報・実績の集計 | daily_report | B | 可 |
| 日報の数値不一致の確定 | daily_report_mismatch | A | **固定** |
| 月末請求書の送付 | invoice_send | A | 可 |

## 毎日 19:00 の募集タイムライン

| 時刻 | 処理 |
|---|---|
| 18:30 | 翌日分の募集文を生成し、承認依頼を通知します（MODE B／C の場合は 19:00 に予約） |
| 18:30〜18:59 | 管理者が確認して承認します。承認済みのものは 19:00 まで待機します |
| 19:00 | 承認済みなら投稿します。未承認なら**送信せず**、新しいリンクで再通知します |
| 19:00 以降 | 承認画面に「承認して即時送信」または「今回はスキップ」が表示されます |

## クラウド環境の比較

概算の月額費用です（東京リージョン、低トラフィック。最新価格は各社の料金ページで確認してください）。

| 案 | 構成 | 月額の目安 | 運用のしやすさ | 障害対応 | 評価 |
|---|---|---|---|---|---|
| **A（推奨）** | Cloud Run ＋ Cloud Scheduler ＋ Cloud SQL（PostgreSQL・最小構成）＋ Secret Manager | 約 2,000〜3,000 円（大半が Cloud SQL。Cloud Run は無料枠内に収まる見込み。Scheduler は 3 ジョブ超過分が 1 ジョブ月 0.1 ドル） | ◎ サーバー管理が不要。自動バックアップと PITR（任意時点への復元）あり | ◎ Cloud Logging とアラート、Scheduler の自動再試行 | 安定性と手間のバランスが最もよい |
| B（低コスト） | Cloud Run ＋ Cloud Scheduler ＋ 外部のサーバーレス PostgreSQL（Neon／Supabase の無料枠） | 約 0〜500 円 | ○ | △ 無料枠には休止・容量・バックアップ期間の制限がある | 費用を最小にしたい場合。`DATABASE_URL` を差し替えるだけで移行できます |
| C | Compute Engine e2-micro（無料枠）＋ SQLite ＋ cron | 約 0〜500 円 | △ OS の更新や監視を自分で行う必要がある | △ 単一サーバーのため止まると全体が止まる | 非推奨 |
| D | Render／Railway など | 約 1,000〜3,000 円 | ○ | ○ | GCP を使わない場合の候補 |

**結論**：初期は **案 A** を推奨します。費用を抑えたい場合は、DB だけを案 B に変えても、構成はそのままで動きます。

### 障害の検知

- Cloud Run の `/healthz` に Uptime Check を設定し、失敗したらメールで通知します。
- ジョブが失敗した場合は、アプリから管理者に通知したうえで HTTP 500 を返し、Cloud Scheduler の再試行（最大 3 回）に任せます。再試行しても二重送信にはなりません。
- 送信できなかった通知はダッシュボードに表示されます。

## 段階的な自動化への移行

| 段階 | 対象 | 方法 |
|---|---|---|
| 第 0 段階（現在） | すべて DRY_RUN | `DRY_RUN=true`、`NOTIFY_DRY_RUN=true` で稼働させ、生成された文面と対象者を管理画面で確認します |
| 第 1 段階 | 通知・承認型 | まず `NOTIFY_DRY_RUN=false`（管理者通知のみ実送信）にし、承認フローを確認できたら `DRY_RUN=false` にします |
| 第 2 段階 | 一部自動化 | 問題なく動いている日報集計（既に B）、FAQ 回答、19 時の募集を、管理者が `/modes` で B にします |
| 第 3 段階 | 高度な自動化 | 応募者対応、招待、個別依頼、日報を B または C にします |
| 第 4 段階 | 最終運用 | 管理者が確認するのは、エラー・例外・固定 A の業務・月次の請求だけになります |

どの段階への移行も、**管理者が明示的に操作した場合にのみ**行われます。

## 本番化のチェックリスト

1. `python -m enpra.cli verify` を実行し、結果を `docs/feasibility.md` に記入する
2. 通知 Bot アカウントを作成し、管理者とコンタクトしたうえで `ADMIN_NOTIFY_ROOM_ID` を設定する
3. `python -m enpra.cli hash-password` で管理者パスワードのハッシュを作り、Secret Manager に登録する
4. DRY_RUN のまま 2〜3 日稼働させ、毎日の募集文・参加集計・日報の集計結果を確認する
5. 管理者の承認を得てから `NOTIFY_DRY_RUN=false` にする
6. 管理者の承認を得てから `DRY_RUN=false` にする（初回の各業務は MODE A のままにする）
