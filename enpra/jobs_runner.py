"""定期ジョブの名前 → 処理の対応表（Cloud Scheduler から /jobs/{name} で呼ぶ）。"""
from __future__ import annotations

from typing import Callable

from sqlalchemy.orm import Session

from . import approvals
from .context import AppContext
from .jobs import contacts, daily_report, indeed, invoices, onboarding, participation, qa, recruitment


def _id(x):
    return getattr(x, "id", x)


JOBS: dict[str, tuple[str, Callable[[Session, AppContext], object]]] = {
    "recruit-prepare": ("毎日18:30 翌日募集文の生成・承認依頼", lambda db, ctx: _id(recruitment.prepare(db, ctx))),
    "recruit-send": ("毎日19:00 募集の投稿（承認済みのみ）", lambda db, ctx: recruitment.send(db, ctx)),
    "participation": ("参加表明の締切後 個別依頼の準備", lambda db, ctx: _id(participation.prepare_requests(db, ctx))),
    "contacts-check": ("コンタクト数の確認・申請の承認依頼", lambda db, ctx: contacts.check(db, ctx)),
    "onboarding": ("新規ワーカーのグループ招待準備", lambda db, ctx: _id(onboarding.prepare_invites(db, ctx))),
    "indeed": ("Indeed未対応応募者への案内準備", lambda db, ctx: _id(indeed.prepare_guides(db, ctx))),
    "qa-poll": ("質問の検知と回答案の作成", lambda db, ctx: qa.poll(db, ctx)),
    "daily-report": ("日報の集計", lambda db, ctx: _id(daily_report.aggregate(db, ctx))),
    "invoices": ("月末なら請求書を作成", lambda db, ctx: _id(invoices.run_if_month_end(db, ctx))),
    "maintenance": (
        "期限切れ処理・予定時刻を過ぎた承認済み処理の実行",
        lambda db, ctx: {"expired": approvals.expire_stale(db, ctx), "ran": approvals.run_due(db, ctx)},
    ),
}


def run_job(db: Session, ctx: AppContext, name: str):
    if name not in JOBS:
        raise KeyError(name)
    approvals.ensure_job_settings(db)
    return JOBS[name][1](db, ctx)
