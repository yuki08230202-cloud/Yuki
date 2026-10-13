"""Chatwork 無料プランの組織外コンタクト上限（既定 20 名）の監視とコンタクト申請の承認。

- 現在数・残り追加可能数を記録し、15 名以上で警告
- 上限到達時は新規のコンタクト承認を停止（承認済みの依頼も実行時に再チェックして止める）
- 既存コンタクトの自動削除はしない
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import approvals, clock
from ..approvals import executor
from ..context import AppContext
from ..models import ApprovalRequest, ContactSnapshot, Worker


def status(db: Session, ctx: AppContext) -> dict:
    contacts = ctx.cw.contacts()
    requests = ctx.cw.incoming_requests()
    count = len(contacts)
    limit = ctx.settings.contact_limit
    snap = ContactSnapshot(count=count, limit=limit, pending_requests=len(requests))
    db.add(snap)
    db.flush()
    return {
        "count": count,
        "limit": limit,
        "remaining": max(0, limit - count),
        "warn": count >= ctx.settings.contact_warn_at,
        "full": count >= limit,
        "requests": requests,
    }


ALTERNATIVES = (
    "上限回避の運用案:\n"
    "1) Chatwork ビジネスプラン等の有料プランへ移行（組織外コンタクト上限の解除）\n"
    "2) 稼働していないワーカーとのコンタクトを管理者が手動で整理（システムは自動削除しません）\n"
    "3) 新規ワーカーはグループの招待リンク（/rooms/{id}/link）経由で参加してもらい、個別連絡はグループ内の宛先指定で行う\n"
    "詳細比較: docs/feasibility.md「コンタクト上限への対応」"
)


def check(db: Session, ctx: AppContext) -> dict:
    st = status(db, ctx)
    if st["full"]:
        ctx.notifier.notify(
            db, "warning", "【上限到達】Chatwork コンタクト数",
            f"組織外コンタクト {st['count']}/{st['limit']} 名。新規のコンタクト承認を停止しています。\n{ALTERNATIVES}",
        )
    elif st["warn"]:
        ctx.notifier.notify(
            db, "warning", "【注意】Chatwork コンタクト数が上限に近づいています",
            f"組織外コンタクト {st['count']}/{st['limit']} 名（残り {st['remaining']} 名）。\n{ALTERNATIVES}",
        )
    created = []
    for r in st["requests"]:
        if st["full"]:
            break
        req = approvals.dispatch(
            db,
            ctx,
            "contact_approve",
            title=f"コンタクト申請の承認: {r.get('name')}",
            message=r.get("message") or "",
            summary=f"申請者: {r.get('name')}（Chatwork ID: {r.get('chatwork_id')}）/ 承認後のコンタクト数 {st['count'] + 1}/{st['limit']}",
            targets=[{"key": str(r["account_id"]), "name": r.get("name", ""), "account_id": r["account_id"]}],
            payload={"request_id": r["request_id"], "account_id": r["account_id"], "name": r.get("name"),
                     "chatwork_id": r.get("chatwork_id")},
            dedupe_key=f"contact_approve:{r['request_id']}",
            warnings=["コンタクト数が警告ラインを超えています"] if st["warn"] else [],
        )
        created.append(req.id)
    return {"count": st["count"], "limit": st["limit"], "remaining": st["remaining"], "approvals": created}


@executor("contact_approve")
def _execute(db: Session, ctx: AppContext, req: ApprovalRequest) -> dict:
    count = len(ctx.cw.contacts())
    if count >= ctx.settings.contact_limit:
        raise RuntimeError(f"コンタクト上限 {count}/{ctx.settings.contact_limit} に達しているため承認を停止しました")
    p = req.payload
    res = ctx.cw.approve_incoming_request(int(p["request_id"]))
    w = db.scalar(select(Worker).where(Worker.account_id == int(p["account_id"])))
    if w is None:
        w = Worker(account_id=int(p["account_id"]), name=p.get("name") or "", chatwork_id=p.get("chatwork_id"))
        db.add(w)
    w.status = "contact_approved"
    if res and res.get("room_id"):
        w.direct_room_id = int(res["room_id"])
    db.flush()
    return {"summary": f"{p.get('name')} さんのコンタクト申請を承認（{count + 1}/{ctx.settings.contact_limit}）",
            "dry_run": bool(res and res.get("dry_run"))}
