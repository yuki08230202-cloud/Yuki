"""DB モデル。日時はすべて naive UTC。"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from . import clock


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- 実行モード
class JobSetting(Base):
    """業務単位の実行モード。A=確認必須 / B=自動実行＋結果通知 / C=完全自動。

    locked=True の業務（報酬・契約判断、根拠なし回答など）は MODE A 固定。
    モード変更は管理者の明示操作（approvals.change_mode）のみで行い、
    時間経過や実績による自動昇格は実装しない。
    """

    __tablename__ = "job_settings"
    job_type: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(1), default="A")
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


# ---------------------------------------------------------------- 承認
class ApprovalRequest(Base):
    """承認・実行の単位。dedupe_key で同一処理の二重作成を防ぐ。

    status:
      pending   承認待ち（MODE A）
      held      保留
      approved  承認済み・予定時刻待ち（scheduled_at が未来）
      executing 実行中（排他用の中間状態）
      executed  実行完了
      failed    実行失敗
      skipped   今回はスキップ（却下）
      expired   承認期限切れ（自動承認はしない）
    """

    __tablename__ = "approval_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64), index=True)
    mode: Mapped[str] = mapped_column(String(1))
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")          # 実行予定メッセージ全文（修正可）
    targets: Mapped[list] = mapped_column(JSON, default=list)       # [{key, name, ...}]
    selected_keys: Mapped[list | None] = mapped_column(JSON)        # 個別承認で選ばれた対象
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    url: Mapped[str | None] = mapped_column(String(500))
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime)
    token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    token_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    approval_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- 外部送信
class OutboundMessage(Base):
    """外部への送信記録。idempotency_key で同一送信の二重実行を防ぐ。"""

    __tablename__ = "outbound_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    channel: Mapped[str] = mapped_column(String(32))  # chatwork / email / indeed_manual / sheets
    target: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))   # sent / dry_run / failed / manual_pending
    external_id: Mapped[str | None] = mapped_column(String(100))
    approval_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    kind: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(16))  # chatwork / email / none
    status: Mapped[str] = mapped_column(String(16))   # sent / dry_run / failed
    error: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- 業務データ
class Worker(Base):
    __tablename__ = "workers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(100))
    chatwork_id: Mapped[str | None] = mapped_column(String(100))
    direct_room_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="active")  # applicant/contact_pending/active/inactive
    rate_per_send: Mapped[int | None] = mapped_column(Integer)          # 円/件（不明なら None → 請求書確認待ち）
    rate_per_appointment: Mapped[int | None] = mapped_column(Integer)
    tax_category: Mapped[str | None] = mapped_column(String(32))       # exclusive_10 / inclusive / exempt
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class Applicant(Base):
    """Indeed 応募者。API 取得できない場合は管理画面から CSV 取込。"""

    __tablename__ = "applicants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="indeed")
    external_id: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    job_title: Mapped[str | None] = mapped_column(String(200))
    applied_at: Mapped[str | None] = mapped_column(String(32))
    contact_url: Mapped[str | None] = mapped_column(String(500))  # Indeed 上のメッセージ画面 URL など
    # new → awaiting_approval → approved_manual_send(手動送信待ち) → guided(案内済み)
    status: Mapped[str] = mapped_column(String(32), default="new")
    guided_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class Recruitment(Base):
    """対象日ごとの募集投稿（対象日ユニークで二重募集を防止）。"""

    __tablename__ = "recruitments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, unique=True)
    room_id: Mapped[int | None] = mapped_column(Integer)
    message_id: Mapped[str | None] = mapped_column(String(50))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    approval_id: Mapped[int | None] = mapped_column(Integer)
    participants_collected_at: Mapped[datetime | None] = mapped_column(DateTime)


class Participation(Base):
    __tablename__ = "participations"
    __table_args__ = (UniqueConstraint("target_date", "account_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    account_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    source_message_id: Mapped[str] = mapped_column(String(50))
    requested_at: Mapped[datetime | None] = mapped_column(DateTime)


class SeenMessage(Base):
    """ポーリングで処理済みの Chatwork メッセージ。"""

    __tablename__ = "seen_messages"
    __table_args__ = (UniqueConstraint("room_id", "message_id", "purpose"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[str] = mapped_column(String(50))
    purpose: Mapped[str] = mapped_column(String(32))


class FaqEntry(Base):
    __tablename__ = "faq_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="manual")  # manual / faq / approved_answer
    normalized: Mapped[str] = mapped_column(Text, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (UniqueConstraint("room_id", "message_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    room_id: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[str] = mapped_column(String(50))
    account_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    body: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    grounding: Mapped[list] = mapped_column(JSON, default=list)
    approval_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="new")  # new/drafted/answered/skipped
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class DailyRecord(Base):
    __tablename__ = "daily_records"
    __table_args__ = (UniqueConstraint("work_date", "account_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    account_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    sent: Mapped[int] = mapped_column(Integer, default=0)
    unsendable: Mapped[int] = mapped_column(Integer, default=0)
    appointments: Mapped[int] = mapped_column(Integer, default=0)
    source_message_id: Mapped[str | None] = mapped_column(String(50))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class SheetChange(Base):
    """スプレッドシート更新の前後値。誤更新時は restore で復元する。"""

    __tablename__ = "sheet_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    approval_id: Mapped[int | None] = mapped_column(Integer)
    sheet_range: Mapped[str] = mapped_column(String(200))
    before: Mapped[list] = mapped_column(JSON)
    after: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))  # applied / dry_run / restored


class ContactSnapshot(Base):
    __tablename__ = "contact_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    count: Mapped[int] = mapped_column(Integer)
    limit: Mapped[int] = mapped_column(Integer)
    pending_requests: Mapped[int] = mapped_column(Integer, default=0)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("month", "worker_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id"))
    sent_count: Mapped[int] = mapped_column(Integer)
    appointment_count: Mapped[int] = mapped_column(Integer)
    subtotal: Mapped[int | None] = mapped_column(Integer)
    tax: Mapped[int | None] = mapped_column(Integer)
    total: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # ready / needs_review / sent
    issues: Mapped[list] = mapped_column(JSON, default=list)
    html: Mapped[str] = mapped_column(Text, default="")
    worker: Mapped[Worker] = relationship()


class TextTemplate(Base):
    """管理画面から登録したテンプレート（ファイルの既定テンプレートより優先）。"""

    __tablename__ = "text_templates"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
