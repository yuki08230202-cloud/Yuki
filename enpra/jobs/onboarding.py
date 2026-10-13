"""新規ワーカーのオンボーディング（グループ招待＋挨拶）。

コンタクト承認済み（status=contact_approved）のワーカーをワーカーグループに追加する。
PUT /rooms/{id}/members は全置換のため、ChatworkClient.add_room_members で既存メンバーとマージする。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import approvals
from ..approvals import executor, selected_targets
from ..context import AppContext
from ..models import ApprovalRequest, Worker
from ..outbound import send_once
from ..templates_text import render


def prepare_invites(db: Session, ctx: AppContext) -> ApprovalRequest | None:
    ws = db.scalars(select(Worker).where(Worker.status == "contact_approved")).all()
    if not ws:
        return None
    targets = [{"key": str(w.account_id), "name": w.name, "account_id": w.account_id} for w in ws]
    keys = ",".join(sorted(t["key"] for t in targets))
    return approvals.dispatch(
        db,
        ctx,
        "group_invite",
        title=f"新規ワーカー {len(targets)}名のグループ招待",
        message=render("welcome", db, name="{name}", admin_name=ctx.settings.admin_name),
        summary=f"ワーカーグループ(room {ctx.settings.recruit_room_id})へ追加し、個別に挨拶を送信",
        targets=targets,
        dedupe_key=f"group_invite:{keys}"[:200],
    )


@executor("group_invite")
def _execute(db: Session, ctx: AppContext, req: ApprovalRequest) -> dict:
    room = ctx.settings.recruit_room_id
    if not room:
        raise RuntimeError("RECRUIT_ROOM_ID が未設定です")
    chosen = selected_targets(req)
    ids = [int(t["account_id"]) for t in chosen]
    res = ctx.cw.add_room_members(room, ids)
    names = []
    for t in chosen:
        w = db.scalar(select(Worker).where(Worker.account_id == int(t["account_id"])))
        if w is None:
            continue
        if w.direct_room_id:
            body = req.message.replace("{name}", w.name)
            send_once(db, key=f"welcome:{w.account_id}", channel="chatwork", target=f"room:{w.direct_room_id}",
                      body=body, approval_id=req.id, dry_run=ctx.cw.dry_run,
                      send=lambda w=w, body=body: ctx.cw.post_message(w.direct_room_id, body))
        w.status = "active"
        names.append(w.name)
    db.flush()
    return {"summary": f"グループに追加: {'、'.join(names)}", "result": res, "dry_run": ctx.cw.dry_run}
