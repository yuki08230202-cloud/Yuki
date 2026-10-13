"""承認管理エンジン。

設計上の保証:
1. MODE A の処理は管理者が承認するまで実行されない。タイムアウトで自動承認されることはない
   （期限切れは status=expired となり、実行されないまま再通知される）。
2. 承認後の実行は一度だけ。status の比較更新（pending/approved → executing）で排他し、
   さらに外部送信は OutboundMessage.idempotency_key で二重送信を防ぐ。
3. モード変更は change_mode() による管理者の明示操作のみ。実績による自動昇格はない。
4. locked な業務（報酬・契約判断、根拠のない回答など）は MODE A から変更できない。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from . import clock
from .context import AppContext
from .models import ApprovalRequest, AuditLog, JobSetting
from .security import new_token, token_hash

log = logging.getLogger(__name__)

MODES = {"A": "確認必須", "B": "自動実行＋結果通知", "C": "完全自動"}

# job_type: (表示名, 初期モード, locked)
JOB_TYPES: dict[str, tuple[str, str, bool]] = {
    "recruit_post": ("毎日19:00の募集投稿", "A", False),
    "individual_request": ("参加者への個別依頼", "A", False),
    "indeed_guide": ("Indeed応募者への案内", "A", False),
    "contact_approve": ("コンタクト申請の承認", "A", False),
    "group_invite": ("ワーカーグループへの招待", "A", False),
    "qa_answer_faq": ("質問回答（FAQ根拠あり）", "A", False),
    "qa_answer": ("質問回答（根拠なし・報酬/契約）", "A", True),
    "daily_report": ("日報・実績の集計反映", "B", False),
    "daily_report_mismatch": ("日報の数値不一致の確定", "A", True),
    "invoice_send": ("月末請求書の送付（中島さん宛）", "A", False),
}

Executor = Callable[[Session, AppContext, ApprovalRequest], dict]
EXECUTORS: dict[str, Executor] = {}


def executor(job_type: str):
    def deco(fn: Executor) -> Executor:
        EXECUTORS[job_type] = fn
        return fn

    return deco


class ApprovalError(RuntimeError):
    pass


# ------------------------------------------------------------------ モード
def ensure_job_settings(db: Session) -> None:
    existing = {j.job_type for j in db.scalars(select(JobSetting))}
    for jt, (_, mode, locked) in JOB_TYPES.items():
        if jt not in existing:
            db.add(JobSetting(job_type=jt, mode=mode, locked=locked, updated_by="system:init"))
    db.flush()


def get_mode(db: Session, job_type: str) -> str:
    js = db.get(JobSetting, job_type)
    if js is None:
        return JOB_TYPES.get(job_type, ("", "A", True))[1]
    return "A" if js.locked else js.mode


def change_mode(db: Session, job_type: str, mode: str, actor: str) -> JobSetting:
    """管理者の明示操作でのみ呼ばれる。actor は認証済み管理者であること。"""
    if mode not in MODES:
        raise ApprovalError(f"不正なモード: {mode}")
    if not actor or actor.startswith("system"):
        raise ApprovalError("モード変更は管理者の明示操作でのみ可能です")
    js = db.get(JobSetting, job_type)
    if js is None:
        raise ApprovalError(f"未知の業務: {job_type}")
    if js.locked and mode != "A":
        raise ApprovalError(f"「{JOB_TYPES[job_type][0]}」は常に確認必須（MODE A 固定）です")
    old = js.mode
    js.mode = mode
    js.updated_by = actor
    js.updated_at = clock.utcnow()
    db.add(AuditLog(actor=actor, action="change_mode", detail={"job_type": job_type, "from": old, "to": mode}))
    db.flush()
    return js


# ------------------------------------------------------------------ 作成
def dispatch(
    db: Session,
    ctx: AppContext,
    job_type: str,
    *,
    title: str,
    message: str = "",
    summary: str = "",
    targets: list[dict] | None = None,
    payload: dict | None = None,
    dedupe_key: str | None = None,
    scheduled_at: datetime | None = None,  # JST aware
    url: str | None = None,
    warnings: list[str] | None = None,
    force_confirm: bool = False,
) -> ApprovalRequest:
    """業務処理を登録する。モードに応じて通知 / 実行する。"""
    if dedupe_key:
        existing = db.scalar(select(ApprovalRequest).where(ApprovalRequest.dedupe_key == dedupe_key))
        if existing is not None:
            return existing

    mode = "A" if force_confirm else get_mode(db, job_type)
    req = ApprovalRequest(
        job_type=job_type,
        mode=mode,
        title=title,
        summary=summary,
        message=message,
        targets=targets or [],
        payload=payload or {},
        dedupe_key=dedupe_key,
        url=url,
        warnings=warnings or [],
        scheduled_at=clock.jst_to_utc_naive(scheduled_at) if scheduled_at else None,
        status="pending",
    )
    db.add(req)
    db.flush()
    db.add(AuditLog(actor="system", action="created", approval_id=req.id, detail={"job_type": job_type, "mode": mode}))

    if mode == "A":
        raw = _issue_token(ctx, req)
        ctx.notifier.notify(db, "approval", f"【承認依頼】{title}", render_approval_notice(ctx, req, raw))
        return req

    req.status = "approved"
    req.decided_by = f"auto:mode{mode}"
    req.decided_at = clock.utcnow()
    db.flush()
    if not _is_future(req.scheduled_at):
        execute(db, ctx, req)
    return req


def _issue_token(ctx: AppContext, req: ApprovalRequest) -> str:
    raw, h = new_token()
    req.token_hash = h
    req.token_used_at = None
    expires = clock.utcnow() + timedelta(hours=ctx.settings.approval_token_ttl_hours)
    req.token_expires_at = expires
    return raw


def approval_link(ctx: AppContext, raw_token: str, action: str | None = None) -> str:
    link = f"{ctx.settings.base_url}/a/{raw_token}"
    return f"{link}?action={action}" if action else link


def render_approval_notice(ctx: AppContext, req: ApprovalRequest, raw_token: str) -> str:
    targets = "、".join(t.get("name", str(t.get("key"))) for t in req.targets) or "-"
    lines = [
        f"業務内容: {JOB_TYPES.get(req.job_type, (req.job_type,))[0]} / {req.title}",
        f"対象者({len(req.targets)}名): {targets}",
        f"予定実行日時: {clock.fmt_dt_ja(req.scheduled_at) if req.scheduled_at else '承認後すぐ'}",
    ]
    if req.summary:
        lines.append(f"概要: {req.summary}")
    if req.url:
        lines.append(f"対象URL: {req.url}")
    if req.warnings:
        lines.append("⚠ 確認事項:\n" + "\n".join(f"・{w}" for w in req.warnings))
    lines += [
        "[hr]実行予定のメッセージ全文:",
        req.message or "(メッセージなし)",
        "[hr]",
        f"承認: {approval_link(ctx, raw_token, 'approve')}",
        f"却下・修正: {approval_link(ctx, raw_token, 'edit')}",
        f"※リンクの操作には管理者ログインが必要です。有効期限 {clock.fmt_dt_ja(req.token_expires_at)}・一度限り。",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ 承認操作
def find_by_token(db: Session, raw_token: str) -> ApprovalRequest | None:
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.token_hash == token_hash(raw_token)))
    if req is None or req.token_used_at is not None:
        return None
    if req.token_expires_at and req.token_expires_at < clock.utcnow():
        return None
    return req


DECIDABLE = ("pending", "held", "expired")


def decide(
    db: Session,
    ctx: AppContext,
    req: ApprovalRequest,
    action: str,
    actor: str,
    *,
    edited_message: str | None = None,
    selected_keys: list[str] | None = None,
    automate_mode: str | None = None,
) -> ApprovalRequest:
    """管理者の判断を反映する。actor は認証済みであることを呼び出し側で保証する。

    action: approve / edit_approve / skip / hold / automate
    """
    if not actor or actor.startswith("system") or actor.startswith("auto"):
        raise ApprovalError("承認は管理者の操作でのみ可能です")
    if req.status not in DECIDABLE:
        raise ApprovalError(f"この依頼は既に処理されています（{req.status}）")

    detail: dict = {"action": action}
    if action == "hold":
        req.status = "held"
        db.add(AuditLog(actor=actor, action="hold", approval_id=req.id, detail=detail))
        db.flush()
        return req

    if action == "skip":
        req.status = "skipped"
        _consume(req, actor)
        db.add(AuditLog(actor=actor, action="skip", approval_id=req.id, detail=detail))
        db.flush()
        return req

    if action not in ("approve", "edit_approve", "automate"):
        raise ApprovalError(f"不正な操作: {action}")

    if action == "edit_approve":
        if edited_message is None or not edited_message.strip():
            raise ApprovalError("修正後のメッセージが空です")
        detail["before"] = req.message
        req.message = edited_message
    if selected_keys is not None:
        valid = {str(t.get("key")) for t in req.targets}
        chosen = [k for k in selected_keys if k in valid]
        if req.targets and not chosen:
            raise ApprovalError("対象者が 1 人も選択されていません")
        req.selected_keys = chosen
        detail["selected"] = chosen
    if action == "automate":
        # 今回分を承認し、同種の処理のモードを管理者の指定どおりに変更する
        change_mode(db, req.job_type, automate_mode or "B", actor)
        detail["automate_mode"] = automate_mode or "B"

    req.status = "approved"
    _consume(req, actor)
    db.add(AuditLog(actor=actor, action=action, approval_id=req.id, detail=detail))
    db.flush()
    if not _is_future(req.scheduled_at):
        execute(db, ctx, req)
    return req


def _consume(req: ApprovalRequest, actor: str) -> None:
    now = clock.utcnow()
    req.token_used_at = now
    req.decided_by = actor
    req.decided_at = now


def _is_future(dt: datetime | None) -> bool:
    return dt is not None and dt > clock.utcnow()


# ------------------------------------------------------------------ 実行
def execute(db: Session, ctx: AppContext, req: ApprovalRequest) -> ApprovalRequest:
    """承認済みの処理を一度だけ実行する。"""
    res = db.execute(
        update(ApprovalRequest)
        .where(ApprovalRequest.id == req.id, ApprovalRequest.status == "approved")
        .values(status="executing", attempts=ApprovalRequest.attempts + 1)
    )
    if res.rowcount != 1:
        db.refresh(req)
        log.info("approval %s は実行対象外（status=%s）", req.id, req.status)
        return req
    db.refresh(req)

    fn = EXECUTORS.get(req.job_type)
    try:
        if fn is None:
            raise ApprovalError(f"実行処理が未登録: {req.job_type}")
        result = fn(db, ctx, req) or {}
        req.status = "executed"
        req.result = result
        req.executed_at = clock.utcnow()
        db.add(AuditLog(actor="system", action="executed", approval_id=req.id, detail={"result": _short(result)}))
        db.flush()
        if req.mode in ("A", "B") or req.warnings:
            ctx.notifier.notify(db, "result", f"【実行完了】{req.title}", _result_text(ctx, req))
    except Exception as e:  # noqa: BLE001 - 失敗は記録して管理者に通知
        log.exception("approval %s の実行に失敗", req.id)
        req.status = "failed"
        req.error = str(e)
        db.add(AuditLog(actor="system", action="failed", approval_id=req.id, detail={"error": str(e)[:500]}))
        db.flush()
        ctx.notifier.notify(
            db,
            "error",
            f"【実行失敗】{req.title}",
            f"エラー: {e}\n管理画面: {ctx.settings.base_url}/approvals/{req.id}\n"
            "（自動再実行はしません。内容を確認のうえ管理画面から再実行してください）",
        )
    return req


def retry_failed(db: Session, ctx: AppContext, req: ApprovalRequest, actor: str) -> ApprovalRequest:
    if req.status != "failed":
        raise ApprovalError("失敗した処理のみ再実行できます")
    req.status = "approved"
    db.add(AuditLog(actor=actor, action="retry", approval_id=req.id))
    db.flush()
    return execute(db, ctx, req)


def run_due(db: Session, ctx: AppContext, job_type: str | None = None) -> list[int]:
    """承認済みで予定時刻を過ぎたものを実行する。"""
    q = select(ApprovalRequest).where(
        ApprovalRequest.status == "approved",
        (ApprovalRequest.scheduled_at.is_(None)) | (ApprovalRequest.scheduled_at <= clock.utcnow()),
    )
    if job_type:
        q = q.where(ApprovalRequest.job_type == job_type)
    done = []
    for req in db.scalars(q).all():
        execute(db, ctx, req)
        done.append(req.id)
    return done


def expire_stale(db: Session, ctx: AppContext) -> list[int]:
    """承認期限切れの依頼を expired にする（実行はしない）。"""
    q = select(ApprovalRequest).where(
        ApprovalRequest.status.in_(("pending", "held")),
        ApprovalRequest.token_expires_at.is_not(None),
        ApprovalRequest.token_expires_at < clock.utcnow(),
    )
    ids = []
    for req in db.scalars(q).all():
        req.status = "expired"
        db.add(AuditLog(actor="system", action="expired", approval_id=req.id))
        ids.append(req.id)
        ctx.notifier.notify(
            db,
            "expired",
            f"【承認期限切れ・未実行】{req.title}",
            f"承認されないまま期限を過ぎたため実行していません。\n必要なら管理画面から再承認してください: "
            f"{ctx.settings.base_url}/approvals/{req.id}",
        )
    db.flush()
    return ids


def reissue_link(db: Session, ctx: AppContext, req: ApprovalRequest, reason: str) -> str:
    """未承認の依頼について新しい承認リンクを発行し再通知する。"""
    if req.status not in DECIDABLE:
        raise ApprovalError("承認待ちではありません")
    if req.status == "expired":
        req.status = "pending"
    raw = _issue_token(ctx, req)
    db.flush()
    ctx.notifier.notify(db, "approval", f"【再通知】{req.title}", f"{reason}\n\n{render_approval_notice(ctx, req, raw)}")
    return raw


def selected_targets(req: ApprovalRequest) -> list[dict]:
    if req.selected_keys is None:
        return list(req.targets)
    keys = set(req.selected_keys)
    return [t for t in req.targets if str(t.get("key")) in keys]


def _short(d: dict) -> dict:
    s = str(d)
    return d if len(s) < 2000 else {"truncated": s[:2000]}


def _result_text(ctx: AppContext, req: ApprovalRequest) -> str:
    r = req.result or {}
    lines = [f"業務: {JOB_TYPES.get(req.job_type, (req.job_type,))[0]}", f"モード: {req.mode}（{MODES[req.mode]}）"]
    if r.get("summary"):
        lines.append(str(r["summary"]))
    if r.get("dry_run"):
        lines.append("※ DRY_RUN のため外部送信・データ更新は行っていません")
    if req.warnings:
        lines.append("⚠ " + " / ".join(req.warnings))
    lines.append(f"詳細: {ctx.settings.base_url}/approvals/{req.id}")
    return "\n".join(lines)
