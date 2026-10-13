"""環境変数から設定を読み込む。

安全側の既定値:
- DRY_RUN=true          … ワーカー・応募者・中島さんへの送信、スプレッドシート更新を行わない
- NOTIFY_DRY_RUN=true   … 管理者への通知も実送信しない（ログ・DBにのみ記録）
本番化は「DRY_RUN 検証 → 管理者承認 → フラグを false に変更」の順で行う。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int | None) -> int | None:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return int(v)


def _list_int(name: str) -> list[int]:
    v = os.environ.get(name, "")
    return [int(x) for x in v.replace(" ", "").split(",") if x]


@dataclass
class Settings:
    # --- 安全フラグ ---
    dry_run: bool = True
    notify_dry_run: bool = True

    # --- 基本 ---
    base_url: str = "http://localhost:8080"
    database_url: str = "sqlite:///./enpra.db"
    timezone: str = "Asia/Tokyo"

    # --- 管理者 ---
    admin_name: str = "渡邉勇貴"
    admin_chatwork_id: str = "i4snxm1oompx"      # 応募者案内テンプレートに差し込む Chatwork ID
    admin_account_id: int | None = None          # 数値の account_id（API で使用。/me または /contacts で確認）
    admin_password_hash: str = ""                 # python -m enpra.cli hash-password で生成
    session_secret: str = ""
    session_max_age_sec: int = 12 * 3600

    # --- Chatwork ---
    chatwork_api_token: str = ""                  # ワーカー向け操作に使うトークン（管理者アカウント）
    chatwork_notify_token: str = ""               # 管理者通知用 Bot アカウントのトークン（未設定なら上と同じ）
    admin_notify_room_id: int | None = None       # 通知を送るルーム（Bot と管理者のダイレクトチャット推奨）
    recruit_room_id: int | None = None            # 募集を投稿するワーカーグループ
    question_room_ids: list[int] = field(default_factory=list)  # 質問を監視するルーム
    report_room_id: int | None = None             # 終了報告が投稿されるルーム（未設定なら recruit_room_id）
    invoice_room_id: int | None = None            # 中島さんへの請求書送付ルーム
    chatwork_webhook_token: str = ""              # Webhook 署名検証用トークン
    contact_limit: int = 20
    contact_warn_at: int = 15

    # --- 募集スケジュール ---
    recruit_prepare_time: str = "18:30"
    recruit_send_time: str = "19:00"
    participation_deadline: str = "22:00"         # 参加表明の締切（この時刻以降に個別依頼を準備）

    # --- 承認 ---
    approval_token_ttl_hours: int = 24

    # --- 代替通知（メール） ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    admin_email: str = ""

    # --- ジョブ実行の認証（Cloud Scheduler → /jobs/*） ---
    job_secret: str = ""

    # --- Google Sheets ---
    google_sheet_id: str = ""
    google_service_account_file: str = ""
    sheet_record_tab: str = "企業フォーム送付_記録"
    sheet_worklog_tab: str = ""                   # 照合元（ワーカー作業シート）。未設定なら照合はスキップ

    # --- Claude API（FAQ 回答案の作成） ---
    anthropic_model: str = "claude-opus-5-5"
    llm_enabled: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            dry_run=_bool("DRY_RUN", True),
            notify_dry_run=_bool("NOTIFY_DRY_RUN", True),
            base_url=e("BASE_URL", cls.base_url).rstrip("/"),
            database_url=e("DATABASE_URL", cls.database_url),
            admin_name=e("ADMIN_NAME", cls.admin_name),
            admin_chatwork_id=e("ADMIN_CHATWORK_ID", cls.admin_chatwork_id),
            admin_account_id=_int("ADMIN_ACCOUNT_ID", None),
            admin_password_hash=e("ADMIN_PASSWORD_HASH", ""),
            session_secret=e("SESSION_SECRET", ""),
            chatwork_api_token=e("CHATWORK_API_TOKEN", ""),
            chatwork_notify_token=e("CHATWORK_NOTIFY_TOKEN", ""),
            admin_notify_room_id=_int("ADMIN_NOTIFY_ROOM_ID", None),
            recruit_room_id=_int("RECRUIT_ROOM_ID", None),
            question_room_ids=_list_int("QUESTION_ROOM_IDS"),
            report_room_id=_int("REPORT_ROOM_ID", None),
            invoice_room_id=_int("INVOICE_ROOM_ID", None),
            chatwork_webhook_token=e("CHATWORK_WEBHOOK_TOKEN", ""),
            contact_limit=_int("CONTACT_LIMIT", 20),
            contact_warn_at=_int("CONTACT_WARN_AT", 15),
            recruit_prepare_time=e("RECRUIT_PREPARE_TIME", "18:30"),
            recruit_send_time=e("RECRUIT_SEND_TIME", "19:00"),
            participation_deadline=e("PARTICIPATION_DEADLINE", "22:00"),
            approval_token_ttl_hours=_int("APPROVAL_TOKEN_TTL_HOURS", 24),
            smtp_host=e("SMTP_HOST", ""),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_user=e("SMTP_USER", ""),
            smtp_password=e("SMTP_PASSWORD", ""),
            smtp_from=e("SMTP_FROM", ""),
            admin_email=e("ADMIN_EMAIL", ""),
            job_secret=e("JOB_SECRET", ""),
            google_sheet_id=e("GOOGLE_SHEET_ID", ""),
            google_service_account_file=e("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
            sheet_record_tab=e("SHEET_RECORD_TAB", "企業フォーム送付_記録"),
            sheet_worklog_tab=e("SHEET_WORKLOG_TAB", ""),
            anthropic_model=e("ANTHROPIC_MODEL", "claude-opus-5-5"),
            llm_enabled=_bool("LLM_ENABLED", False),
        )

    @property
    def effective_report_room_id(self) -> int | None:
        return self.report_room_id or self.recruit_room_id
