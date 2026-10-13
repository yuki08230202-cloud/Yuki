"""募集への参加表明の収集と、参加者全員への個別依頼。

Chatwork API はリアクションを取得できない（公式エンドポイントなし）ため、
募集メッセージへの「参加」返信（またはグループ内の「参加」投稿）で判定する。
本人の明示的な参加表明がない人は対象にしない。人数による足切りはしない。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import approvals, clock
from ..approvals import executor, selected_targets
from ..chatwork import to_header
from ..context import AppContext
from ..models import ApprovalRequest, Participation, Recruitment, Worker
from ..outbound import send_once
from ..templates_text import render

_TAG_RE = re.compile(r"\[(?:rp|To|piconname|picon|qt|qtmeta)[^\]]*\]|\[/qt\]", re.I)
_JOIN_RE = re.compile(r"^\s*(参加|参加します|参加希望|参加でお願いします|参加させてください)[!！。\s]*$")
_CANCEL_RE = re.compile(r"(不参加|参加できません|参加取り?消|キャンセル)")


def strip_tags(body: str) -> str:
    text = _TAG_RE.sub("", body)
    # 返信先の名前行（「〇〇さん」）を除く
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().endswith("さん")]
    return "\n".join(lines).strip()


def classify(body: str, room_id: int, recruit_message_id: str) -> str | None:
    """'join' / 'cancel' / None。"""
    is_reply = f"to={room_id}-{recruit_message_id}" in body
    text = strip_tags(body)
    if _CANCEL_RE.search(text):
        return "cancel" if (is_reply or len(text) <= 30) else None
    if _JOIN_RE.match(text):
        return "join"
    return None


def collect_participants(ctx: AppContext, rec: Recruitment, deadline: datetime | None = None) -> list[dict]:
    room_id = rec.room_id
    posted_unix = rec.posted_at.replace(tzinfo=timezone.utc).timestamp() if rec.posted_at else 0
    deadline_unix = deadline.timestamp() if deadline else None
    excluded = {ctx.settings.admin_account_id}
    intents: dict[int, dict] = {}
    msgs = sorted(ctx.cw.messages(room_id, force=True), key=lambda m: m.get("send_time", 0))
    for m in msgs:
        t = m.get("send_time", 0)
        if t < posted_unix or (deadline_unix and t > deadline_unix):
            continue
        acc = m.get("account", {})
        aid = int(acc.get("account_id", 0))
        if not aid or aid in excluded:
            continue
        kind = classify(m.get("body", ""), room_id, str(rec.message_id))
        if kind == "join":
            intents[aid] = {"account_id": aid, "name": acc.get("name", str(aid)), "message_id": str(m["message_id"])}
        elif kind == "cancel":
            intents.pop(aid, None)
    return list(intents.values())


def prepare_requests(db: Session, ctx: AppContext, target: date | None = None) -> ApprovalRequest | None:
    target = target or clock.tomorrow_jst()
    rec = db.scalar(select(Recruitment).where(Recruitment.target_date == target))
    if rec is None or rec.posted_at is None:
        ctx.notifier.notify(db, "info", "個別依頼の準備をスキップ", f"{clock.fmt_date_ja(target)} の募集が投稿されていません。")
        return None
    deadline = clock.at_jst(clock.today_jst(), ctx.settings.participation_deadline)
    people = collect_participants(ctx, rec, deadline)
    rec.participants_collected_at = clock.utcnow()

    warnings: list[str] = []
    if not people:
        ctx.notifier.notify(db, "info", f"{clock.fmt_date_ja(target)} 参加表明なし", "参加表明（「参加」返信）はありませんでした。")
        return None

    targets = []
    for p in people:
        w = db.scalar(select(Worker).where(Worker.account_id == p["account_id"]))
        if w is None:
            w = Worker(account_id=p["account_id"], name=p["name"], status="active")
            db.add(w)
            db.flush()
        if not db.scalar(
            select(Participation).where(Participation.target_date == target, Participation.account_id == p["account_id"])
        ):
            db.add(Participation(target_date=target, account_id=p["account_id"], name=p["name"], source_message_id=p["message_id"]))
        targets.append({"key": str(p["account_id"]), "name": p["name"], "account_id": p["account_id"]})
    db.flush()

    list_info = "（担当リストは管理者からの別途連絡に従ってください）"
    if ctx.sheets is None:
        warnings.append("営業リストの自動割当は未連携です。重複・不足がないか確認してください")
    else:
        try:
            avail = ctx.sheets.available_list_count()
            if avail is not None and avail < len(targets):
                warnings.append(f"配布可能リスト({avail})が参加者数({len(targets)})より少ない可能性があります")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"リスト残数の確認に失敗: {e}")

    template = render(
        "individual_request", db, name="{name}", date_ja=clock.fmt_date_ja(target), list_info=list_info
    )
    return approvals.dispatch(
        db,
        ctx,
        "individual_request",
        title=f"{clock.fmt_date_ja(target)} 参加者{len(targets)}名への個別依頼",
        message=template,
        summary="参加表明者全員へ個別チャットで依頼文を送信（{name} は各自の名前に置換）",
        targets=targets,
        payload={"target_date": target.isoformat()},
        dedupe_key=f"individual_request:{target.isoformat()}",
        warnings=warnings,
    )


@executor("individual_request")
def _execute(db: Session, ctx: AppContext, req: ApprovalRequest) -> dict:
    target = date.fromisoformat(req.payload["target_date"])
    sent, failed, fallback = [], [], []
    contacts_cache: dict[int, int] | None = None
    for t in selected_targets(req):
        aid = int(t["account_id"])
        body = req.message.replace("{name}", t["name"])
        w = db.scalar(select(Worker).where(Worker.account_id == aid))
        room_id = w.direct_room_id if w else None
        if room_id is None:
            if contacts_cache is None:
                contacts_cache = {int(c["account_id"]): int(c["room_id"]) for c in ctx.cw.contacts()}
            room_id = contacts_cache.get(aid)
            if w and room_id:
                w.direct_room_id = room_id
        if room_id is None:
            # コンタクト外: グループ内で宛先付きで依頼する
            room_id = ctx.settings.recruit_room_id
            body = to_header(aid, t["name"]) + body
            fallback.append(t["name"])
        try:
            send_once(
                db,
                key=f"individual_request:{target.isoformat()}:{aid}",
                channel="chatwork",
                target=f"room:{room_id}",
                body=body,
                approval_id=req.id,
                dry_run=ctx.cw.dry_run,
                send=lambda room_id=room_id, body=body: ctx.cw.post_message(room_id, body),
            )
            p = db.scalar(select(Participation).where(Participation.target_date == target, Participation.account_id == aid))
            if p:
                p.requested_at = clock.utcnow()
            sent.append(t["name"])
        except Exception as e:  # noqa: BLE001 - 全員分を試してからまとめて失敗扱い
            failed.append(f"{t['name']}: {e}")
    summary = f"送信 {len(sent)}名: {'、'.join(sent) or '-'}"
    if fallback:
        summary += f"\nダイレクトチャットがないためグループで宛先指定: {'、'.join(fallback)}"
    if failed:
        raise RuntimeError(summary + "\n失敗: " + " / ".join(failed) + "\n（再実行すると未送信者にのみ送ります）")
    return {"summary": summary, "sent": sent, "dry_run": ctx.cw.dry_run}
