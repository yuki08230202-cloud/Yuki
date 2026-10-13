"""毎日19:00の翌日稼働者募集（土日祝含む）。

タイムライン（JST）:
  18:30 prepare()  … 翌日分の募集文を生成し承認依頼（MODE A）/ 予約（MODE B/C）
  19:00 send()     … 承認済みなら投稿。未承認なら送信せず再通知
  19:00以降の承認  … 承認画面で「承認して即時送信」を選ぶと即時投稿（スキップも可）
同じ対象日の募集は Recruitment.target_date / dedupe_key で一度だけ。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import approvals, clock
from ..approvals import executor
from ..context import AppContext
from ..models import ApprovalRequest, Recruitment
from ..outbound import send_once
from ..templates_text import render


def _dedupe(target: date) -> str:
    return f"recruit_post:{target.isoformat()}"


def build_message(db: Session, ctx: AppContext, target: date) -> str:
    return render("recruit", db, date_ja=clock.fmt_date_ja(target), deadline=ctx.settings.participation_deadline)


def prepare(db: Session, ctx: AppContext, target: date | None = None) -> ApprovalRequest:
    target = target or clock.tomorrow_jst()
    send_at = clock.at_jst(target - timedelta(days=1), ctx.settings.recruit_send_time)
    warnings = []
    if not ctx.settings.recruit_room_id:
        warnings.append("RECRUIT_ROOM_ID が未設定です")
    return approvals.dispatch(
        db,
        ctx,
        "recruit_post",
        title=f"{clock.fmt_date_ja(target)} 稼働者募集の投稿",
        message=build_message(db, ctx, target),
        summary=f"ワーカーグループ(room {ctx.settings.recruit_room_id})へ {ctx.settings.recruit_send_time} に投稿",
        targets=[{"key": "group", "name": "ワーカーグループ全員"}],
        payload={"target_date": target.isoformat(), "room_id": ctx.settings.recruit_room_id},
        dedupe_key=_dedupe(target),
        scheduled_at=send_at,
        warnings=warnings,
    )


def send(db: Session, ctx: AppContext, target: date | None = None) -> dict:
    """19:00 に呼ばれる。承認済みのみ投稿する。"""
    target = target or clock.tomorrow_jst()
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.dedupe_key == _dedupe(target)))
    if req is None:
        # 18:30 の準備が失敗していた場合：ここで作成（MODE A なら承認待ちになり送信はしない）
        req = prepare(db, ctx, target)
    if req.status == "approved":
        approvals.execute(db, ctx, req)
        return {"status": req.status, "approval_id": req.id}
    if req.status in ("pending", "held", "expired"):
        approvals.reissue_link(
            db,
            ctx,
            req,
            f"{ctx.settings.recruit_send_time} 時点で未承認のため、募集はまだ送信していません。"
            "このリンクから承認すると即時送信されます（送らない場合は「今回はスキップ」）。",
        )
        return {"status": "not_approved", "approval_id": req.id}
    return {"status": req.status, "approval_id": req.id}


@executor("recruit_post")
def _execute(db: Session, ctx: AppContext, req: ApprovalRequest) -> dict:
    target = date.fromisoformat(req.payload["target_date"])
    room_id = req.payload.get("room_id") or ctx.settings.recruit_room_id
    if not room_id:
        raise RuntimeError("RECRUIT_ROOM_ID が未設定です")
    rec = db.scalar(select(Recruitment).where(Recruitment.target_date == target))
    if rec and rec.message_id and not str(rec.message_id).startswith("dryrun-"):
        return {"summary": "既に投稿済みのため二重投稿しませんでした", "message_id": rec.message_id}

    om = send_once(
        db,
        key=_dedupe(target),
        channel="chatwork",
        target=f"room:{room_id}",
        body=req.message,
        approval_id=req.id,
        dry_run=ctx.cw.dry_run,
        send=lambda: ctx.cw.post_message(room_id, req.message),
    )
    rec = rec or Recruitment(target_date=target)
    rec.room_id = room_id
    rec.message_id = om.external_id
    rec.posted_at = clock.utcnow()
    rec.approval_id = req.id
    db.add(rec)
    db.flush()
    return {
        "summary": f"{clock.fmt_date_ja(target)} の募集を投稿しました（message_id={om.external_id}）",
        "message_id": om.external_id,
        "dry_run": om.status == "dry_run",
    }
